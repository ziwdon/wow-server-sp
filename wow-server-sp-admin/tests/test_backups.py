import os
import time
import tarfile
from pathlib import Path

import pytest

from app.services.backups import (
    BackupListingError,
    backup_status,
    backups_summary,
    list_backups,
    resolve_backup_archive,
)


def _touch(p, size=10):
    p.write_bytes(b"x" * size)


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


def test_status_uses_latest_run_outcome_not_an_older_error(tmp_path):
    log = tmp_path / "backup.log"
    log.write_text(
        "[2026-05-20 01:00:00] ERROR: tar failed\n"
        "[2026-05-20 02:00:00] Starting backup (label=daily)...\n"
        "[2026-05-20 02:01:00] Backup complete.\n"
    )

    s = backup_status(backups_dir=tmp_path, log_path=log)

    assert s.last_error is None


def test_returns_none_error_when_log_has_no_errors(tmp_path):
    log = tmp_path / "backup.log"
    log.write_text("[2026-05-19] Backed up acore_auth\n[2026-05-20] Backed up acore_world\n")
    s = backup_status(backups_dir=tmp_path, log_path=log)
    assert s.last_error is None


def test_list_backups_uses_file_mtime_newest_first(tmp_path):
    b = tmp_path / "backups"
    b.mkdir()
    daily = b / "azerothcore-backup-daily-2026-05-29.tar.gz"
    manual = b / "azerothcore-backup-manual-2026-05-29T14-03-10.tar.gz"
    pre = b / "azerothcore-backup-prerestore-2026-05-28T09-00-00.tar.gz"
    for p in (daily, manual, pre):
        _touch(p)
    _touch(b / "not-a-backup.txt")  # ignored

    # Distinct mtimes (oldest -> newest): prerestore, manual, daily.
    now = time.time()
    os.utime(pre, (now - 200, now - 200))
    os.utime(manual, (now - 100, now - 100))
    os.utime(daily, (now, now))

    rows = list_backups(backups_dir=b)
    assert len(rows) == 3
    assert {r.label for r in rows} == {"daily", "manual", "prerestore"}
    # Sorted newest-first by mtime, NOT by the date-only filename stamp
    # (which would otherwise sort the daily archive to 00:00 UTC).
    assert [r.label for r in rows] == ["daily", "manual", "prerestore"]
    # The daily row reflects its real write time (mtime), not the date-only
    # 00:00 UTC filename stamp. (Tolerance: datetime truncates to microseconds.)
    assert abs(rows[0].created.timestamp() - daily.stat().st_mtime) < 1


def test_backups_summary(tmp_path):
    b = tmp_path / "backups"
    b.mkdir()
    _touch(b / "azerothcore-backup-daily-2026-05-29.tar.gz", size=1000)
    _touch(b / "azerothcore-backup-manual-2026-05-29T14-03-10.tar.gz", size=2000)
    s = backups_summary(backups_dir=b)
    assert s.total_count == 2
    assert s.disk_used_bytes == 3000
    assert s.last_backup_unix is not None


def test_list_and_summary_never_open_or_validate_archives(tmp_path, monkeypatch):
    backups = tmp_path / "backups"
    backups.mkdir()
    archive = backups / "azerothcore-backup-manual-corrupt.tar.gz"
    archive.write_bytes(b"not a gzip archive")
    monkeypatch.setattr(tarfile, "open", lambda *_a, **_k: pytest.fail("archive opened"))
    monkeypatch.setattr(
        "app.services.actions.validate_canonical_backup",
        lambda *_a, **_k: pytest.fail("archive validated"),
    )
    real_exists = Path.exists

    def fail_listing_exists(path):
        if path == backups:
            pytest.fail("directory existence probed")
        return real_exists(path)

    monkeypatch.setattr(Path, "exists", fail_listing_exists)

    [row] = list_backups(backups_dir=backups)
    summary = backups_summary(backups_dir=backups)

    assert row.filename == archive.name
    assert row.size_bytes == len(b"not a gzip archive")
    assert summary.total_count == 1


def test_list_backups_wraps_real_directory_enumeration_errors(tmp_path):
    backups = tmp_path / "backups"
    backups.write_text("not a directory")

    with pytest.raises(BackupListingError, match=r"^Could not read backup metadata\.$"):
        list_backups(backups_dir=backups)


def test_list_backups_empty_dir(tmp_path):
    assert list_backups(backups_dir=tmp_path / "nope") == []


def test_list_backups_includes_preclear_label(tmp_path):
    b = tmp_path / "backups"
    b.mkdir()
    _touch(b / "azerothcore-backup-preclear-2026-05-30T10-00-00.tar.gz")
    rows = list_backups(backups_dir=b)
    assert len(rows) == 1
    assert rows[0].label == "preclear"


def test_matching_backup_symlink_is_rejected_without_following(tmp_path):
    backups = tmp_path / "backups"
    backups.mkdir()
    outside = tmp_path / "outside.tar.gz"
    outside.write_bytes(b"secret")
    link = backups / "azerothcore-backup-manual-link.tar.gz"
    link.symlink_to(outside)

    assert resolve_backup_archive(backups_dir=backups, archive_name=link.name) is None
    with pytest.raises(BackupListingError):
        list_backups(backups_dir=backups)
