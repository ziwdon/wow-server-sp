"""In-container port of scripts/install-azerothcore.sh's heredoc-written
backup.sh. Writes mysqldump output to /ac/backups (the rw sub-mount) via
`docker exec ac-database mysqldump …`, plus a config tarball.

Why not invoke the host's backup.sh? It hardcodes
STACK_DIR=/opt/stacks/azerothcore (no env override) and writes via the
host filesystem; from inside the admin container the path doesn't exist
and the /ac mount is ro except for the docker-compose.admin.yml and
backups sub-mounts. Re-implementing the steps in Python is more reliable
than monkey-patching the host script.
"""

from __future__ import annotations

import datetime as dt
import logging
import subprocess
import tarfile
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

DATABASES = ("acore_auth", "acore_characters", "acore_world", "acore_playerbots")
DB_CONTAINER = "ac-database"


@dataclass
class BackupResult:
    ok: bool
    dumped: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    error: str | None = None


def _db_exists(db: str, db_password: str) -> bool:
    """Probe whether the database exists by running `USE <db>;`."""
    result = subprocess.run(
        [
            "docker", "exec", DB_CONTAINER,
            "mysql", "-uroot", f"-p{db_password}",
            "-e", f"USE {db};",
        ],
        capture_output=True,
    )
    if result.returncode != 0:
        msg = result.stderr.decode(errors="replace").strip()
        log.warning("db probe for %s returned rc=%d: %s", db, result.returncode, msg)
    return result.returncode == 0


def _dump_db(db: str, db_password: str, target: Path) -> bool:
    """mysqldump one database to `target`. Returns True on success."""
    result = subprocess.run(
        [
            "docker", "exec", DB_CONTAINER,
            "mysqldump", "-uroot", f"-p{db_password}",
            "--single-transaction", "--routines", "--triggers", "--events",
            db,
        ],
        capture_output=True,
    )
    if result.returncode != 0:
        log.error("mysqldump %s failed: %s", db, result.stderr.decode(errors="replace"))
        return False
    target.write_bytes(result.stdout)
    target.chmod(0o600)
    return True


def _write_git_revisions(stack_dir: Path, target: Path) -> bool:
    """Mirror backup.sh's git-revisions-<date>.txt: one line per repo
    with `name <sha>` (or `name unknown` if git fails). Best-effort; we
    record `unknown` rather than aborting the whole backup."""
    repos = (
        ("core", stack_dir),
        ("mod-playerbots", stack_dir / "modules" / "mod-playerbots"),
        ("mod-ah-bot-plus", stack_dir / "modules" / "mod-ah-bot-plus"),
    )
    try:
        lines: list[str] = []
        for name, path in repos:
            sha = "unknown"
            if path.exists():
                r = subprocess.run(
                    ["git", "-C", str(path), "rev-parse", "HEAD"],
                    capture_output=True, text=True,
                )
                if r.returncode == 0:
                    sha = r.stdout.strip() or "unknown"
            lines.append(f"{name} {sha}")
        target.write_text("\n".join(lines) + "\n")
        target.chmod(0o600)
        return True
    except OSError as e:
        log.error("git-revisions write failed: %s", e)
        return False


def _tar_config(stack_dir: Path, target: Path) -> bool:
    """tar.gz of .env + docker-compose.override.yml + configs/, matching
    backup.sh's content list. stack_dir is the in-container /ac mount."""
    try:
        with tarfile.open(target, "w:gz") as tf:
            for name in (".env", "docker-compose.override.yml", "configs"):
                src = stack_dir / name
                if src.exists():
                    tf.add(src, arcname=name)
        target.chmod(0o600)
        return True
    except OSError as e:
        log.error("config tar failed: %s", e)
        return False


def run_full_backup(
    *,
    backups_dir: Path,
    stack_dir: Path,
    db_password: str,
    date_str: str | None = None,
) -> BackupResult:
    """Run the four mysqldumps + config tarball. Returns BackupResult.

    `backups_dir`: where to write `*.sql` / `*.tar.gz`. Inside the admin
    container this is `/ac/backups`; in tests it's a tmpdir.
    `stack_dir`: the in-container AC stack root for tar source files.
    """
    date_str = date_str or dt.date.today().isoformat()
    result = BackupResult(ok=True)

    backups_dir.mkdir(parents=True, exist_ok=True)
    backups_dir.chmod(0o700)

    for db in DATABASES:
        if not _db_exists(db, db_password):
            log.warning("backup: database %s not present, skipping", db)
            result.skipped.append(db)
            continue
        target = backups_dir / f"{db}-{date_str}.sql"
        if _dump_db(db, db_password, target):
            result.dumped.append(db)
        else:
            result.ok = False
            result.error = f"mysqldump failed for {db}"
            return result

    cfg = backups_dir / f"azerothcore-config-{date_str}.tar.gz"
    if not _tar_config(stack_dir, cfg):
        result.ok = False
        result.error = "config tar failed"
        return result

    # Match the host's backup.sh by also writing git-revisions-<date>.txt.
    # Best-effort: a write failure here does NOT fail the whole backup,
    # because the SQL dumps and config tarball (the actually-restorable
    # artifacts) are already on disk.
    revs = backups_dir / f"git-revisions-{date_str}.txt"
    if not _write_git_revisions(stack_dir, revs):
        log.warning("git-revisions write failed; backup otherwise OK")

    return result
