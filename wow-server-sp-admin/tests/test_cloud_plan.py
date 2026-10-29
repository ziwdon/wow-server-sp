"""Pure decisions for the Proton Drive backup copy: stamps, candidates, classification, retention."""

from __future__ import annotations

import datetime as dt
from datetime import date

import pytest

from app.services.backups import _ARCHIVE_RE, BackupInfo
from app.services.cloud_config import CloudConfig
from app.services.cloud_plan import (
    RemoteEntry,
    archive_label,
    archive_stamp_date,
    classify,
    is_backup_name,
    plan_retention,
    select_candidates,
)

# 2026-10-28 12:00:00 UTC
EPOCH_OCT_28 = 1793188800
HEX32 = "a" * 32
TODAY = date(2026, 10, 28)


def _file(name: str | None, size: int = 1, sha1: str | None = None) -> RemoteEntry:
    return RemoteEntry(name, "file", "uid-" + str(name), size, sha1)


def _folder(name: str) -> RemoteEntry:
    return RemoteEntry(name, "folder", "uid-" + name, None, None)


def _info(name: str, iso_mtime: str, size: int = 1) -> BackupInfo:
    m = _ARCHIVE_RE.fullmatch(name)
    assert m, name
    return BackupInfo(
        filename=name,
        label=m.group(1),
        created=dt.datetime.fromisoformat(iso_mtime).replace(tzinfo=dt.timezone.utc),
        size_bytes=size,
    )


# --- stamps ---


def test_archive_stamp_date_formats():
    assert archive_stamp_date("azerothcore-backup-daily-2026-10-20.tar.gz") == date(2026, 10, 20)
    assert archive_stamp_date("azerothcore-backup-manual-2026-10-27T21-03-51.tar.gz") == date(2026, 10, 27)
    assert archive_stamp_date(
        f"azerothcore-backup-imported-{EPOCH_OCT_28}-{HEX32}.tar.gz"
    ) == date(2026, 10, 28)
    assert archive_stamp_date("azerothcore-backup-daily-garbage.tar.gz") is None
    assert archive_stamp_date("notes.txt") is None


def test_archive_stamp_date_imported_epoch_is_read_as_utc():
    # 23:59:59 UTC on the 28th stays on the 28th whatever the host timezone is.
    assert archive_stamp_date(
        f"azerothcore-backup-imported-{1793231999}-{HEX32}.tar.gz"
    ) == date(2026, 10, 28)
    assert archive_stamp_date(
        f"azerothcore-backup-imported-{1793232000}-{HEX32}.tar.gz"
    ) == date(2026, 10, 29)


@pytest.mark.parametrize(
    "name",
    [
        "azerothcore-backup-daily-2026-02-30.tar.gz",  # not a calendar date
        "azerothcore-backup-daily-2026-13-01.tar.gz",
        "azerothcore-backup-manual-2026-02-30T10-00-00.tar.gz",
        "azerothcore-backup-manual-2026-10-27T25-00-00.tar.gz",  # hour 25
        "azerothcore-backup-manual-2026-10-27T21-03.tar.gz",  # seconds missing
        "azerothcore-backup-manual-2026-10-27.tar.gz",  # manual is always timestamped
        "azerothcore-backup-daily-2026-10-27T21-03-51.tar.gz",  # daily is date-only
        "azerothcore-backup-daily-2026-10-2.tar.gz",
        "azerothcore-backup-daily-x2026-10-20.tar.gz",
        "azerothcore-backup-imported-abc-" + HEX32 + ".tar.gz",
        f"azerothcore-backup-imported-{EPOCH_OCT_28}.tar.gz",  # no random suffix
        "azerothcore-backup-imported-" + "9" * 40 + "-" + HEX32 + ".tar.gz",  # absurd epoch
        "azerothcore-backup-daily-2026-10-20/.tar.gz",
    ],
)
def test_archive_stamp_date_rejects_off_shapes(name):
    assert archive_stamp_date(name) is None


def test_archive_stamp_date_accepts_every_non_daily_timestamped_label():
    for label in ("manual", "prerestore", "preclear", "preupgrade", "prerollback"):
        name = f"azerothcore-backup-{label}-2026-10-27T21-03-51.tar.gz"
        assert archive_stamp_date(name) == date(2026, 10, 27), label


# --- names ---


def test_archive_label():
    assert archive_label("azerothcore-backup-daily-2026-10-20.tar.gz") == "daily"
    assert archive_label("azerothcore-backup-preupgrade-2025-01-01T00-00-00.tar.gz") == "preupgrade"
    assert archive_label("notes.txt") is None
    assert archive_label("azerothcore-backup-weekly-2026-10-20.tar.gz") is None
    assert archive_label(None) is None


def test_is_backup_name_accepts_real_names():
    assert is_backup_name("azerothcore-backup-daily-2026-10-20.tar.gz")
    assert is_backup_name("azerothcore-backup-manual-2026-10-27T21-03-51.tar.gz")
    assert is_backup_name(f"azerothcore-backup-imported-{EPOCH_OCT_28}-{HEX32}.tar.gz")


@pytest.mark.parametrize(
    "name",
    [
        None,
        "",
        "notes.txt",
        "azerothcore-backup-daily-a/b.tar.gz",  # _ARCHIVE_RE's (.+) would let this through
        "azerothcore-backup-daily-../x.tar.gz",
        "azerothcore-backup-daily-2026..10.tar.gz",
        "azerothcore-backup-daily-a\\b.tar.gz",
        "azerothcore-backup-daily-2026-10-20\n.tar.gz",
        "azerothcore-backup-daily-2026-10-20.tar.gz\n",
        "azerothcore-backup-daily-a\x00b.tar.gz",
        "azerothcore-backup-daily-a\x7fb.tar.gz",
        "azerothcore-backup-daily-a\tb.tar.gz",
    ],
)
def test_is_backup_name_rejects_unsafe_or_foreign_names(name):
    assert is_backup_name(name) is False
    assert archive_label(name) is None


# --- candidates ---


def test_select_candidates_filters_labels_age_and_keeps_protected():
    local = [
        _info("azerothcore-backup-daily-2026-10-28.tar.gz", "2026-10-28T03:00"),
        _info("azerothcore-backup-daily-2026-10-20.tar.gz", "2026-10-20T03:00"),
        _info("azerothcore-backup-manual-2026-10-28T10-00-00.tar.gz", "2026-10-28T10:00"),
        _info("azerothcore-backup-preupgrade-2025-05-01T09-00-00.tar.gz", "2025-05-01T09:00"),
    ]
    cfg = CloudConfig(labels=("daily", "preupgrade"))
    assert [c.filename for c in select_candidates(local, cfg, TODAY)] == [
        "azerothcore-backup-daily-2026-10-28.tar.gz",
        "azerothcore-backup-preupgrade-2025-05-01T09-00-00.tar.gz",
    ]


def test_select_candidates_age_boundary_matches_retention():
    # retention 7: a stamp 7 days old is still wanted, 8 days old is not
    # (and plan_retention trashes 8 but not 7).
    local = [
        _info("azerothcore-backup-daily-2026-10-21.tar.gz", "2026-10-21T03:00"),
        _info("azerothcore-backup-daily-2026-10-20.tar.gz", "2026-10-20T03:00"),
    ]
    cfg = CloudConfig(retention_days=7)
    assert [c.filename for c in select_candidates(local, cfg, TODAY)] == [
        "azerothcore-backup-daily-2026-10-21.tar.gz"
    ]


def test_select_candidates_does_not_upload_protected_label_that_is_not_selected():
    local = [_info("azerothcore-backup-preupgrade-2025-05-01T09-00-00.tar.gz", "2025-05-01T09:00")]
    assert select_candidates(local, CloudConfig(labels=("daily",)), TODAY) == []


def test_select_candidates_sorts_newest_first_by_created():
    older = _info("azerothcore-backup-manual-2026-10-27T10-00-00.tar.gz", "2026-10-27T10:00")
    newer = _info("azerothcore-backup-manual-2026-10-28T10-00-00.tar.gz", "2026-10-28T10:00")
    prot = _info("azerothcore-backup-prerollback-2025-01-01T00-00-00.tar.gz", "2025-01-01T00:00")
    got = select_candidates([prot, older, newer], CloudConfig(), TODAY)
    assert [c.filename for c in got] == [newer.filename, older.filename, prot.filename]


def test_select_candidates_falls_back_to_mtime_when_stamp_unparsable():
    fresh = _info("azerothcore-backup-daily-garbage.tar.gz", "2026-10-27T03:00")
    stale = _info("azerothcore-backup-manual-oops.tar.gz", "2026-01-01T03:00")
    assert select_candidates([fresh, stale], CloudConfig(), TODAY) == [fresh]


def test_select_candidates_prefers_filename_stamp_over_mtime():
    # mtime is recent (e.g. the file was copied), but the name says it is old
    odd = _info("azerothcore-backup-daily-2026-01-01.tar.gz", "2026-10-28T03:00")
    assert select_candidates([odd], CloudConfig(), TODAY) == []


def test_select_candidates_imported_uses_epoch_date():
    old = _info(f"azerothcore-backup-imported-{EPOCH_OCT_28 - 30 * 86400}-{HEX32}.tar.gz", "2026-10-28T03:00")
    new = _info(f"azerothcore-backup-imported-{EPOCH_OCT_28}-{HEX32}.tar.gz", "2026-10-28T03:00")
    assert select_candidates([old, new], CloudConfig(), TODAY) == [new]


def test_select_candidates_ignores_unsafe_names():
    bad = BackupInfo(
        filename="azerothcore-backup-daily-a/b.tar.gz",
        label="daily",
        created=dt.datetime(2026, 10, 28, tzinfo=dt.timezone.utc),
        size_bytes=1,
    )
    assert select_candidates([bad], CloudConfig(), TODAY) == []


# --- classification ---


def test_classify_present_conflict_missing():
    c = _info("azerothcore-backup-daily-2026-10-28.tar.gz", "2026-10-28T03:00", size=10)
    n = c.filename
    assert classify(c, {}, "ab") == "missing"
    assert classify(c, {n: RemoteEntry(n, "file", "u", 10, "ab")}, "ab") == "present"
    assert classify(c, {n: RemoteEntry(n, "file", "u", 10, "cd")}, "ab") == "conflict"
    assert classify(c, {n: RemoteEntry(n, "file", "u", 10, None)}, "ab") == "present"
    assert classify(c, {n: RemoteEntry(n, "file", "u", 11, None)}, "ab") == "conflict"


def test_classify_sha1_decides_over_size_and_ignores_hex_case():
    c = _info("azerothcore-backup-daily-2026-10-28.tar.gz", "2026-10-28T03:00", size=10)
    n = c.filename
    # same size but a different digest is a different file
    assert classify(c, {n: RemoteEntry(n, "file", "u", 10, "cd")}, "ab") == "conflict"
    # equal digest wins even if the listing's size is off
    assert classify(c, {n: RemoteEntry(n, "file", "u", 99, "ab")}, "ab") == "present"
    assert classify(c, {n: RemoteEntry(n, "file", "u", 10, "AB")}, "ab") == "present"


def test_classify_unknown_remote_size_without_digest_is_conflict():
    c = _info("azerothcore-backup-daily-2026-10-28.tar.gz", "2026-10-28T03:00", size=10)
    n = c.filename
    assert classify(c, {n: RemoteEntry(n, "file", "u", None, None)}, "ab") == "conflict"


def test_classify_a_folder_with_the_name_is_a_conflict():
    c = _info("azerothcore-backup-daily-2026-10-28.tar.gz", "2026-10-28T03:00", size=10)
    n = c.filename
    assert classify(c, {n: _folder(n)}, "ab") == "conflict"


# --- retention ---


def test_plan_retention_uses_backup_sh_cutoff():
    entries = [
        _file("azerothcore-backup-daily-2026-10-20.tar.gz"),
        _file("azerothcore-backup-daily-2026-10-21.tar.gz"),
        _file("azerothcore-backup-daily-2026-10-28.tar.gz"),
    ]
    assert plan_retention(entries, CloudConfig(retention_days=7), TODAY) == [
        "azerothcore-backup-daily-2026-10-20.tar.gz"
    ]


def test_plan_retention_keeps_protected_labels_forever():
    entries = [
        _file("azerothcore-backup-preupgrade-2025-01-01T00-00-00.tar.gz"),
        _file("azerothcore-backup-prerollback-2025-01-01T00-00-00.tar.gz"),
        _file("azerothcore-backup-daily-2026-10-28.tar.gz"),
    ]
    assert plan_retention(entries, CloudConfig(), TODAY) == []


def test_plan_retention_ignores_folders_undecryptable_and_unparsable():
    entries = [
        _folder("azerothcore-backup-daily-2020-01-01.tar.gz"),
        RemoteEntry(None, "file", "u1", 1, None),
        _file("azerothcore-backup-daily-garbage.tar.gz"),
        _file("holiday.jpg"),
        _file("azerothcore-backup-daily-2026-10-28.tar.gz"),
    ]
    assert plan_retention(entries, CloudConfig(), TODAY) == []


def test_plan_retention_never_trashes_newest_or_today():
    old_only = [
        _file("azerothcore-backup-daily-2026-01-01.tar.gz"),
        _file("azerothcore-backup-daily-2026-01-02.tar.gz"),
    ]
    assert plan_retention(old_only, CloudConfig(), TODAY) == ["azerothcore-backup-daily-2026-01-01.tar.gz"]
    today = [
        _file(f"azerothcore-backup-imported-{EPOCH_OCT_28}-{HEX32}.tar.gz"),
        _file("azerothcore-backup-daily-2026-10-28.tar.gz"),
    ]
    assert plan_retention(today, CloudConfig(retention_days=1), TODAY) == []


def test_plan_retention_sole_old_archive_is_kept():
    only = [_file("azerothcore-backup-daily-2026-01-01.tar.gz")]
    assert plan_retention(only, CloudConfig(), TODAY) == []


def test_plan_retention_a_protected_archive_is_not_the_newest_exemption():
    # The protected file is newer than the old daily, but "newest" is among the
    # non-protected ones, so the only old daily is the exempt one.
    entries = [
        _file("azerothcore-backup-daily-2026-01-01.tar.gz"),
        _file("azerothcore-backup-preupgrade-2026-10-01T00-00-00.tar.gz"),
    ]
    assert plan_retention(entries, CloudConfig(), TODAY) == []


def test_plan_retention_keeps_every_archive_that_shares_the_newest_date():
    # Two old archives from the same (newest) day: neither is clearly "the" newest,
    # so neither is trashed.
    entries = [
        _file("azerothcore-backup-daily-2026-01-02.tar.gz"),
        _file("azerothcore-backup-manual-2026-01-02T10-00-00.tar.gz"),
        _file("azerothcore-backup-daily-2026-01-01.tar.gz"),
    ]
    assert plan_retention(entries, CloudConfig(), TODAY) == ["azerothcore-backup-daily-2026-01-01.tar.gz"]


def test_plan_retention_returns_oldest_first():
    entries = [
        _file("azerothcore-backup-daily-2026-10-02.tar.gz"),
        _file("azerothcore-backup-daily-2026-10-01.tar.gz"),
        _file("azerothcore-backup-manual-2026-09-15T08-00-00.tar.gz"),
        _file("azerothcore-backup-daily-2026-10-28.tar.gz"),
    ]
    assert plan_retention(entries, CloudConfig(retention_days=7), TODAY) == [
        "azerothcore-backup-manual-2026-09-15T08-00-00.tar.gz",
        "azerothcore-backup-daily-2026-10-01.tar.gz",
        "azerothcore-backup-daily-2026-10-02.tar.gz",
    ]


def test_plan_retention_ignores_names_that_are_not_safe_backup_names():
    entries = [
        _file("azerothcore-backup-daily-2020-01-01/../x.tar.gz"),
        _file("azerothcore-backup-daily-2020-01-01\\x.tar.gz"),
        _file("azerothcore-backup-daily-2026-10-28.tar.gz"),
    ]
    assert plan_retention(entries, CloudConfig(), TODAY) == []


def test_plan_retention_skips_a_name_listed_more_than_once():
    # <folder>/<name> would be ambiguous, so the duplicated name is left alone.
    dup = "azerothcore-backup-daily-2020-01-01.tar.gz"
    entries = [_file(dup), _folder(dup), _file("azerothcore-backup-daily-2026-10-28.tar.gz")]
    assert plan_retention(entries, CloudConfig(), TODAY) == []
    entries = [_file(dup), _file(dup), _file("azerothcore-backup-daily-2026-10-28.tar.gz")]
    assert plan_retention(entries, CloudConfig(), TODAY) == []


def test_plan_retention_a_duplicated_newest_name_still_protects_itself():
    # The duplicated name is never trashed, and it still counts as the newest archive.
    dup = "azerothcore-backup-daily-2026-01-02.tar.gz"
    entries = [_file(dup), _file(dup), _file("azerothcore-backup-daily-2026-01-01.tar.gz")]
    assert plan_retention(entries, CloudConfig(), TODAY) == ["azerothcore-backup-daily-2026-01-01.tar.gz"]


def test_plan_retention_honours_retention_days_setting():
    entries = [
        _file("azerothcore-backup-daily-2026-10-01.tar.gz"),
        _file("azerothcore-backup-daily-2026-10-28.tar.gz"),
    ]
    assert plan_retention(entries, CloudConfig(retention_days=30), TODAY) == []
    assert plan_retention(entries, CloudConfig(retention_days=26), TODAY) == [
        "azerothcore-backup-daily-2026-10-01.tar.gz"
    ]
    assert plan_retention(entries, CloudConfig(retention_days=27), TODAY) == []
