"""Read /opt/stacks/azerothcore/backups/ for freshness + the backup.log for errors."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class BackupStatus:
    last_backup_unix: float | None
    last_error: str | None


def backup_status(*, backups_dir: Path, log_path: Path) -> BackupStatus:
    last_mtime: float | None = None
    if backups_dir.exists():
        mtimes = [p.stat().st_mtime for p in backups_dir.glob("*.sql")]
        if mtimes:
            last_mtime = max(mtimes)

    last_error: str | None = None
    if log_path.exists():
        for line in reversed(log_path.read_text(errors="replace").splitlines()):
            if "ERROR" in line:
                last_error = line
                break

    return BackupStatus(last_backup_unix=last_mtime, last_error=last_error)
