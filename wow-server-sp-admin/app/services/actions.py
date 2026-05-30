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
        on_progress("backup", "backup FAILED")
        return ActionResult.ERROR
    on_progress("done", f"backup OK: {result.archive}")
    return ActionResult.OK


def read_manifest(archive: Path) -> dict | None:
    """Extract and parse manifest.json from a backup archive; None on failure."""
    try:
        with tarfile.open(archive, "r:gz") as tf:
            member = tf.extractfile("manifest.json")
            if member is None:
                return None
            return json.loads(member.read())
    except (tarfile.TarError, KeyError, OSError, json.JSONDecodeError) as e:
        log.error("could not read manifest from %s: %s", archive, e)
        return None


def _import_db(db: str, sql_path: Path, password: str, on_progress: ProgressCb) -> bool:
    on_progress("restore", f"restoring {db}")
    drop = subprocess.run(
        [
            "docker", "exec", "ac-database", "mysql", "-uroot", f"-p{password}",
            "-e", f"DROP DATABASE IF EXISTS {db}; CREATE DATABASE {db};",
        ],
        capture_output=True, text=True,
    )
    if drop.returncode != 0:
        on_progress("restore", f"{db}: drop/create failed: {drop.stderr.strip()}")
        return False
    with sql_path.open("rb") as fh:
        imp = subprocess.run(
            ["docker", "exec", "-i", "ac-database", "mysql", "-uroot", f"-p{password}", db],
            stdin=fh, capture_output=True, text=True,
        )
    if imp.returncode != 0:
        on_progress("restore", f"{db}: import failed: {imp.stderr.strip()}")
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

    # 2. Manifest + DB-set validation.
    manifest = read_manifest(archive)
    if manifest is None or manifest.get("format_version") != 1:
        on_progress("validate", "unsupported or missing manifest")
        return ActionResult.ERROR
    dbs = manifest.get("databases", [])
    unknown = [d for d in dbs if d not in KNOWN_DBS]
    if unknown:
        on_progress("validate", f"rejecting unknown DBs: {unknown}")
        return ActionResult.ERROR
    selected = [d for d in dbs if d in KNOWN_DBS]
    on_progress("validate", f"will restore: {selected}")

    # 3. Stop worldserver (no backup on stop anymore).
    stop = run_stop(on_progress=on_progress)
    if stop not in (ActionResult.OK, ActionResult.ALREADY):
        return stop

    stage: Path | None = None
    try:
        # 4. Pre-restore safety backup — abort (and restart) if it fails.
        on_progress("safety", "taking pre-restore safety backup")
        safety = create_backup("prerestore", on_progress=on_progress)
        if not safety.ok:
            on_progress("safety", "pre-restore backup FAILED; aborting and restarting")
            _restart_after_restore_failure(on_progress)
            return ActionResult.ERROR

        # 5. Extract + import.
        password = str(db_credentials()["password"])
        stage = Path(tempfile.mkdtemp(prefix="restore-"))
        with tarfile.open(archive, "r:gz") as tf:
            if _archive_has_unsafe_member(tf, stage):
                on_progress("validate", "archive contains unsafe paths")
                _restart_after_restore_failure(on_progress)
                return ActionResult.ERROR
            tf.extractall(stage, filter="data")
        missing_sql = [
            db for db in selected
            if not (stage / "sql" / f"{db}.sql").is_file()
        ]
        if missing_sql:
            on_progress("restore", f"archive missing SQL dumps for: {missing_sql}")
            _restart_after_restore_failure(on_progress)
            return ActionResult.ERROR
        for db in selected:
            sql_path = stage / "sql" / f"{db}.sql"
            if not _import_db(db, sql_path, password, on_progress):
                _restart_after_restore_failure(on_progress)
                return ActionResult.ERROR

        # 6. Restore admin.yml if present (the one config the admin may write).
        admin_yml = stage / "config" / "docker-compose.admin.yml"
        if admin_yml.is_file():
            on_progress("restore", "restoring docker-compose.admin.yml")
            _restore_admin_yml(admin_yml, ac_stack)
    except Exception as e:  # noqa: BLE001
        log.exception("restore failed after worldserver stop")
        on_progress("restore", f"restore failed: {e}")
        _restart_after_restore_failure(on_progress)
        return ActionResult.ERROR
    finally:
        if stage is not None:
            shutil.rmtree(stage, ignore_errors=True)

    # 7. Start.
    start = run_start(on_progress=on_progress)
    if start not in (ActionResult.OK, ActionResult.ALREADY):
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
    if info.status in ("exited", "missing"):
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
        on_progress("attach", f"console error: {e}")
        return ActionResult.ERROR

    on_progress("docker_stop", "docker stop --time 60 ac-worldserver")
    subprocess.run(
        ["docker", "stop", "--time", "60", WORLDSERVER],
        check=False,
    )

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
    result = subprocess.run(
        [*compose_args, "up", "-d", "ac-worldserver", "ac-database"],
        capture_output=True, text=True,
        env={**os.environ, **extra_env},
    )
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
    result = subprocess.run(
        ["docker", "stop", "--time", "60", WORLDSERVER],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        on_progress("force_stop", f"docker stop failed: {result.stderr}; escalating to kill")
        subprocess.run(["docker", "kill", WORLDSERVER], check=False)

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
    result = subprocess.run(
        [
            "docker", "exec", WORLDSERVER, "sh", "-c",
            'cat /azerothcore/env/dist/etc/worldserver.conf 2>/dev/null; '
            'for f in /azerothcore/env/dist/etc/modules/*.conf; do '
            '[ -f "$f" ] && cat "$f"; '
            'done',
        ],
        capture_output=True, text=True,
    )
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
    result = subprocess.run(
        ["docker", "exec", WORLDSERVER, "env"],
        capture_output=True, text=True,
    )
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
