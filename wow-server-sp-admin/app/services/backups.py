"""Read /opt/stacks/azerothcore/backups/ for freshness + the backup.log for errors."""

from __future__ import annotations

import datetime as _dt
import re
import tarfile
from dataclasses import dataclass
from pathlib import Path
from pathlib import Path as _Path


_ARCHIVE_RE = re.compile(
    r"^azerothcore-backup-(daily|manual|prerestore|preclear|imported)-(.+)\.tar\.gz$"
)


@dataclass(frozen=True)
class BackupStatus:
    last_backup_unix: float | None
    last_error: str | None


def backup_status(*, backups_dir: Path, log_path: Path) -> BackupStatus:
    last_mtime: float | None = None
    if backups_dir.exists():
        mtimes = [
            p.stat().st_mtime
            for pattern in ("azerothcore-backup-*.tar.gz", "*.sql")
            for p in backups_dir.glob(pattern)
        ]
        if mtimes:
            last_mtime = max(mtimes)

    last_error: str | None = None
    if log_path.exists():
        for line in reversed(log_path.read_text(errors="replace").splitlines()):
            if "Backup complete." in line:
                # A newer completed run supersedes historical failures. The log
                # still retains those entries for operators who need history.
                break
            if "] ERROR:" in line:
                last_error = line
                break

    return BackupStatus(last_backup_unix=last_mtime, last_error=last_error)


@dataclass(frozen=True)
class BackupInfo:
    filename: str
    label: str
    created: _dt.datetime
    size_bytes: int
    health: str
    health_detail: str
    restorable: bool


@dataclass(frozen=True)
class BackupsSummary:
    last_backup_unix: float | None
    total_count: int
    healthy_count: int
    unusable_count: int
    disk_used_bytes: int


def _archive_health(archive: Path) -> tuple[str, str, bool]:
    """Classify an archive without trusting its filename.

    A manifest that records skipped databases is a partial archive even if the
    tarball itself is readable. Everything else must satisfy the same canonical
    validator used by restore before it is advertised as restorable.
    """
    from app.services.actions import _load_strict_json, validate_canonical_backup

    try:
        with tarfile.open(archive, "r:gz") as tf:
            manifest_file = tf.extractfile("manifest.json")
            if manifest_file is None:
                return "corrupt", "missing manifest.json", False
            manifest = _load_strict_json(manifest_file.read())
    except (UnicodeDecodeError, ValueError):
        return "corrupt", "archive manifest is malformed", False
    except (tarfile.TarError, OSError):
        return "corrupt", "archive or manifest cannot be read", False

    if not isinstance(manifest, dict):
        return "corrupt", "archive manifest is malformed", False
    skipped = manifest.get("skipped_databases")
    if isinstance(skipped, list) and skipped:
        return "partial", f"skipped databases: {', '.join(map(str, skipped))}", False
    if skipped is not None and not isinstance(skipped, list):
        return "corrupt", "archive manifest has an invalid skipped_databases field", False

    validation_error = validate_canonical_backup(archive)
    if validation_error is not None:
        return "corrupt", validation_error, False
    return "complete", "complete and restorable", True


def list_backups(*, backups_dir: _Path) -> list[BackupInfo]:
    """Single-archive backups, newest first.

    The displayed time is the archive's mtime — the actual moment backup.sh
    wrote it — not the filename stamp. `daily` archives are stamped date-only by
    design (one per calendar day), so the stamp alone renders as 00:00 UTC;
    mtime is accurate to the second and matches the dashboard's "Last Backup"
    card. The filename is still parsed, but only for the label.
    """
    if not backups_dir.exists():
        return []
    out: list[BackupInfo] = []
    for p in backups_dir.glob("azerothcore-backup-*.tar.gz"):
        m = _ARCHIVE_RE.match(p.name)
        if not m:
            continue
        st = p.stat()
        health, health_detail, restorable = _archive_health(p)
        out.append(
            BackupInfo(
                filename=p.name,
                label=m.group(1),
                created=_dt.datetime.fromtimestamp(st.st_mtime, tz=_dt.timezone.utc),
                size_bytes=st.st_size,
                health=health,
                health_detail=health_detail,
                restorable=restorable,
            )
        )
    out.sort(key=lambda b: b.created, reverse=True)
    return out


def backups_summary(*, backups_dir: _Path) -> BackupsSummary:
    rows = list_backups(backups_dir=backups_dir)
    last = max((r.created.timestamp() for r in rows), default=None)
    return BackupsSummary(
        last_backup_unix=last,
        total_count=len(rows),
        healthy_count=sum(row.restorable for row in rows),
        unusable_count=sum(not row.restorable for row in rows),
        disk_used_bytes=sum(r.size_bytes for r in rows),
    )
