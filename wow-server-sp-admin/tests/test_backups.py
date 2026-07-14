import os
import time
import io
import json
import tarfile
from pathlib import Path

from app.services.backups import backup_status, backups_summary, list_backups


def _touch(p, size=10):
    p.write_bytes(b"x" * size)


def _write_archive(path, *, skipped=(), valid=True, manifest=None):
    """Create a small canonical v1 archive, optionally marked partial."""
    with tarfile.open(path, "w:gz") as tf:
        if manifest is None:
            manifest = json.dumps({
                "format_version": 1,
                "databases": ["acore_auth", "acore_characters", "acore_world", "acore_playerbots"],
                "skipped_databases": list(skipped),
            }).encode()
        info = tarfile.TarInfo("manifest.json")
        info.size = len(manifest)
        tf.addfile(info, io.BytesIO(manifest))
        if valid:
            for db in ("acore_auth", "acore_characters", "acore_world", "acore_playerbots"):
                dump = b"-- Dump completed on 2026-07-11  3:00:01\n"
                sql = tarfile.TarInfo(f"sql/{db}.sql")
                sql.size = len(dump)
                tf.addfile(sql, io.BytesIO(dump))


def _write_v2_archive(path, *, sections, manifest=None):
    dump = b"".join(
        (
            f"-- Current Database: `{database}`\n"
            f"CREATE DATABASE `{database}`;\n"
            f"USE `{database}`;\n"
        ).encode()
        for database in sections
    ) + b"-- Dump completed on 2026-07-11 3:00:01\n"
    with tarfile.open(path, "w:gz") as tf:
        if manifest is None:
            manifest = json.dumps({
                "format_version": 2,
                "databases": ["acore_auth", "acore_characters", "acore_world", "acore_playerbots"],
                "skipped_databases": [],
                "dump_layout": "single-multi-database",
            }).encode()
        info = tarfile.TarInfo("manifest.json")
        info.size = len(manifest)
        tf.addfile(info, io.BytesIO(manifest))
        info = tarfile.TarInfo("sql/azerothcore.sql")
        info.size = len(dump)
        tf.addfile(info, io.BytesIO(dump))


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


def test_list_backups_exposes_complete_partial_and_corrupt_health(tmp_path):
    b = tmp_path / "backups"
    b.mkdir()
    _write_archive(b / "azerothcore-backup-manual-complete.tar.gz")
    _write_archive(
        b / "azerothcore-backup-manual-partial.tar.gz",
        skipped=("acore_world",),
    )
    (b / "azerothcore-backup-manual-corrupt.tar.gz").write_bytes(b"not a gzip archive")

    rows = {row.filename: row for row in list_backups(backups_dir=b)}

    assert rows["azerothcore-backup-manual-complete.tar.gz"].health == "complete"
    assert rows["azerothcore-backup-manual-complete.tar.gz"].restorable is True
    assert rows["azerothcore-backup-manual-partial.tar.gz"].health == "partial"
    assert rows["azerothcore-backup-manual-partial.tar.gz"].restorable is False
    assert "acore_world" in rows["azerothcore-backup-manual-partial.tar.gz"].health_detail
    assert rows["azerothcore-backup-manual-corrupt.tar.gz"].health == "corrupt"
    assert rows["azerothcore-backup-manual-corrupt.tar.gz"].restorable is False


def test_list_backups_marks_noncanonical_v2_streams_unrestorable(tmp_path):
    b = tmp_path / "backups"
    b.mkdir()
    expected = ("acore_auth", "acore_characters", "acore_world", "acore_playerbots")
    for suffix, sections in (
        ("missing", expected[:-1]),
        ("extra", (*expected, "unexpected_schema")),
        ("reordered", (expected[1], expected[0], *expected[2:])),
    ):
        _write_v2_archive(
            b / f"azerothcore-backup-manual-{suffix}.tar.gz",
            sections=sections,
        )

    rows = list_backups(backups_dir=b)

    assert len(rows) == 3
    assert all(row.health == "corrupt" and not row.restorable for row in rows)
    assert all("database sections" in row.health_detail for row in rows)


def test_list_backups_marks_duplicate_manifest_keys_unrestorable(tmp_path):
    b = tmp_path / "backups"
    b.mkdir()
    _write_v2_archive(
        b / "azerothcore-backup-manual-duplicate.tar.gz",
        sections=("acore_auth", "acore_characters", "acore_world", "acore_playerbots"),
        manifest=(
            b'{"format_version":99,"format_version":2,'
            b'"databases":["acore_auth","acore_characters","acore_world","acore_playerbots"],'
            b'"skipped_databases":[],"dump_layout":"single-multi-database"}'
        ),
    )

    [row] = list_backups(backups_dir=b)

    assert row.health == "corrupt"
    assert row.restorable is False
    assert row.health_detail == "archive manifest is malformed"


def test_list_backups_marks_duplicate_skipped_databases_corrupt_not_partial(tmp_path):
    b = tmp_path / "backups"
    b.mkdir()
    _write_archive(
        b / "azerothcore-backup-manual-duplicate-skipped.tar.gz",
        manifest=(
            b'{"format_version":1,'
            b'"databases":["acore_auth","acore_characters","acore_world","acore_playerbots"],'
            b'"skipped_databases":[],"skipped_databases":["acore_world"]}'
        ),
    )

    [row] = list_backups(backups_dir=b)

    assert row.health == "corrupt"
    assert row.restorable is False
    assert row.health_detail == "archive manifest is malformed"


def test_summary_excludes_partial_and_corrupt_archives_from_healthy_count(tmp_path):
    b = tmp_path / "backups"
    b.mkdir()
    _write_archive(b / "azerothcore-backup-manual-complete.tar.gz")
    _write_archive(b / "azerothcore-backup-manual-partial.tar.gz", skipped=("acore_world",))
    (b / "azerothcore-backup-manual-corrupt.tar.gz").write_bytes(b"not a gzip archive")

    summary = backups_summary(backups_dir=b)

    assert summary.total_count == 3
    assert summary.healthy_count == 1
    assert summary.unusable_count == 2


def test_list_backups_empty_dir(tmp_path):
    assert list_backups(backups_dir=tmp_path / "nope") == []


def test_list_backups_includes_preclear_label(tmp_path):
    b = tmp_path / "backups"
    b.mkdir()
    _touch(b / "azerothcore-backup-preclear-2026-05-30T10-00-00.tar.gz")
    rows = list_backups(backups_dir=b)
    assert len(rows) == 1
    assert rows[0].label == "preclear"
