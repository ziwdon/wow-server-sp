import os
import time
from pathlib import Path

from app.services.backups import backup_status


def test_returns_none_when_no_backups(tmp_path):
    s = backup_status(backups_dir=tmp_path, log_path=tmp_path / "backup.log")
    assert s.last_backup_unix is None
    assert s.last_error is None


def test_returns_most_recent_mtime(tmp_path):
    (tmp_path / "acore_auth-2026-05-19.sql").touch()
    older = time.time() - 86400
    os.utime(tmp_path / "acore_auth-2026-05-19.sql", (older, older))

    newer = tmp_path / "acore_auth-2026-05-20.sql"
    newer.touch()
    s = backup_status(backups_dir=tmp_path, log_path=tmp_path / "backup.log")
    assert s.last_backup_unix is not None
    assert s.last_backup_unix >= newer.stat().st_mtime - 1


def test_picks_up_error_line(tmp_path):
    log = tmp_path / "backup.log"
    log.write_text("[2026-05-19] Backed up acore_auth\n[2026-05-20] ERROR: tar failed\n")
    s = backup_status(backups_dir=tmp_path, log_path=log)
    assert s.last_error == "[2026-05-20] ERROR: tar failed"


def test_returns_last_error_line_when_multiple_errors(tmp_path):
    log = tmp_path / "backup.log"
    log.write_text(
        "[2026-05-18] ERROR: old failure\n"
        "[2026-05-19] Backed up acore_auth\n"
        "[2026-05-20] ERROR: tar failed\n"
    )
    s = backup_status(backups_dir=tmp_path, log_path=log)
    assert s.last_error == "[2026-05-20] ERROR: tar failed"


def test_returns_none_error_when_log_has_no_errors(tmp_path):
    log = tmp_path / "backup.log"
    log.write_text("[2026-05-19] Backed up acore_auth\n[2026-05-20] Backed up acore_world\n")
    s = backup_status(backups_dir=tmp_path, log_path=log)
    assert s.last_error is None
