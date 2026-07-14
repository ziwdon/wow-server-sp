"""Read /opt/stacks/azerothcore/backups/ for freshness + the backup.log for errors."""

from __future__ import annotations

import datetime as _dt
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from app.services import logs as logs_svc


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
    for line in reversed(logs_svc.tail_filtered(log_path, n=10_000, max_bytes=1024 * 1024)):
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


@dataclass(frozen=True)
class BackupsSummary:
    last_backup_unix: float | None
    total_count: int
    disk_used_bytes: int


class BackupListingError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("Could not read backup metadata.")


def resolve_backup_archive(*, backups_dir: Path, archive_name: str) -> Path | None:
    """Resolve `archive_name` inside `backups_dir` WITHOUT following a final symlink.

    Rejects the name lexically first, then uses `lstat()` to confirm the
    directory entry itself is a regular file (not a symlink, not a fifo/device/
    dir), and finally confirms the fully-resolved real path's parent is
    exactly the canonical backups directory. This defeats both a symlink
    planted directly in the backups directory and a symlinked intermediate
    directory trick.
    """
    if (
        "/" in archive_name
        or ".." in archive_name
        or _ARCHIVE_RE.fullmatch(archive_name) is None
    ):
        return None
    try:
        root = backups_dir.resolve(strict=True)
        candidate = backups_dir / archive_name
        metadata = candidate.lstat()
        resolved = candidate.resolve(strict=True)
    except (FileNotFoundError, OSError):
        return None
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        return None
    if resolved.parent != root:
        return None
    return candidate


def list_backups(*, backups_dir: Path) -> list[BackupInfo]:
    """Single-archive backups, newest first.

    The displayed time is the archive's mtime — the actual moment backup.sh
    wrote it — not the filename stamp. `daily` archives are stamped date-only by
    design (one per calendar day), so the stamp alone renders as 00:00 UTC;
    mtime is accurate to the second and matches the dashboard's "Last Backup"
    card. The filename is still parsed, but only for the label.
    """
    try:
        entries = os.scandir(backups_dir)
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise BackupListingError() from exc

    try:
        out: list[BackupInfo] = []
        with entries:
            for entry in entries:
                m = _ARCHIVE_RE.match(entry.name)
                if not m:
                    continue
                if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                    # A matching name that is a symlink (or any other
                    # non-regular-file entry) is treated as a metadata read
                    # failure rather than silently followed or skipped — an
                    # attacker-planted symlink must never resolve here.
                    raise BackupListingError()
                st = entry.stat(follow_symlinks=False)
                out.append(
                    BackupInfo(
                        filename=entry.name,
                        label=m.group(1),
                        created=_dt.datetime.fromtimestamp(
                            st.st_mtime, tz=_dt.timezone.utc
                        ),
                        size_bytes=st.st_size,
                    )
                )
    except OSError as exc:
        raise BackupListingError() from exc
    out.sort(key=lambda b: b.created, reverse=True)
    return out


def backups_summary(*, backups_dir: Path) -> BackupsSummary:
    rows = list_backups(backups_dir=backups_dir)
    last = max((r.created.timestamp() for r in rows), default=None)
    return BackupsSummary(
        last_backup_unix=last,
        total_count=len(rows),
        disk_used_bytes=sum(r.size_bytes for r in rows),
    )
