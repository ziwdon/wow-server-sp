"""Lifecycle action state machines.

Each action accepts an `on_progress(step, msg)` callback so the calling
HTTP route can stream updates via SSE.
"""

from __future__ import annotations

import enum
import json
import logging
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import docker
import docker.errors

from app.services.console import WorldserverConsole
from app.services.docker_client import WORLDSERVER, inspect_worldserver
from app.services.env_var import config_key_to_ac_env_var
from app.state import db_credentials

ProgressCb = Callable[[str, str], None]
log = logging.getLogger(__name__)
KNOWN_DBS = ("acore_auth", "acore_characters", "acore_world", "acore_playerbots")
SQL_FOOTER_TAIL_BYTES = 8192
V2_SECTION_LINE_MAX_BYTES = 4096
MAX_EXPANDED_ARCHIVE_BYTES = 16 * 1024 ** 3
MAX_ARCHIVE_MEMBER_COUNT = 10_000
MAX_EXPANDED_MEMBER_BYTES = 8 * 1024 ** 3
MAX_MANIFEST_BYTES = 1024 ** 2
MAX_ADMIN_OVERLAY_BYTES = 1024 ** 2
ADMIN_OVERLAY_ARCHIVE_MEMBER = "config/docker-compose.admin.yml"
MANIFEST_ARCHIVE_MEMBER = "manifest.json"
V2_CREATE_DATABASE_RE = re.compile(
    r"^\s*CREATE\s+DATABASE\b(?:(?!;).)*?`(?P<database>[^`]+)`(?:(?!;).)*?;",
    re.IGNORECASE | re.MULTILINE | re.DOTALL,
)
V2_CREATE_DATABASE_START_RE = re.compile(rb"CREATE\s+DATABASE\b", re.IGNORECASE)
V2_CREATE_DATABASE_SCAN_BYTES = 64 * 1024
V2_CREATE_DATABASE_MAX_BYTES = 1024 * 1024
MYSQL_TIMEOUT = 3600
DOCKER_TIMEOUT = 180
QUICK_TIMEOUT = 30
STOPPED_WORLDSERVER_STATUSES = frozenset({"created", "dead", "exited", "missing"})
CLEAR_SQL = Path(
    os.environ.get("CLEAR_RNDBOTS_SQL", "/app/app/data/clear_rndbots.sql")
)


class ActionTimeout(RuntimeError):
    """A bounded external command exceeded its deadline."""


class ActionResult(str, enum.Enum):
    OK = "ok"
    TIMEOUT = "timeout"
    ALREADY = "already"
    ERROR = "error"


def _wait_for_status(target: str, timeout: int, on_progress: ProgressCb) -> bool:
    # A missing container is also a valid terminal state when stopping:
    # if the container was removed (manual docker rm, daemon restart) while
    # we were waiting, treating it as hung would waste the full timeout.
    terminal = {target, "missing"} if target == "exited" else {target}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        info = inspect_worldserver()
        if info.status in terminal:
            return True
        time.sleep(2)
    on_progress("wait_exit", f"timeout waiting for status={target}")
    return False


def run_backup_manual(*, on_progress: ProgressCb) -> ActionResult:
    """Create a single on-demand archive via the bundled backup.sh."""
    from app.services.backup import run_backup
    on_progress("backup", "creating manual backup")
    result = run_backup("manual", on_progress=on_progress)
    if not result.ok:
        if getattr(result, "timed_out", False):
            on_progress("backup", "backup TIMED OUT")
            return ActionResult.TIMEOUT
        on_progress("backup", "backup FAILED")
        return ActionResult.ERROR
    on_progress("done", f"backup OK: {result.archive}")
    return ActionResult.OK


def _reject_duplicate_json_keys(pairs) -> dict:
    """Build a JSON object while rejecting duplicate keys at every level."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_strict_json(payload):
    """Decode JSON without silently accepting duplicate object keys."""
    return json.loads(payload, object_pairs_hook=_reject_duplicate_json_keys)


def _load_manifest_member(archive: tarfile.TarFile) -> dict:
    """Read and parse manifest.json with its size bounded before any read.

    The size check runs against member metadata (``TarInfo.size``) first, so
    an oversized manifest is rejected without ever allocating a buffer for
    it. ``read(MAX_MANIFEST_BYTES + 1)`` is a second, defense-in-depth bound
    in case the header lied about size.
    """
    member = archive.getmember(MANIFEST_ARCHIVE_MEMBER)
    if not member.isfile() or member.size > MAX_MANIFEST_BYTES:
        raise ValueError("archive manifest exceeds its size limit")
    stream = archive.extractfile(member)
    if stream is None:
        raise ValueError("archive manifest cannot be read")
    payload = stream.read(MAX_MANIFEST_BYTES + 1)
    if len(payload) > MAX_MANIFEST_BYTES:
        raise ValueError("archive manifest exceeds its size limit")
    manifest = _load_strict_json(payload)
    if not isinstance(manifest, dict):
        raise ValueError("archive manifest must be an object")
    return manifest


def read_manifest(archive: Path) -> dict | None:
    """Extract and parse manifest.json from a backup archive; None on failure."""
    try:
        with tarfile.open(archive, "r:gz") as tf:
            return _load_manifest_member(tf)
    except (tarfile.TarError, KeyError, OSError, UnicodeDecodeError, ValueError) as e:
        log.error("could not read manifest from %s: %s", archive, e)
        return None


def _sql_has_canonical_completion_footer(sql_file, size: int) -> bool:
    """Check only the bounded tail for mysqldump's completion footer."""
    if size <= 0:
        return False
    sql_file.seek(max(0, size - SQL_FOOTER_TAIL_BYTES))
    tail = sql_file.read(SQL_FOOTER_TAIL_BYTES)
    return re.search(
        rb"(?:^|\n)-- Dump completed on [^\r\n]+\s*\Z", tail,
    ) is not None


def _validate_v2_sql_stream_inventory(sql_file) -> str | None:
    """Require exactly one canonical mysqldump database marker per v2 stream.

    `readline(limit)` keeps preflight memory bounded even when a dump contains
    exceptionally large INSERT rows. Only canonical ``-- Current Database``
    marker lines contribute to the inventory, matching the disaster-recovery
    restore contract.
    """
    expected_index = 0
    at_line_start = True
    prefix = b"-- Current Database:"
    marker = b"-- Current Database: `"
    try:
        while line := sql_file.readline(V2_SECTION_LINE_MAX_BYTES + 1):
            if at_line_start and line.startswith(prefix):
                if len(line) > V2_SECTION_LINE_MAX_BYTES or not line.endswith(b"\n"):
                    return "v2 multi-database SQL dump has an oversized database section header"
                stripped = line.rstrip(b"\r\n")
                if not (
                    stripped.startswith(marker)
                    and stripped.endswith(b"`")
                    and stripped.count(b"`") == 2
                ):
                    return "v2 multi-database SQL dump has malformed database section header"
                try:
                    section = stripped.split(b"`", 2)[1].decode("utf-8")
                except UnicodeDecodeError:
                    return "v2 multi-database SQL dump has unreadable database sections"
                if (
                    expected_index >= len(KNOWN_DBS)
                    or section != KNOWN_DBS[expected_index]
                ):
                    return (
                        "v2 multi-database SQL dump database sections must exactly match "
                        "the canonical databases once each"
                    )
                expected_index += 1
            at_line_start = line.endswith(b"\n")
    except (OSError, tarfile.TarError):
        return "v2 multi-database SQL dump has unreadable database sections"

    if expected_index != len(KNOWN_DBS):
        return (
            "v2 multi-database SQL dump database sections must exactly match "
            "the canonical databases once each"
        )
    return None


def _validate_archive_members(archive: tarfile.TarFile) -> str | None:
    """Apply extraction limits and reject member types outside the format.

    manifest.json and the admin overlay get their own tight, type-specific
    caps (checked from member metadata, before any read) instead of the
    generic multi-GiB member cap -- both are read into memory whole
    elsewhere, so their limit must be small enough to bound that allocation.
    """
    total_size = 0
    # Iterate lazily so a member-heavy archive is rejected at the limit rather
    # than first materializing every header through getmembers().
    for member_count, member in enumerate(archive, start=1):
        if member_count > MAX_ARCHIVE_MEMBER_COUNT:
            return "archive has too many members"
        normalized = member.name.rstrip("/")
        if normalized == ADMIN_OVERLAY_ARCHIVE_MEMBER and not member.isfile():
            return "archive admin overlay is not a regular file"
        if member.isdir():
            continue
        if not member.isfile():
            return f"archive contains unsupported member type: {member.name}"
        if normalized == MANIFEST_ARCHIVE_MEMBER:
            if member.size > MAX_MANIFEST_BYTES:
                return "archive manifest exceeds its size limit"
        elif normalized == ADMIN_OVERLAY_ARCHIVE_MEMBER:
            if member.size > MAX_ADMIN_OVERLAY_BYTES:
                return "archive admin overlay exceeds its size limit"
        elif member.size > MAX_EXPANDED_MEMBER_BYTES:
            return f"archive member exceeds the expanded-size limit: {member.name}"
        total_size += member.size
        if total_size > MAX_EXPANDED_ARCHIVE_BYTES:
            return "archive exceeds the total expanded-size limit"
    return None


def validate_canonical_backup(archive: Path) -> str | None:
    """Return an error unless *archive* is a complete canonical v1 or v2 backup."""
    try:
        with tarfile.open(archive, "r:gz") as tf:
            member_error = _validate_archive_members(tf)
            if member_error is not None:
                return member_error
            try:
                manifest = _load_manifest_member(tf)
            except KeyError:
                return "archive is missing manifest.json"
            except (UnicodeDecodeError, json.JSONDecodeError):
                return "archive manifest is malformed"
            except ValueError as e:
                # `_load_manifest_member`'s own bounded-read/shape errors carry
                # a stable, specific message; anything else that reaches JSON
                # decoding (e.g. duplicate-key rejection) is generic malformed
                # input and should not leak parser internals to the caller.
                message = str(e)
                if message in {
                    "archive manifest exceeds its size limit",
                    "archive manifest cannot be read",
                    "archive manifest must be an object",
                }:
                    return message
                return "archive manifest is malformed"
            format_version = manifest.get("format_version")
            if type(format_version) is not int or format_version not in (1, 2):
                return "archive has an unsupported manifest format"
            databases = manifest.get("databases")
            if not isinstance(databases, list) or databases != list(KNOWN_DBS):
                return "archive must contain exactly the four canonical databases"
            if manifest.get("skipped_databases") != []:
                return "partial archives are not restorable"

            if format_version == 2:
                if manifest.get("dump_layout") != "single-multi-database":
                    return "archive has an unsupported v2 dump layout"
                try:
                    sql_member = tf.getmember("sql/azerothcore.sql")
                except KeyError:
                    return "archive is missing the v2 multi-database SQL dump"
                sql_file = tf.extractfile(sql_member)
                if not sql_member.isfile() or sql_file is None or not _sql_has_canonical_completion_footer(sql_file, sql_member.size):
                    return "archive multi-database SQL dump is empty or incomplete"
                inventory_file = tf.extractfile(sql_member)
                if inventory_file is None:
                    return "archive is missing the v2 multi-database SQL dump"
                inventory_error = _validate_v2_sql_stream_inventory(inventory_file)
                if inventory_error is not None:
                    return inventory_error
                return None

            for db in KNOWN_DBS:
                try:
                    sql_member = tf.getmember(f"sql/{db}.sql")
                except KeyError:
                    return f"archive is missing SQL dump for {db}"
                if not sql_member.isfile():
                    return f"archive SQL dump for {db} is not a regular file"
                sql_file = tf.extractfile(sql_member)
                if sql_file is None or not _sql_has_canonical_completion_footer(
                    sql_file, sql_member.size,
                ):
                    return f"archive SQL dump for {db} is empty or incomplete"
    except (tarfile.TarError, OSError) as e:
        log.error("could not validate backup archive %s: %s", archive, e)
        return "archive cannot be read"
    return None


def _validate_restored_admin_yml(admin_yml: Path) -> str | None:
    """Validate archive overlay keys+values against the admin key index."""
    from app.services.compose_admin import validate_restored_overlay
    from app.state import get_state

    try:
        entries_by_env = {
            entry.env_var: entry for entry in get_state().key_index.values()
        }
    except RuntimeError:
        # Direct action tests do not initialize the web-app singleton. An empty
        # overlay remains safe, while any setting must fail closed.
        entries_by_env = {}
    return validate_restored_overlay(admin_yml, entries_by_env=entries_by_env)


def _import_db(db: str, sql_path: Path, password: str, on_progress: ProgressCb) -> bool:
    on_progress("restore", f"restoring {db}")
    for phase, statement in (
        ("drop", f"DROP DATABASE IF EXISTS {db};"),
        ("create", f"CREATE DATABASE {db};"),
    ):
        try:
            result = subprocess.run(
                [
                    "docker", "exec", "ac-database", "mysql", "-uroot", f"-p{password}",
                    "-e", statement,
                ],
                capture_output=True, text=True, timeout=MYSQL_TIMEOUT,
            )
        except subprocess.TimeoutExpired as e:
            on_progress("restore", f"{db}: {phase} timed out")
            raise ActionTimeout(f"{db} {phase} timed out") from e
        if result.returncode != 0:
            on_progress("restore", f"{db}: {phase} failed: {result.stderr.strip()}")
            return False
    with sql_path.open("rb") as fh:
        try:
            imp = subprocess.run(
                ["docker", "exec", "-i", "ac-database", "mysql", "-uroot", f"-p{password}", db],
                stdin=fh, capture_output=True, text=True, timeout=MYSQL_TIMEOUT,
            )
        except subprocess.TimeoutExpired as e:
            on_progress("restore", f"{db}: import timed out")
            raise ActionTimeout(f"{db} import timed out") from e
    if imp.returncode != 0:
        on_progress("restore", f"{db}: import failed: {imp.stderr.strip()}")
        return False
    return True


def _v2_create_database_statements(sql_path: Path) -> str:
    """Return the v2 dump's canonical CREATE DATABASE DDL verbatim."""
    definitions: dict[str, str] = {}
    pending = b""
    with sql_path.open("rb") as dump:
        while chunk := dump.read(V2_CREATE_DATABASE_SCAN_BYTES):
            pending += chunk
            while True:
                start = V2_CREATE_DATABASE_START_RE.search(pending)
                if start is None:
                    pending = pending[-len(b"CREATE DATABASE"):]
                    break
                end = pending.find(b";", start.start())
                if end < 0:
                    if len(pending) - start.start() > V2_CREATE_DATABASE_MAX_BYTES:
                        raise ValueError("v2 CREATE DATABASE statement exceeds the scan limit")
                    pending = pending[start.start():]
                    break
                statement = pending[start.start():end + 1]
                pending = pending[end + 1:]
                match = V2_CREATE_DATABASE_RE.fullmatch(statement.decode("utf-8"))
                if match is None:
                    continue
                database = match.group("database")
                if database not in KNOWN_DBS:
                    continue
                if database in definitions:
                    raise ValueError(f"v2 dump defines {database} more than once")
                definitions[database] = match.group(0).strip()
    missing = [database for database in KNOWN_DBS if database not in definitions]
    if missing:
        raise ValueError(f"v2 dump is missing CREATE DATABASE for: {', '.join(missing)}")
    return "\n".join(definitions[database] for database in KNOWN_DBS)


def _import_multi_database(sql_path: Path, password: str, on_progress: ProgressCb) -> bool:
    """Restore a v2 mysqldump --databases archive as one SQL stream."""
    on_progress("restore", "restoring consistent multi-database snapshot")
    try:
        create_sql = _v2_create_database_statements(sql_path)
    except (OSError, UnicodeError, ValueError) as e:
        on_progress("restore", f"v2 create failed: {e}")
        return False
    for phase, statement in (
        ("drop", " ".join(f"DROP DATABASE IF EXISTS {db};" for db in KNOWN_DBS)),
        ("create", create_sql),
    ):
        try:
            result = subprocess.run(
                ["docker", "exec", "ac-database", "mysql", "-uroot", f"-p{password}",
                 "-e", statement],
                capture_output=True, text=True, timeout=MYSQL_TIMEOUT,
            )
        except subprocess.TimeoutExpired as e:
            on_progress("restore", f"v2 {phase} timed out")
            raise ActionTimeout(f"v2 {phase} timed out") from e
        if result.returncode != 0:
            on_progress("restore", f"v2 {phase} failed: {result.stderr.strip()}")
            return False

    # Replaying the dump's idempotent database DDL preserves its charset and
    # collation defaults. The same statements are harmless no-ops in the full
    # consistent-snapshot stream imported below.
    try:
        with sql_path.open("rb") as fh:
            imp = subprocess.run(
                ["docker", "exec", "-i", "ac-database", "mysql", "-uroot", f"-p{password}"],
                stdin=fh, capture_output=True, text=True, timeout=MYSQL_TIMEOUT,
            )
    except subprocess.TimeoutExpired as e:
        on_progress("restore", "v2 import timed out")
        raise ActionTimeout("v2 import timed out") from e
    if imp.returncode != 0:
        on_progress("restore", f"v2 import failed: {imp.stderr.strip()}")
        return False
    return True


def _archive_has_unsafe_member(archive: tarfile.TarFile, target_dir: Path) -> bool:
    root = target_dir.resolve()
    for member in archive.getmembers():
        member_path = (target_dir / member.name).resolve()
        try:
            member_path.relative_to(root)
        except ValueError:
            return True
    return False


def _restore_admin_yml(admin_yml: Path, ac_stack: Path) -> None:
    from app.state import get_state

    try:
        state = get_state()
    except RuntimeError:
        target = ac_stack / "docker-compose.admin.yml"
        target.write_text(admin_yml.read_text())
        return
    state.admin.snapshot()
    with state.admin.path.open("w", encoding="utf-8") as f:
        f.write(admin_yml.read_text())


def _restart_after_restore_failure(on_progress: ProgressCb) -> None:
    try:
        run_start(on_progress=on_progress)
    except Exception as e:  # noqa: BLE001
        log.error("failed to restart after restore failure: %s", e)
        on_progress("start", f"restart after restore failure failed: {e}")


def _count_query(sql: str, password: str) -> int:
    """Run a single COUNT(*) via docker exec; return -1 on error."""
    try:
        r = subprocess.run(
            [
                "docker", "exec", "ac-database", "mysql", "-uroot", f"-p{password}",
                "-N", "-s", "-e", sql,
            ],
            capture_output=True, text=True, timeout=QUICK_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return -1
    if r.returncode != 0:
        return -1
    try:
        return int(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return -1


def run_clear_bots(*, on_progress: ProgressCb) -> ActionResult:
    """Destructively wipe the rndbot pool. stop → preclear backup → SQL → start.

    Mirrors run_restore's safety machinery. The pool regenerates on the
    next worldserver boot ramp.
    """
    from app.services.backup import run_backup as create_backup

    # 1. Stop (deleting under a live worldserver risks crashes + re-save races).
    stop = run_stop(on_progress=on_progress)
    if stop not in (ActionResult.OK, ActionResult.ALREADY):
        return stop

    try:
        # 2. Pre-clear safety backup — abort (and restart) if it fails.
        on_progress("safety", "taking pre-clear safety backup")
        safety = create_backup("preclear", on_progress=on_progress)
        if not safety.ok:
            on_progress("safety", "pre-clear backup FAILED; aborting and restarting")
            _restart_after_restore_failure(on_progress)
            return ActionResult.ERROR

        # 3. Pipe the bundled SQL to a single root mysql invocation.
        password = str(db_credentials()["password"])
        on_progress("clear", "running clear_rndbots.sql")
        with CLEAR_SQL.open("rb") as fh:
            imp = subprocess.run(
                [
                    "docker", "exec", "-i", "ac-database", "mysql", "-uroot",
                    f"-p{password}",
                ],
                stdin=fh, capture_output=True, text=True, timeout=MYSQL_TIMEOUT,
            )
        if imp.returncode != 0:
            on_progress("clear", f"clear SQL FAILED: {imp.stderr.strip()}")
            _restart_after_restore_failure(on_progress)
            return ActionResult.ERROR

        # 4. Informational post-clear verification (never fails the action).
        acc = _count_query(
            "SELECT COUNT(*) FROM acore_auth.account WHERE username LIKE 'RNDBOT%'",
            password)
        chars = _count_query(
            "SELECT COUNT(*) FROM acore_characters.characters c "
            "JOIN acore_auth.account a ON a.id=c.account "
            "WHERE a.username LIKE 'RNDBOT%'", password)
        rb = _count_query(
            "SELECT COUNT(*) FROM acore_playerbots.playerbots_random_bots", password)
        at = _count_query(
            "SELECT COUNT(*) FROM acore_playerbots.playerbots_account_type", password)
        on_progress(
            "verify",
            f"remaining — accounts:{acc} chars:{chars} random_bots:{rb} account_type:{at}",
        )
        if any(n not in (0, -1) for n in (acc, chars, rb, at)):
            on_progress("verify", "WARNING: non-zero remaining rndbot rows after clear")
    except subprocess.TimeoutExpired:
        on_progress("clear", "clear SQL timed out")
        _restart_after_restore_failure(on_progress)
        return ActionResult.TIMEOUT
    except Exception as e:  # noqa: BLE001
        log.exception("clear-bots failed after stop")
        on_progress("clear", f"clear failed: {e}")
        _restart_after_restore_failure(on_progress)
        return ActionResult.ERROR

    # 5. Start.
    start = run_start(on_progress=on_progress)
    if start not in (ActionResult.OK, ActionResult.ALREADY):
        return start
    on_progress("done", "bot pool cleared — regenerates on boot ramp")
    return ActionResult.OK


def run_restore(archive_name: str, *, on_progress: ProgressCb) -> ActionResult:
    """In-app restore: DB(s) + admin.yml. Same-machine rollback. See spec §9."""
    from app.services.backup import run_backup as create_backup

    ac_stack = Path(os.environ.get("AC_STACK_DIR", "/ac"))
    backups_dir = ac_stack / "backups"

    # 1. Validate the filename (no traversal) + existence.
    if (
        "/" in archive_name
        or ".." in archive_name
        or not archive_name.startswith("azerothcore-backup-")
        or not archive_name.endswith(".tar.gz")
    ):
        on_progress("validate", "invalid archive name")
        return ActionResult.ERROR
    archive = backups_dir / archive_name
    if not archive.is_file():
        on_progress("validate", "archive not found")
        return ActionResult.ERROR

    # 2. Validate the complete canonical archive before stopping worldserver.
    validation_error = validate_canonical_backup(archive)
    if validation_error is not None:
        on_progress("validate", validation_error)
        return ActionResult.ERROR
    # validate_canonical_backup already proved this manifest is well-formed
    # and within bounds; re-read it through the same bounded loader rather
    # than an unbounded extractfile().read() (this call site is easy to miss
    # when auditing manifest reads -- it does not go through
    # validate_canonical_backup's return path).
    with tarfile.open(archive, "r:gz") as tf:
        manifest = _load_manifest_member(tf)
    v2 = manifest["format_version"] == 2
    selected = list(KNOWN_DBS)
    on_progress("validate", f"will restore: {selected} ({'v2 consistent snapshot' if v2 else 'v1'})")

    stage: Path | None = None
    stopped = False
    try:
        # Extract and validate all SQL before stopping or replacing anything.
        stage = Path(tempfile.mkdtemp(prefix="restore-"))
        with tarfile.open(archive, "r:gz") as tf:
            member_error = _validate_archive_members(tf)
            if member_error is not None:
                on_progress("validate", member_error)
                return ActionResult.ERROR
            if _archive_has_unsafe_member(tf, stage):
                on_progress("validate", "archive contains unsafe paths")
                return ActionResult.ERROR
            tf.extractall(stage, filter="data")
        admin_yml = stage / "config" / "docker-compose.admin.yml"
        if admin_yml.exists() and not admin_yml.is_file():
            on_progress("validate", "archive admin overlay is not a regular file")
            return ActionResult.ERROR
        if admin_yml.is_file():
            overlay_error = _validate_restored_admin_yml(admin_yml)
            if overlay_error is not None:
                on_progress("validate", overlay_error)
                return ActionResult.ERROR
        stop = run_stop(on_progress=on_progress)
        if stop not in (ActionResult.OK, ActionResult.ALREADY):
            return stop
        stopped = True
        on_progress("safety", "taking pre-restore safety backup")
        safety = create_backup("prerestore", on_progress=on_progress)
        if not safety.ok:
            on_progress("safety", "pre-restore backup FAILED; aborting and restarting")
            _restart_after_restore_failure(on_progress)
            return ActionResult.ERROR
        password = str(db_credentials()["password"])
        if v2:
            imported = _import_multi_database(stage / "sql" / "azerothcore.sql", password, on_progress)
            if not imported:
                on_progress("restore", f"FAILED after replacing database(s); server remains stopped. Restore safety archive: {safety.archive}")
                return ActionResult.ERROR
        else:
            for db in selected:
                sql_path = stage / "sql" / f"{db}.sql"
                if not _import_db(db, sql_path, password, on_progress):
                    on_progress("restore", f"FAILED after replacing database(s); server remains stopped. Restore safety archive: {safety.archive}")
                    return ActionResult.ERROR

        # 6. Restore admin.yml if present (the one config the admin may write).
        if admin_yml.is_file():
            on_progress("restore", "restoring docker-compose.admin.yml")
            _restore_admin_yml(admin_yml, ac_stack)
    except ActionTimeout as e:
        log.warning("restore command timed out: %s", e)
        on_progress("restore", f"{e}; server remains stopped; restore the pre-restore archive to recover")
        return ActionResult.TIMEOUT
    except Exception as e:  # noqa: BLE001
        log.exception("restore failed")
        on_progress("restore", f"restore failed: {e}")
        if not stopped:
            return ActionResult.ERROR
        on_progress("restore", "server remains stopped; restore the pre-restore archive to recover")
        return ActionResult.ERROR
    finally:
        if stage is not None:
            shutil.rmtree(stage, ignore_errors=True)

    # 7. Start.
    start = run_start(on_progress=on_progress)
    if start not in (ActionResult.OK, ActionResult.ALREADY):
        on_progress(
            "restore",
            "server remains stopped; restore the pre-restore archive to recover",
        )
        return start
    on_progress("done", "restore complete")
    return ActionResult.OK


def run_stop(
    *,
    on_progress: ProgressCb,
    grace_seconds: int = 30,
) -> ActionResult:
    """Safe stop.

    Sequence (grace_seconds=30 default):
      t=0   announce + notify ("shutting down in 30s")
      t=20  announce + notify ("final 10 seconds")
      t=29  saveall (explicit save while AC is still healthy)
      t=30  detach, then `docker stop --time 60` (SIGTERM → AC's clean
            shutdown handler → World::StopNow(SHUTDOWN_EXIT_CODE) →
            final implicit saveall → exit code 0; Docker marks the
            container as user-stopped so `restart: unless-stopped`
            backs off). The `--time 60` window gives AC's final save
            headroom under load — on a quiet world it completes in
            5-15 s, but with ~2500 bot characters the save can stretch
            to 30-45 s; 60 s avoids a Docker-initiated SIGKILL while
            the save is mid-flight.
    We do NOT use `server shutdown N` — its countdown is collapsed by
    the SIGTERM `docker stop` sends, defeating its purpose.
    """
    info = inspect_worldserver()
    if info.status in STOPPED_WORLDSERVER_STATUSES:
        on_progress("inspect", f"already {info.status}")
        return ActionResult.OK

    # Compute the two sub-windows: most of the grace, then a final 10s.
    final_window = min(10, grace_seconds)
    early_window = max(0, grace_seconds - final_window - 1)

    on_progress("attach", "attaching to worldserver stdin")
    try:
        with WorldserverConsole(WORLDSERVER) as console:
            on_progress("notify", f"announcing {grace_seconds}s grace to players")
            console.send(
                f"announce Server shutting down in {grace_seconds} seconds "
                "for maintenance. Please log out safely."
            )
            console.send(f"notify Server shutting down in {grace_seconds}s.")

            if early_window > 0:
                on_progress("wait_grace", f"waiting {early_window}s")
                time.sleep(early_window)

            on_progress("notify_final", "final-10s warning")
            console.send("announce Final 10 seconds.")
            console.send("notify 10s remaining.")
            time.sleep(max(0, final_window - 1))

            on_progress("save", "saveall")
            console.send("saveall")
            time.sleep(1)
    except Exception as e:  # noqa: BLE001
        # A Docker daemon restart or another lifecycle action can stop the
        # container between the initial inspect and the PTY attach. Do not
        # turn that completed stop into a misleading attach warning followed
        # by a pointless wait for an already non-running container.
        latest = inspect_worldserver()
        if latest.status in STOPPED_WORLDSERVER_STATUSES:
            on_progress("inspect", f"already {latest.status}")
            return ActionResult.OK
        log.warning("console warning phase failed; proceeding with docker stop: %s", e)
        on_progress("attach", f"console warning phase failed ({e}); proceeding with docker stop")

    on_progress("docker_stop", "docker stop --time 60 ac-worldserver")
    try:
        subprocess.run(
            ["docker", "stop", "--time", "60", WORLDSERVER],
            check=False, timeout=DOCKER_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        on_progress("docker_stop", "docker stop timed out")
        return ActionResult.TIMEOUT

    on_progress("wait_exit", "waiting for exited state")
    if not _wait_for_status("exited", timeout=120, on_progress=on_progress):
        return ActionResult.TIMEOUT

    on_progress("done", "stopped")
    return ActionResult.OK


def _ac_compose_base_args(ac_container_path: Path) -> tuple[list[str], dict[str, str]]:
    """Build (cmd_args, extra_env) for a `docker compose` call that manages
    the AC project from inside the admin container.

    The crux: Docker Compose resolves relative bind-mount paths (./data/mysql
    etc.) relative to the project-directory. Inside the admin container AC's
    stack is mounted at /ac/ = /opt/stacks/azerothcore on the host. Without an
    explicit --project-directory, those paths resolve to /ac/... which the host
    Docker daemon cannot find.  We set --project-directory to the HOST-side path
    (determined from the admin container's own mount info) so the daemon gets
    valid paths like /opt/stacks/azerothcore/data/mysql.

    Separately, every AC service has `env_file: ${DOCKER_AC_ENV_FILE:-conf/dist/env.ac}`.
    With --project-directory pointing to the host path, that relative env_file
    would also resolve to the host path which is NOT accessible inside the
    container.  We override DOCKER_AC_ENV_FILE to the absolute /ac/... path in
    extra_env so the CLI can still read the file.

    Docker Compose does not search for compose files in --project-directory; it
    looks in CWD (/app/ — no compose files there).  We pass explicit -f args
    using /ac/... paths, derived from COMPOSE_FILE in /ac/.env.
    """
    # Determine host-side AC stack path by inspecting our own container mounts.
    host_ac_path = "/opt/stacks/azerothcore"  # documented install default
    try:
        client = docker.from_env()
        me = client.containers.get("azerothcore-admin")
        for m in me.attrs.get("Mounts", []):
            if m.get("Destination") == "/ac":
                found = m.get("Source", "").strip()
                if found:
                    host_ac_path = found
                    break
    except Exception:  # noqa: BLE001 — no daemon in tests or wrong container name
        pass

    # Read COMPOSE_FILE and COMPOSE_PROJECT_NAME from /ac/.env.
    compose_files = "docker-compose.yml"
    project_name = "azerothcore"
    env_path = ac_container_path / ".env"
    if env_path.exists():
        for line in env_path.read_text(errors="replace").splitlines():
            if line.startswith("COMPOSE_FILE="):
                compose_files = line[len("COMPOSE_FILE="):].strip()
            elif line.startswith("COMPOSE_PROJECT_NAME="):
                project_name = line[len("COMPOSE_PROJECT_NAME="):].strip()

    # Build -f args using /ac/... container paths (readable by the CLI).
    f_args: list[str] = []
    for cf in compose_files.split(":"):
        cf = cf.strip()
        if cf:
            f_args += ["-f", str(ac_container_path / cf)]

    cmd_args = [
        "docker", "compose",
        "--project-name", project_name,
        "--project-directory", host_ac_path,
        *f_args,
        "--env-file", str(env_path),
    ]

    # Override env_file path to the absolute container path so the CLI can read
    # it; the --project-directory host path is not accessible inside the container.
    extra_env = {"DOCKER_AC_ENV_FILE": str(ac_container_path / "conf/dist/env.ac")}

    return cmd_args, extra_env


WORLD_INIT_RE = re.compile(r"World\s+Initialized\s+In", re.IGNORECASE)


def _wait_for_world_init(timeout: int, on_progress: ProgressCb) -> bool:
    """Tail /ac/logs/Server.log for the world-init line. Emitted exactly
    once at end of boot, then Server.log goes quiet (per CLAUDE.md).

    Baselines `last_size` to the file's *current* size at entry so a stale
    "World Initialized" line from the previous boot does NOT trigger a
    false positive on Restart. AC's Server.log appender uses mode `w`
    (truncate-on-open), so once the new worldserver opens the file the
    size drops below `last_size`; we reset to 0 and read everything the
    new boot writes from then on.
    """
    log_path = Path(os.environ.get("AC_STACK_DIR", "/ac")) / "logs" / "Server.log"
    deadline = time.monotonic() + timeout
    last_size = log_path.stat().st_size if log_path.exists() else 0
    while time.monotonic() < deadline:
        if log_path.exists():
            size = log_path.stat().st_size
            if size < last_size:
                # AC re-opened Server.log in mode `w` -> file truncated.
                last_size = 0
            if size > last_size:
                with log_path.open("r", errors="replace") as f:
                    f.seek(last_size)
                    chunk = f.read()
                last_size = size
                if WORLD_INIT_RE.search(chunk):
                    return True
        time.sleep(2)
    on_progress("wait_init", "timeout waiting for World Initialized")
    return False


def run_start(*, on_progress: ProgressCb) -> ActionResult:
    info = inspect_worldserver()
    if info.status == "running":
        on_progress("inspect", "already running")
        return ActionResult.ALREADY

    ac_stack = Path(os.environ.get("AC_STACK_DIR", "/ac"))
    compose_args, extra_env = _ac_compose_base_args(ac_stack)

    on_progress("compose_up", "docker compose up -d ac-worldserver ac-database")
    try:
        result = subprocess.run(
            [*compose_args, "up", "-d", "ac-worldserver", "ac-database"],
            capture_output=True, text=True, timeout=300,
            env={**os.environ, **extra_env},
        )
    except subprocess.TimeoutExpired:
        on_progress("compose_up", "docker compose up timed out")
        return ActionResult.TIMEOUT
    if result.returncode != 0:
        on_progress("compose_up", f"compose up FAILED: {result.stderr}")
        return ActionResult.ERROR

    on_progress("wait_init", "waiting for World initialized line")
    if not _wait_for_world_init(timeout=300, on_progress=on_progress):
        return ActionResult.TIMEOUT

    on_progress("done", "running")
    return ActionResult.OK


def run_restart(
    *,
    on_progress: ProgressCb,
    grace_seconds: int = 30,
) -> ActionResult:
    stop_result = run_stop(
        on_progress=on_progress,
        grace_seconds=grace_seconds,
    )
    if stop_result not in (ActionResult.OK, ActionResult.ALREADY):
        return stop_result
    return run_start(on_progress=on_progress)


def run_force_stop(*, on_progress: ProgressCb) -> ActionResult:
    on_progress("force_stop", "docker stop --time 60 ac-worldserver")
    try:
        result = subprocess.run(
            ["docker", "stop", "--time", "60", WORLDSERVER],
            capture_output=True, text=True, timeout=DOCKER_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        on_progress("force_stop", "docker stop timed out")
        return ActionResult.TIMEOUT
    if result.returncode != 0:
        on_progress("force_stop", f"docker stop failed: {result.stderr}; escalating to kill")
        try:
            subprocess.run(["docker", "kill", WORLDSERVER], check=False, timeout=QUICK_TIMEOUT)
        except subprocess.TimeoutExpired:
            on_progress("force_stop", "docker kill timed out")
            return ActionResult.TIMEOUT

    if not _wait_for_status("exited", timeout=120, on_progress=on_progress):
        return ActionResult.TIMEOUT

    on_progress("done", "force-stopped (no backup taken)")
    return ActionResult.OK


# Matches a `Key = Value` line in a loaded .conf file. Same shape as
# the parser in services/config_index.py — both must agree on what
# counts as a key, otherwise the verifier reports false negatives for
# legitimately-loaded keys.
_CONF_KV_RE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9_.]*)\s*=")


@dataclass(frozen=True)
class VerifyFailure:
    env_var: str
    config_key: str | None  # None when the failure is "not present in env"
    reason: str  # short human-readable cause


def _read_loaded_config(on_progress: ProgressCb) -> set[str] | None:
    """Read every .conf actually loaded by the running worldserver and
    return the set of derived AC_* env-var names. Returns None on read
    error so callers can fail loudly."""
    try:
        result = subprocess.run(
            [
                "docker", "exec", WORLDSERVER, "sh", "-c",
                'cat /azerothcore/env/dist/etc/worldserver.conf 2>/dev/null; '
                'for f in /azerothcore/env/dist/etc/modules/*.conf; do '
                '[ -f "$f" ] && cat "$f"; '
                'done',
            ],
            capture_output=True, text=True, timeout=QUICK_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        on_progress("verify", "timed out reading loaded configs")
        return None
    if result.returncode != 0:
        on_progress("verify", f"could not read loaded configs: {result.stderr.strip()}")
        return None
    derived: set[str] = set()
    for line in result.stdout.splitlines():
        m = _CONF_KV_RE.match(line)
        if m:
            derived.add(config_key_to_ac_env_var(m.group(1)))
    return derived


def _read_live_env(on_progress: ProgressCb) -> dict[str, str] | None:
    try:
        result = subprocess.run(
            ["docker", "exec", WORLDSERVER, "env"],
            capture_output=True, text=True, timeout=QUICK_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        on_progress("verify", "timed out reading container env")
        return None
    if result.returncode != 0:
        on_progress("verify", f"docker exec env failed: {result.stderr.strip()}")
        return None
    out: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            out[k] = v
    return out


def verify_env_vars_bound(
    expected: dict[str, str],
    *,
    env_var_to_key: dict[str, str],
    on_progress: ProgressCb,
) -> list[VerifyFailure]:
    """Two-part check (mirrors the install script):

    (a) Presence — every expected env var is in the container's
        environment with the expected value.
    (b) Reverse-mapping — the env var name resolves back to a key in
        the .conf files actually loaded by worldserver. This is the
        only way to catch AC's silent-drop trap (env vars whose names
        don't match any loaded key are silently ignored).

    `env_var_to_key` is `{AC_FOO_BAR: "Foo.Bar"}` for every admin-set
    env var, supplied by the caller so the failure list can name the
    user-facing key alongside the env var.
    """
    on_progress("verify", f"checking {len(expected)} env vars in ac-worldserver")

    live = _read_live_env(on_progress)
    if live is None:
        return [
            VerifyFailure(v, env_var_to_key.get(v), "could not read container env")
            for v in expected
        ]

    loaded_envs = _read_loaded_config(on_progress)
    if loaded_envs is None:
        return [
            VerifyFailure(
                v, env_var_to_key.get(v),
                "could not read loaded .conf files (silent-drop check skipped)",
            )
            for v in expected
        ]

    failures: list[VerifyFailure] = []
    for var, want in expected.items():
        key = env_var_to_key.get(var)
        # (a) Presence.
        got = live.get(var)
        if got != str(want):
            on_progress("verify", f"{var}: expected {want!r}, got {got!r}")
            failures.append(VerifyFailure(var, key, f"value mismatch: got {got!r}"))
            continue
        # (b) Reverse-mapping — silent-drop trap.
        if var not in loaded_envs:
            on_progress(
                "verify",
                f"{var}: present in env but no loaded .conf key derives this "
                f"name — AzerothCore is silently dropping it",
            )
            failures.append(VerifyFailure(
                var, key,
                "silently dropped by AC (no loaded .conf key maps to this env var)",
            ))

    if not failures:
        on_progress("verify", "all env vars bound correctly")
    return failures


def run_reset_bots(*, on_progress: ProgressCb) -> ActionResult:
    """Re-roll the existing rndbot pool via the worldserver console.

    Fire-and-forget: we confirm dispatch, not completion. The re-roll
    runs server-side for some time after the command is sent.
    """
    info = inspect_worldserver()
    if info.status != "running":
        on_progress("inspect", f"server must be running (is {info.status})")
        return ActionResult.ERROR
    on_progress("attach", "attaching to worldserver stdin")
    try:
        with WorldserverConsole(WORLDSERVER) as console:
            console.send("playerbot rndbot init")
    except Exception as e:  # noqa: BLE001
        on_progress("attach", f"console error: {e}")
        return ActionResult.ERROR
    on_progress("done", "Command sent. Re-roll continues inside the worldserver.")
    return ActionResult.OK
