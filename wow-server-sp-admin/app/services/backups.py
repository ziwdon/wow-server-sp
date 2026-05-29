"""Read /opt/stacks/azerothcore/backups/ for freshness + the backup.log for errors."""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass
from pathlib import Path
from pathlib import Path as _Path


_ARCHIVE_RE = re.compile(
    r"^azerothcore-backup-(daily|manual|prerestore)-(.+)\.tar\.gz$"
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


@dataclass(frozen=True)
class BackupsSummary:
    last_backup_unix: float | None
    total_count: int
    disk_used_bytes: int


def _parse_stamp(stamp: str, fallback_mtime: float) -> _dt.datetime:
    for fmt in ("%Y-%m-%dT%H-%M-%S", "%Y-%m-%d"):
        try:
            return _dt.datetime.strptime(stamp, fmt).replace(tzinfo=_dt.timezone.utc)
        except ValueError:
            continue
    return _dt.datetime.fromtimestamp(fallback_mtime, tz=_dt.timezone.utc)


def list_backups(*, backups_dir: _Path) -> list[BackupInfo]:
    """Single-archive backups, newest first. Filename-only parse (no tar open)."""
    if not backups_dir.exists():
        return []
    out: list[BackupInfo] = []
    for p in backups_dir.glob("azerothcore-backup-*.tar.gz"):
        m = _ARCHIVE_RE.match(p.name)
        if not m:
            continue
        st = p.stat()
        out.append(
            BackupInfo(
                filename=p.name,
                label=m.group(1),
                created=_parse_stamp(m.group(2), st.st_mtime),
                size_bytes=st.st_size,
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
        disk_used_bytes=sum(r.size_bytes for r in rows),
    )
