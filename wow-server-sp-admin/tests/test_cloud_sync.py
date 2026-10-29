"""One upload-and-retention run (spec 6.6), against an in-memory Drive folder.

``FakeCli`` has the wrapper's method names, holds the folder in memory and records
every call (with its folder argument); ``FakeVault`` counts enter/exit and tells
the fake CLI whether it is being called while the vault is open. Local archives
are real files under ``tmp_path`` so the real ``list_backups``,
``resolve_backup_archive`` and ``file_sha1`` run.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import os
from contextlib import contextmanager
from datetime import date
from pathlib import Path

import pytest

from app.services import cloud_sync
from app.services.cloud_cli import CloudCliError, UploadSummary
from app.services.cloud_config import CloudConfig, RemoteFile
from app.services.cloud_plan import RemoteEntry
from app.services.cloud_sync import RunOutcome, file_sha1, run_sync
from app.services.cloud_vault import VaultError

FOLDER = "/my-files/WoW Backups"
TODAY = date(2026, 10, 28)

AUTH = "You need to login first"


def daily(day: str) -> str:
    return f"azerothcore-backup-daily-{day}.tar.gz"


NEW = daily("2026-10-28")
D27 = daily("2026-10-27")
D26 = daily("2026-10-26")
D20 = daily("2026-10-20")  # 8 days old on TODAY: past 7-day retention
D19 = daily("2026-10-19")
PREUPGRADE = "azerothcore-backup-preupgrade-2026-09-01T10-00-00.tar.gz"  # old, but protected


def sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


class FakeVault:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.enters = 0
        self.exits = 0
        self.inside = False

    @contextmanager
    def unlocked(self):
        if self.error is not None:
            raise self.error
        self.enters += 1
        self.inside = True
        try:
            yield
        finally:
            self.exits += 1
            self.inside = False


class FakeCli:
    """Same method names as ``ProtonDriveCli``; in-memory folder; records every call."""

    def __init__(self, vault: FakeVault) -> None:
        self.vault = vault
        self.calls: list[tuple] = []  # (op, folder, *extra)
        self.entries: list[RemoteEntry] = []
        self.info_node: RemoteEntry = RemoteEntry("WoW Backups", "folder", "u-folder", None, None)
        self.info_error: Exception | None = None
        self.list_errors: dict[int, Exception] = {}  # 1-based list call number -> error
        self.upload_errors: dict[str, Exception] = {}
        self.upload_stores: dict[str, bytes | None] = {}  # name -> bytes to store; None = store nothing
        self.store_despite_error: set[str] = set()  # the upload raises, but the file did land
        self.trash_errors: dict[str, Exception] = {}
        self.trash_noop: set[str] = set()  # trash "succeeds" but the file stays in the folder
        self.outside_vault_calls = 0

    def _enter(self, op: str, folder: str, *extra) -> None:
        self.calls.append((op, folder, *extra))
        if not self.vault.inside:
            self.outside_vault_calls += 1

    # --- the wrapper's methods ---

    def info(self, folder: str) -> RemoteEntry:
        self._enter("info", folder)
        if self.info_error is not None:
            raise self.info_error
        return self.info_node

    def list_folder(self, folder: str) -> list[RemoteEntry]:
        self._enter("list", folder)
        number = sum(1 for call in self.calls if call[0] == "list")
        if number in self.list_errors:
            raise self.list_errors[number]
        return list(self.entries)

    def upload(self, archive: Path, folder: str) -> UploadSummary:
        self._enter("upload", folder, archive.name)
        data = self.upload_stores.get(archive.name, archive.read_bytes())
        error = self.upload_errors.get(archive.name)
        if data is not None and (error is None or archive.name in self.store_despite_error):
            self.entries.append(RemoteEntry(archive.name, "file", "u-" + archive.name, len(data), sha1(data)))
        if error is not None:
            raise error
        # Exit 0 with an all-zero summary proves nothing; only the re-listing counts.
        return UploadSummary(0, 0, 0, [])

    def trash(self, folder: str, name: str) -> None:
        self._enter("trash", folder, name)
        if name in self.trash_errors:
            raise self.trash_errors[name]
        if name not in self.trash_noop:
            self.entries = [e for e in self.entries if e.name != name]

    # --- views for assertions ---

    def names(self, op: str) -> list[str]:
        return [call[2] for call in self.calls if call[0] == op]

    def count(self, op: str) -> int:
        return sum(1 for call in self.calls if call[0] == op)


class Rig:
    def __init__(self, tmp_path: Path) -> None:
        self.backups = tmp_path / "backups"
        self.backups.mkdir()
        self.vault = FakeVault()
        self.cli = FakeCli(self.vault)
        self.cfg = CloudConfig(enabled=True, folder=FOLDER)

    def local(self, name: str, iso: str, data: bytes | None = None) -> bytes:
        data = name.encode() if data is None else data
        path = self.backups / name
        path.write_bytes(data)
        stamp = dt.datetime.fromisoformat(iso).replace(tzinfo=dt.timezone.utc).timestamp()
        os.utime(path, (stamp, stamp))
        return data

    def remote(self, name: str, data: bytes | None = None) -> None:
        data = name.encode() if data is None else data
        self.cli.entries.append(RemoteEntry(name, "file", "u-" + name, len(data), sha1(data)))

    def run(self, **overrides) -> RunOutcome:
        args = dict(
            cli=self.cli, vault=self.vault, cfg=self.cfg, backups_dir=self.backups, today=TODAY,
        )
        args.update(overrides)
        return run_sync(**args)


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


# --- file_sha1 ---


def test_file_sha1_matches_hashlib_across_chunks(tmp_path):
    data = os.urandom(2 * 1024 * 1024 + 12345)  # more than two 1 MiB chunks
    path = tmp_path / "blob"
    path.write_bytes(data)
    assert file_sha1(path) == hashlib.sha1(data).hexdigest()
    empty = tmp_path / "empty"
    empty.write_bytes(b"")
    assert file_sha1(empty) == hashlib.sha1(b"").hexdigest()


# --- the happy paths ---


def test_uploads_missing_newest_first_then_applies_retention(rig):
    rig.local(NEW, "2026-10-28T03:00:00")
    rig.local(D27, "2026-10-27T03:00:00")
    rig.remote(D27)  # same content as the local copy
    rig.remote(D19)  # past retention

    out = rig.run()

    assert rig.cli.names("upload") == [NEW]
    assert rig.cli.names("trash") == [D19]
    assert out.result == "ok"
    assert out.error is None
    assert out.connection == "connected"
    assert out.folder_status == "ok"
    assert out.uploaded == [NEW]
    assert out.conflicts == [] and out.failed == [] and out.trashed == [D19]
    # What the Drive holds at the end: the trashed file is gone from the view.
    assert sorted(f.name for f in out.remote) == [D27, NEW]
    assert out.listed is True


def test_several_missing_files_go_up_newest_first_with_one_relist(rig):
    rig.local(D26, "2026-10-26T03:00:00")
    rig.local(NEW, "2026-10-28T03:00:00")
    rig.local(D27, "2026-10-27T03:00:00")

    out = rig.run()

    assert rig.cli.names("upload") == [NEW, D27, D26]
    assert out.uploaded == [NEW, D27, D26]
    assert rig.cli.count("list") == 2  # before and after the uploads
    assert out.result == "ok"


def test_nothing_to_upload_means_a_single_listing(rig):
    rig.local(NEW, "2026-10-28T03:00:00")
    rig.remote(NEW)

    out = rig.run()

    assert rig.cli.count("list") == 1  # no upload attempted: the first listing is final
    assert rig.cli.names("upload") == []
    assert out.result == "ok"
    assert out.uploaded == []
    assert [f.name for f in out.remote] == [NEW]


def test_already_present_old_file_is_still_trashed_by_retention(rig):
    rig.local(NEW, "2026-10-28T03:00:00")
    rig.remote(NEW)
    rig.remote(D20)
    rig.remote(D27)

    out = rig.run()

    assert rig.cli.names("trash") == [D20]
    assert out.trashed == [D20]


def test_protected_labels_are_uploaded_at_any_age_and_never_trashed(rig):
    rig.local(NEW, "2026-10-28T03:00:00")
    rig.local(PREUPGRADE, "2026-09-01T10:00:00")
    rig.remote(
        "azerothcore-backup-prerollback-2026-01-01T00-00-00.tar.gz"
    )  # ancient, protected

    out = rig.run()

    assert rig.cli.names("upload") == [NEW, PREUPGRADE]
    assert rig.cli.names("trash") == []
    assert out.result == "ok"


def test_no_candidates_means_no_retention(rig):
    # R-5: nothing to confirm, so retention must not run even though D19 is trashable.
    rig.remote(D19)
    rig.remote(D27)

    out = rig.run()

    assert rig.cli.names("upload") == []
    assert rig.cli.names("trash") == []
    assert out.result == "ok"
    assert out.error is None
    assert sorted(f.name for f in out.remote) == [D19, D27]


# --- conflicts, failures, and the retention gate ---


def test_conflict_is_reported_never_uploaded_and_does_not_block_retention_when_newest_present(rig):
    rig.local(NEW, "2026-10-28T03:00:00")
    rig.local(D26, "2026-10-26T03:00:00")
    rig.remote(NEW)
    rig.remote(D26, b"somebody else's file with this name")
    rig.remote(D19)

    out = rig.run()

    assert out.conflicts == [D26]
    assert D26 not in rig.cli.names("upload")
    assert rig.cli.names("trash") == [D19]  # retention still runs
    assert out.result == "warning"
    assert out.failed == []


def test_conflict_on_the_newest_blocks_retention(rig):
    rig.local(NEW, "2026-10-28T03:00:00")
    rig.remote(NEW, b"a different file with the newest name")
    rig.remote(D19)

    out = rig.run()

    assert out.conflicts == [NEW]
    assert rig.cli.names("upload") == []
    assert rig.cli.names("trash") == []
    assert out.result == "warning"


def test_retention_skipped_when_newest_candidate_not_confirmed(rig):
    rig.local(NEW, "2026-10-28T03:00:00")
    rig.local(D27, "2026-10-27T03:00:00")
    rig.remote(D19)
    rig.cli.upload_errors[NEW] = CloudCliError("failed", "Storage quota exceeded")

    out = rig.run()

    assert out.result == "partial"
    assert out.failed == [NEW]
    assert out.failure_details[NEW] == "Storage quota exceeded"
    assert rig.cli.names("trash") == []
    assert D27 in out.uploaded  # one file's problem does not stop the others
    assert out.error is None


def test_a_failed_older_file_does_not_block_retention(rig):
    rig.local(NEW, "2026-10-28T03:00:00")
    rig.local(D27, "2026-10-27T03:00:00")
    rig.remote(D19)
    rig.cli.upload_errors[D27] = CloudCliError("timeout", "Proton Drive did not answer within 60 minute(s).")

    out = rig.run()

    assert out.result == "partial"
    assert out.failed == [D27]
    assert out.uploaded == [NEW]
    assert rig.cli.names("trash") == [D19]


def test_upload_that_exits_clean_but_leaves_no_file_is_failed(rig):
    rig.local(NEW, "2026-10-28T03:00:00")
    rig.remote(D19)
    rig.cli.upload_stores[NEW] = None  # the CLI said "fine" and stored nothing

    out = rig.run()

    assert out.failed == [NEW]
    assert out.uploaded == []
    assert "not in" in out.failure_details[NEW]
    assert rig.cli.names("trash") == []
    assert out.result == "partial"


def test_upload_that_lands_with_other_content_is_a_conflict(rig):
    rig.local(NEW, "2026-10-28T03:00:00")
    rig.cli.upload_stores[NEW] = b"not what we sent"

    out = rig.run()

    assert out.conflicts == [NEW]
    assert out.uploaded == []
    assert out.result == "warning"


def test_failed_upload_that_is_verified_present_afterwards_counts_as_uploaded(rig):
    # A timeout can hide an upload that finished; the re-listing is the judge.
    rig.local(NEW, "2026-10-28T03:00:00")
    rig.cli.upload_errors[NEW] = CloudCliError("timeout", "Proton Drive did not answer within 60 minute(s).")
    rig.cli.store_despite_error.add(NEW)

    out = rig.run()

    assert out.uploaded == [NEW]
    assert out.failed == []
    assert out.result == "ok"


def test_unreadable_local_archive_is_failed_not_fatal(rig):
    rig.local(NEW, "2026-10-28T03:00:00")
    rig.local(D27, "2026-10-27T03:00:00")

    def sha1_of(path: Path) -> str:
        if path.name == D27:
            raise PermissionError("root-owned")
        return file_sha1(path)

    out = rig.run(sha1_of=sha1_of)

    assert out.failed == [D27]
    assert "read" in out.failure_details[D27]
    assert rig.cli.names("upload") == [NEW]
    assert out.uploaded == [NEW]
    assert out.result == "partial"


def test_unreadable_newest_archive_blocks_retention(rig):
    rig.local(NEW, "2026-10-28T03:00:00")
    rig.remote(D19)

    def sha1_of(path: Path) -> str:
        raise PermissionError("root-owned")

    out = rig.run(sha1_of=sha1_of)

    assert out.failed == [NEW]
    assert rig.cli.names("trash") == []
    assert rig.cli.names("upload") == []


def test_local_path_that_is_not_a_regular_file_is_failed(rig, monkeypatch):
    rig.local(NEW, "2026-10-28T03:00:00")
    rig.local(D27, "2026-10-27T03:00:00")
    real = cloud_sync.resolve_backup_archive
    monkeypatch.setattr(
        cloud_sync,
        "resolve_backup_archive",
        lambda *, backups_dir, archive_name: None if archive_name == D27 else real(
            backups_dir=backups_dir, archive_name=archive_name
        ),
    )

    out = rig.run()

    assert out.failed == [D27]
    assert "regular file" in out.failure_details[D27]
    assert rig.cli.names("upload") == [NEW]


def test_refused_upload_is_a_per_file_failure(rig):
    rig.local(NEW, "2026-10-28T03:00:00")
    rig.local(D27, "2026-10-27T03:00:00")
    rig.cli.upload_errors[NEW] = ValueError("That file is not a backup archive in the backups folder.")

    out = rig.run()

    assert out.failed == [NEW]
    assert out.uploaded == [D27]
    assert out.result == "partial"
    assert rig.cli.names("trash") == []


def test_duplicate_remote_names_with_different_content_are_a_conflict(rig):
    rig.local(NEW, "2026-10-28T03:00:00")
    rig.remote(NEW)
    rig.remote(NEW, b"another file with the same name")

    out = rig.run()

    assert out.conflicts == [NEW]
    assert rig.cli.names("upload") == []


def test_failures_outrank_conflicts_in_the_result(rig):
    rig.local(NEW, "2026-10-28T03:00:00")
    rig.local(D27, "2026-10-27T03:00:00")
    rig.remote(D27, b"other content")
    rig.cli.upload_errors[NEW] = CloudCliError("failed", "Storage quota exceeded")

    out = rig.run()

    assert out.conflicts == [D27] and out.failed == [NEW]
    assert out.result == "partial"


# --- trash problems ---


def test_failed_trash_is_partial_and_does_not_stop_the_other_trashes(rig):
    rig.local(NEW, "2026-10-28T03:00:00")
    rig.remote(NEW)
    rig.remote(D20)
    rig.remote(D19)
    rig.cli.trash_errors[D19] = CloudCliError("failed", "Proton Drive did not move it to the trash.")

    out = rig.run()

    assert rig.cli.names("trash") == [D19, D20]  # oldest first
    assert out.trashed == [D20]
    assert out.result == "partial"
    assert out.failed == []
    assert out.error is not None and "trash" in out.error.lower()
    assert f"{D19}: Proton Drive did not move it to the trash." in out.error  # names the file
    assert D19 in [f.name for f in out.remote]  # still in the Drive
    assert D20 not in [f.name for f in out.remote]
    assert rig.cli.count("list") == 2  # the folder is checked again after the trash calls


def test_refused_trash_is_recorded_like_a_failed_one(rig):
    rig.local(NEW, "2026-10-28T03:00:00")
    rig.remote(NEW)
    rig.remote(D19)
    rig.remote(D20)
    rig.cli.trash_errors[D19] = ValueError("Only backup archive files can be moved to the Drive trash.")

    out = rig.run()

    assert out.trashed == [D20]
    assert out.result == "partial"
    assert out.error is not None and D19 in out.error


def test_trash_reported_ok_but_still_listed_is_not_trashed(rig):
    rig.local(NEW, "2026-10-28T03:00:00")
    rig.remote(NEW)
    rig.remote(D19)
    rig.remote(D20)
    rig.cli.trash_noop.add(D19)  # exit 0, ok: true -- and the file is still there

    out = rig.run()

    assert rig.cli.names("trash") == [D19, D20]
    assert out.trashed == [D20]
    assert out.result == "partial"
    assert out.error is not None and D19 in out.error and D20 not in out.error
    assert "still in the Drive folder" in out.error
    assert out.failed == []
    # `remote` comes from the listing taken after the trash calls.
    assert sorted(f.name for f in out.remote) == [D19, NEW]
    assert rig.cli.count("list") == 2


def test_post_trash_listing_failure_is_partial_and_trashed_keeps_the_reported_names(rig):
    rig.local(NEW, "2026-10-28T03:00:00")
    rig.remote(NEW)
    rig.remote(D19)
    rig.remote(D20)
    rig.cli.list_errors[2] = CloudCliError("failed", "Proton Drive reported an error.")  # the list after trashing

    out = rig.run()

    assert out.trashed == [D19, D20]  # as reported by the CLI; nothing confirmed them
    assert out.result == "partial"
    assert out.error is not None
    assert "not confirmed" in out.error and "Proton Drive reported an error." in out.error
    assert out.uploaded == [] and out.failed == []
    # No listing after the trash calls: the earlier one, minus what the CLI said it moved.
    assert [f.name for f in out.remote] == [NEW]
    assert out.listed is True


def test_auth_error_on_the_post_trash_listing_aborts_the_run(rig):
    rig.local(NEW, "2026-10-28T03:00:00")
    rig.remote(NEW)
    rig.remote(D19)
    rig.cli.list_errors[2] = CloudCliError("auth", AUTH)

    out = rig.run()

    assert out.result == "error"
    assert out.connection == "disconnected"
    assert out.error == AUTH
    assert out.trashed == [D19]  # reported before the session went away
    assert [f.name for f in out.remote] == [NEW]


def test_auth_error_while_trashing_aborts_the_run(rig):
    rig.local(NEW, "2026-10-28T03:00:00")
    rig.remote(NEW)
    rig.remote(D19)
    rig.remote(D20)
    rig.cli.trash_errors[D19] = CloudCliError("auth", AUTH)

    out = rig.run()

    assert rig.cli.names("trash") == [D19]  # nothing after the auth error
    assert out.result == "error"
    assert out.connection == "disconnected"
    assert out.error == AUTH


# --- aborted runs ---


def test_unparsable_listing_aborts_before_any_trash(rig):
    rig.local(NEW, "2026-10-28T03:00:00")
    rig.cli.list_errors[1] = CloudCliError("parse", "Proton Drive returned an answer the app could not read.")

    out = rig.run()

    assert out.result == "error"
    assert out.error == "Proton Drive returned an answer the app could not read."
    assert rig.cli.names("upload") == [] and rig.cli.names("trash") == []
    assert out.connection == "connected"
    assert out.folder_status == "ok"
    assert out.remote == [] and out.listed is False


def test_relist_failure_after_uploads_is_an_error_and_skips_retention(rig):
    rig.local(NEW, "2026-10-28T03:00:00")
    rig.remote(D19)
    rig.cli.list_errors[2] = CloudCliError("parse", "Proton Drive returned an answer the app could not read.")

    out = rig.run()

    assert rig.cli.names("upload") == [NEW]
    assert rig.cli.names("trash") == []
    assert out.result == "error"
    assert out.uploaded == []  # nothing is claimed that was not verified
    # The first listing did succeed, so the view of the Drive is kept (it predates the upload).
    assert out.listed is True
    assert [f.name for f in out.remote] == [D19]


def test_auth_error_marks_disconnected(rig):
    rig.cli.info_error = CloudCliError("auth", AUTH)

    out = rig.run()

    assert out.result == "error"
    assert out.connection == "disconnected"
    assert out.error == AUTH
    assert out.folder_status is None
    assert [c[0] for c in rig.cli.calls] == ["info"]


def test_auth_error_mid_run_aborts_and_marks_disconnected(rig):
    rig.local(NEW, "2026-10-28T03:00:00")
    rig.local(D27, "2026-10-27T03:00:00")
    rig.remote(D19)
    rig.cli.upload_errors[NEW] = CloudCliError("auth", AUTH)

    out = rig.run()

    assert rig.cli.names("upload") == [NEW]  # the second upload is never attempted
    assert rig.cli.names("trash") == []
    assert out.result == "error"
    assert out.connection == "disconnected"
    assert out.failed == []  # the session ended; the file did not "fail"


def test_auth_error_on_relist_marks_disconnected(rig):
    rig.local(NEW, "2026-10-28T03:00:00")
    rig.cli.list_errors[2] = CloudCliError("auth", AUTH)

    out = rig.run()

    assert out.result == "error"
    assert out.connection == "disconnected"
    assert rig.cli.names("trash") == []


def test_vault_error_marks_vault_error(rig):
    rig.local(NEW, "2026-10-28T03:00:00")
    rig.vault.error = VaultError("Can't unlock the stored session - reconnect.")

    out = rig.run()

    assert out.result == "error"
    assert out.connection == "vault_error"
    assert out.error == "Can't unlock the stored session - reconnect."
    assert rig.cli.calls == []  # no CLI call at all
    assert out.remote == [] and out.uploaded == [] and out.trashed == []


def test_missing_folder_sets_folder_status(rig):
    rig.local(NEW, "2026-10-28T03:00:00")
    rig.cli.info_error = CloudCliError("not_found", "Node not found: WoW Backups")

    out = rig.run()

    assert out.result == "error"
    assert out.folder_status == "missing"
    assert out.connection == "connected"
    assert [c[0] for c in rig.cli.calls] == ["info"]  # nothing else called
    assert out.error and "folder" in out.error.lower()
    assert "Node not found" not in out.error


def test_folder_that_disappears_before_the_listing_is_missing(rig):
    rig.cli.list_errors[1] = CloudCliError("not_found", "Node not found: WoW Backups")

    out = rig.run()

    assert out.result == "error"
    assert out.folder_status == "missing"


def test_info_on_file_sets_not_folder(rig):
    rig.cli.info_node = RemoteEntry("WoW Backups", "file", "u-file", 5, None)

    out = rig.run()

    assert out.result == "error"
    assert out.folder_status == "not_folder"
    assert out.connection == "connected"
    assert [c[0] for c in rig.cli.calls] == ["info"]


def test_other_cli_error_on_info_keeps_the_message_and_changes_no_state(rig):
    rig.cli.info_error = CloudCliError("timeout", "Proton Drive did not answer within 2 minute(s).")

    out = rig.run()

    assert out.result == "error"
    assert out.error == "Proton Drive did not answer within 2 minute(s)."
    assert out.connection == "connected"  # no evidence either way
    assert out.folder_status is None


def test_unreadable_backups_folder_is_an_error_and_nothing_is_uploaded(rig, tmp_path):
    not_a_dir = tmp_path / "backups-is-a-file"
    not_a_dir.write_text("x")

    out = rig.run(backups_dir=not_a_dir)

    assert out.result == "error"
    assert out.error and "backup" in out.error.lower()
    assert rig.cli.names("upload") == [] and rig.cli.names("trash") == []
    assert out.connection == "connected" and out.folder_status == "ok"


def test_invalid_folder_setting_is_an_error_before_the_vault_is_opened(rig):
    rig.cfg = CloudConfig(enabled=True, folder="/etc/passwd")

    out = rig.run()

    assert out.result == "error"
    assert out.error and "folder" in out.error.lower()
    assert rig.vault.enters == 0
    assert rig.cli.calls == []


# --- the vault and the folder ---


def test_vault_locked_again_even_on_exception(rig):
    rig.local(NEW, "2026-10-28T03:00:00")
    rig.cli.list_errors[1] = RuntimeError("a bug, not a CLI problem")

    with pytest.raises(RuntimeError):
        rig.run()

    assert rig.vault.enters == 1
    assert rig.vault.exits == 1


@pytest.mark.parametrize("scenario", ["ok", "auth", "failed_upload"])
def test_vault_is_entered_and_left_once_and_every_call_happens_inside(rig, scenario):
    rig.local(NEW, "2026-10-28T03:00:00")
    rig.remote(D19)
    if scenario == "auth":
        rig.cli.list_errors[2] = CloudCliError("auth", AUTH)
    if scenario == "failed_upload":
        rig.cli.upload_errors[NEW] = CloudCliError("failed", "Storage quota exceeded")

    rig.run()

    assert rig.vault.enters == 1 and rig.vault.exits == 1
    assert rig.cli.outside_vault_calls == 0
    assert rig.vault.inside is False


def test_run_touches_only_current_folder(rig):
    rig.local(NEW, "2026-10-28T03:00:00")
    rig.local(D27, "2026-10-27T03:00:00")
    rig.remote(D19)
    rig.remote(D20)

    rig.run()

    ops = {call[0] for call in rig.cli.calls}
    assert ops == {"info", "list", "upload", "trash"}
    assert all(call[1] == rig.cfg.folder for call in rig.cli.calls)


# --- what the outcome says about the Drive ---


def test_remote_holds_only_plain_backup_files(rig):
    rig.local(NEW, "2026-10-28T03:00:00")
    rig.remote(NEW)
    rig.cli.entries += [
        RemoteEntry("notes.txt", "file", "u1", 3, None),
        RemoteEntry(None, "file", "u2", 3, None),  # a name the CLI could not decrypt
        RemoteEntry(daily("2026-10-25"), "folder", "u3", None, None),  # right name, but a folder
        RemoteEntry("azerothcore-backup-daily-x/../y.tar.gz", "file", "u4", 1, None),
        RemoteEntry(D27, "file", "u5", None, None),  # no size or digest
    ]

    out = rig.run()

    assert out.remote == [
        RemoteFile(NEW, len(NEW), sha1(NEW.encode())),
        RemoteFile(D27, None, None),
    ]


@pytest.mark.parametrize(
    ("uploads", "trashes", "lists"),
    [(False, False, 1), (True, False, 2), (False, True, 2), (True, True, 3)],
)
def test_the_folder_is_listed_again_only_after_uploads_and_after_trash_calls(rig, uploads, trashes, lists):
    rig.local(NEW, "2026-10-28T03:00:00")
    if not uploads:
        rig.remote(NEW)
    if trashes:
        rig.remote(D19)

    out = rig.run()

    assert out.result == "ok"
    assert bool(rig.cli.names("upload")) is uploads
    assert bool(rig.cli.names("trash")) is trashes
    assert rig.cli.count("list") == lists
