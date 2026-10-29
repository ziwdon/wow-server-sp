"""The Proton Drive CLI wrapper: argv shapes, environment, confinement and output parsing.

The real wrapper runs against ``tests/fake_proton_drive.py`` (a stand-in
executable that records its argv and prints canned output), so the argv the app
can produce is asserted exactly as the binary would receive it.
"""

from __future__ import annotations

import dataclasses
import json
import os
import pwd
import subprocess
import sys
from pathlib import Path

import pytest

from app.services import cloud_cli
from app.services.cloud_cli import (
    CliPaths,
    CloudCliError,
    ProtonDriveCli,
    UploadSummary,
    default_binary,
)
from app.services.cloud_plan import RemoteEntry

ARCHIVE = "azerothcore-backup-daily-2026-10-28.tar.gz"
OLD = "azerothcore-backup-daily-2026-10-20.tar.gz"
FOLDER = "/my-files/B"

LISTING = (
    '[\n{"uid":"a","parentUid":"p","name":{"ok":true,"value":"azerothcore-backup-daily-2026-10-28.tar.gz"},'
    '"type":"file","activeRevision":{"claimedSize":115000000,"claimedDigests":{"sha1":"ab12"}}},\n'
    '{"uid":"b","parentUid":"p","name":{"ok":false,"error":"x"},"type":"file"},\n'
    '{"uid":"c","parentUid":"p","name":{"ok":true,"value":"Old"},"type":"folder"}\n]\n'
)
NODE_FOLDER = (
    '{"uid":"f1","parentUid":"p","name":{"ok":true,"value":"WoW Backups"},"type":"folder",'
    '"creationTime":"2026-10-28T10:00:00.000Z","isShared":false}\n'
)
UPLOAD_OK = '{"transferredItems":1,"transferredBytes":5,"skippedItems":0,"failedItems":0,"failures":[]}\n'
UPLOAD_SKIPPED = '{"transferredItems":0,"transferredBytes":0,"skippedItems":1,"failedItems":0,"failures":[]}\n'
UPLOAD_FAILED = (
    '{"transferredItems":0,"transferredBytes":0,"skippedItems":0,"failedItems":1,"failures":'
    '[{"name":"azerothcore-backup-daily-2026-10-28.tar.gz","nodeUid":"u","error":'
    '"ValidationError: Storage quota exceeded"}]}\n'
)
TRASH_OK = '[\n{"ok":true,"uid":"a"}\n]\n'
VERSION_OUT = "Proton Drive CLI drive-cli@0.9.0\nProton Drive SDK 1.2.3\nYou are running the latest version.\n"


# --- fixtures ---


@pytest.fixture
def backups_dir(tmp_path: Path) -> Path:
    d = tmp_path / "backups"
    d.mkdir()
    (d / ARCHIVE).write_bytes(b"x")
    return d


@pytest.fixture
def archive(backups_dir: Path) -> Path:
    return backups_dir / ARCHIVE


@pytest.fixture
def fake_binary(tmp_path: Path) -> Path:
    source = Path(__file__).with_name("fake_proton_drive.py").read_text(encoding="utf-8")
    body = source.split("\n", 1)[1]  # drop the portable shebang, use this interpreter
    binary = tmp_path / "bin" / "proton-drive"
    binary.parent.mkdir()
    binary.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    binary.chmod(0o755)
    return binary


@pytest.fixture
def paths(tmp_path: Path, fake_binary: Path, backups_dir: Path) -> CliPaths:
    return CliPaths(
        binary=fake_binary,
        cache_dir=tmp_path / "cache",
        gnupg_home=tmp_path / "gnupg",
        store_dir=tmp_path / "store",
        backups_dir=backups_dir,
    )


@pytest.fixture
def fake_env(tmp_path: Path) -> dict[str, str]:
    """Variables the fake reads. The wrapper's own env never carries them (it is built fresh)."""
    return {"FAKE_CLI_ARGV_LOG": str(tmp_path / "argv.log")}


@pytest.fixture
def runs() -> list[tuple[list[str], dict]]:
    """Every (argv, kwargs) the wrapper handed to ``run``."""
    return []


@pytest.fixture
def cli(paths: CliPaths, fake_env: dict[str, str], runs: list) -> ProtonDriveCli:
    def run(argv, **kwargs):
        runs.append((list(argv), dict(kwargs)))
        merged = dict(kwargs)
        merged["env"] = {**kwargs["env"], **fake_env}
        return subprocess.run(argv, **merged)

    def popen(argv, **kwargs):
        merged = dict(kwargs)
        merged["env"] = {**kwargs["env"], **fake_env}
        return subprocess.Popen(argv, **merged)

    return ProtonDriveCli(paths, run=run, popen=popen)


@pytest.fixture
def respond(fake_env: dict[str, str]):
    def _respond(stdout: str = "", stderr: str = "", exit: int = 0) -> None:
        fake_env["FAKE_CLI_STDOUT"] = stdout
        fake_env["FAKE_CLI_STDERR"] = stderr
        fake_env["FAKE_CLI_EXIT"] = str(exit)

    return _respond


@pytest.fixture
def argv_log(fake_env: dict[str, str]):
    def _read() -> list[dict]:
        path = Path(fake_env["FAKE_CLI_ARGV_LOG"])
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    return _read


def _stub(paths: CliPaths, stdout: str = "", stderr: str = "", returncode: int = 0) -> ProtonDriveCli:
    """A wrapper whose ``run`` returns a canned result without spawning anything."""

    def run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)

    return ProtonDriveCli(paths, run=run)


# --- listing and node parsing ---


def test_list_folder_argv_env_and_parse(cli, respond, argv_log):
    respond(stdout=LISTING)
    entries = cli.list_folder("/my-files/WoW Backups/")
    assert argv_log()[-1]["argv"] == ["fs", "list", "--json", "/my-files/WoW Backups"]
    assert argv_log()[-1]["PROTON_DRIVE_CREDENTIALS_STORE"] == "pass"
    assert [(e.name, e.type, e.size, e.sha1) for e in entries] == [
        ("azerothcore-backup-daily-2026-10-28.tar.gz", "file", 115000000, "ab12"),
        (None, "file", None, None),
        ("Old", "folder", None, None),
    ]
    assert all(isinstance(e, RemoteEntry) for e in entries)
    assert [e.uid for e in entries] == ["a", "b", "c"]


def test_list_folder_tolerates_entries_without_revision(cli, respond):
    respond(
        stdout='[\n{"uid":"a","name":{"ok":true,"value":"f.tar.gz"},"type":"file"},\n'
        '{"uid":"b","name":{"ok":true,"value":"g.tar.gz"},"type":"file","activeRevision":{}},\n'
        '{"uid":"c","name":{"ok":true,"value":"h.tar.gz"},"type":"file","activeRevision":null}\n]\n'
    )
    entries = cli.list_folder(FOLDER)
    assert [(e.name, e.size, e.sha1) for e in entries] == [
        ("f.tar.gz", None, None),
        ("g.tar.gz", None, None),
        ("h.tar.gz", None, None),
    ]


def test_list_folder_empty_folder(cli, respond):
    respond(stdout="[\n\n]\n")
    assert cli.list_folder(FOLDER) == []


def test_list_folder_odd_values_become_none_not_a_crash(cli, respond):
    respond(
        stdout=json.dumps(
            [
                {"uid": "a", "name": {"ok": True, "value": "a"}, "type": "file",
                 "activeRevision": {"claimedSize": "5", "claimedDigests": {"sha1": 7}}},
                {"uid": "b", "name": {"ok": True, "value": "b"}, "type": "file",
                 "activeRevision": {"claimedSize": True, "claimedDigests": "nope"}},
                {"uid": "c", "name": {"ok": True, "value": "c"}, "type": "file",
                 "activeRevision": {"claimedSize": 1.5, "claimedDigests": {}}},
                {"uid": "d", "name": {"ok": True, "value": 12}, "type": "file"},
                {"uid": "e", "name": "plain", "type": "file"},
                {"uid": "f", "name": {"ok": True}, "type": "file"},
                {"uid": "g", "name": {"ok": True, "value": "g"}},
                {"name": {"ok": True, "value": "h"}, "type": 3},
            ]
        )
    )
    entries = cli.list_folder(FOLDER)
    assert [(e.name, e.size, e.sha1) for e in entries[:3]] == [("a", None, None), ("b", None, None), ("c", None, None)]
    assert [e.name for e in entries[3:6]] == [None, None, None]
    # A missing/odd type is never "file", so nothing downstream treats it as trashable.
    assert [e.type for e in entries[6:]] == ["unknown", "unknown"]
    assert entries[7].uid == ""


@pytest.mark.parametrize(
    "stdout",
    [
        '[\n{"uid":"a"',  # truncated: an error part-way through the stream
        "",
        "not json",
        '{"uid":"a"}',  # an object where a list is expected
        "[1, 2]",  # list items that are not nodes
        "[null]",
        '[\n{"uid":"a","name":{"ok":true,"value":"x"},"type":"file"}\n]\ntrailing',
    ],
)
def test_malformed_listing_is_a_parse_error(cli, respond, stdout):
    respond(stdout=stdout)
    with pytest.raises(CloudCliError) as exc:
        cli.list_folder(FOLDER)
    assert exc.value.kind == "parse"


def test_info_argv_and_result(cli, respond, argv_log):
    respond(stdout=NODE_FOLDER)
    entry = cli.info("/my-files/WoW Backups")
    assert argv_log()[-1]["argv"] == ["fs", "info", "--json", "/my-files/WoW Backups"]
    assert (entry.name, entry.type, entry.uid, entry.size, entry.sha1) == ("WoW Backups", "folder", "f1", None, None)


@pytest.mark.parametrize("stdout", ["", "[]", "not json", "null"])
def test_info_non_object_is_a_parse_error(cli, respond, stdout):
    respond(stdout=stdout)
    with pytest.raises(CloudCliError) as exc:
        cli.info(FOLDER)
    assert exc.value.kind == "parse"


def test_create_folder_argv(cli, respond, argv_log):
    respond(stdout=NODE_FOLDER)
    entry = cli.create_folder("/my-files/WoW Backups/")
    assert argv_log()[-1]["argv"] == ["fs", "create-folder", "--json", "/my-files", "WoW Backups"]
    assert (entry.name, entry.type) == ("WoW Backups", "folder")


def test_create_folder_nested_parent(cli, respond, argv_log):
    respond(stdout=NODE_FOLDER)
    cli.create_folder("/my-files/A/B")
    assert argv_log()[-1]["argv"] == ["fs", "create-folder", "--json", "/my-files/A", "B"]


def test_create_folder_refuses_a_leaf_the_cli_would_read_as_an_option(cli, argv_log):
    with pytest.raises(ValueError):
        cli.create_folder("/my-files/-f")
    assert argv_log() == []


def test_create_folder_non_object_is_a_parse_error(cli, respond):
    respond(stdout="[]")
    with pytest.raises(CloudCliError) as exc:
        cli.create_folder(FOLDER)
    assert exc.value.kind == "parse"


# --- upload ---


def test_upload_argv_and_summary(cli, archive, respond, argv_log):
    respond(stdout=UPLOAD_OK)
    summary = cli.upload(archive, "/my-files/B/")
    assert argv_log()[-1]["argv"] == ["fs", "upload", "--json", "-f", "skip", "-t", str(archive), "/my-files/B"]
    assert summary == UploadSummary(transferred=1, skipped=0, failed=0, failures=[])


def test_upload_reports_skipped(cli, archive, respond):
    respond(stdout=UPLOAD_SKIPPED)
    assert cli.upload(archive, FOLDER) == UploadSummary(transferred=0, skipped=1, failed=0, failures=[])


def test_upload_failure_raises_with_failure_text(cli, archive, respond):
    respond(stdout=UPLOAD_FAILED, stderr="1 item(s) failed to upload\n", exit=1)
    with pytest.raises(CloudCliError) as exc:
        cli.upload(archive, FOLDER)
    assert exc.value.kind == "failed"
    assert "Storage quota exceeded" in exc.value.message
    assert "Storage quota exceeded" in str(exc.value)


def test_upload_failed_items_raise_even_when_the_exit_status_is_zero(cli, archive, respond):
    respond(stdout=UPLOAD_FAILED, exit=0)
    with pytest.raises(CloudCliError) as exc:
        cli.upload(archive, FOLDER)
    assert exc.value.kind == "failed"


def test_upload_failure_without_error_text_still_fails(cli, archive, respond):
    respond(
        stdout='{"transferredItems":0,"transferredBytes":0,"skippedItems":0,"failedItems":2,"failures":[]}',
        exit=1,
    )
    with pytest.raises(CloudCliError) as exc:
        cli.upload(archive, FOLDER)
    assert exc.value.kind == "failed"
    assert exc.value.message


def test_upload_nonzero_exit_without_failures_uses_stderr(cli, archive, respond):
    respond(stdout=UPLOAD_OK, stderr="something broke\n", exit=1)
    with pytest.raises(CloudCliError) as exc:
        cli.upload(archive, FOLDER)
    assert exc.value.kind == "failed"
    assert exc.value.message == "something broke"


def test_upload_missing_remote_folder_is_not_found(cli, archive, respond):
    respond(stderr="Node not found: B\n", exit=1)
    with pytest.raises(CloudCliError) as exc:
        cli.upload(archive, FOLDER)
    assert exc.value.kind == "not_found"


@pytest.mark.parametrize(
    "stdout",
    [
        "",
        "not json",
        "[]",
        '{"transferredItems":1}',
        '{"transferredItems":"1","transferredBytes":5,"skippedItems":0,"failedItems":0,"failures":[]}',
        '{"transferredItems":true,"transferredBytes":5,"skippedItems":0,"failedItems":0,"failures":[]}',
        '{"transferredItems":1,"transferredBytes":5,"skippedItems":0,"failedItems":0,"failures":"x"}',
    ],
)
def test_upload_unreadable_summary_is_a_parse_error(cli, archive, respond, stdout):
    respond(stdout=stdout)
    with pytest.raises(CloudCliError) as exc:
        cli.upload(archive, FOLDER)
    assert exc.value.kind == "parse"


def test_upload_refuses_paths_outside_backups_dir(cli, tmp_path, argv_log):
    outside = tmp_path / ARCHIVE
    outside.write_bytes(b"x")
    with pytest.raises(ValueError):
        cli.upload(tmp_path / "azerothcore-backup-daily-2026-10-28.tar.gz", "/my-files/B")
    with pytest.raises(ValueError):
        cli.upload(outside, "/my-files/B")
    assert argv_log() == []


def test_upload_refuses_everything_but_a_plain_archive_in_backups_dir(cli, backups_dir, archive, tmp_path, argv_log):
    other = tmp_path / "other"
    other.mkdir()
    (other / ARCHIVE).write_bytes(b"x")
    (backups_dir / "notes.txt").write_text("hi")
    link = backups_dir / "azerothcore-backup-manual-2026-10-28.tar.gz"
    link.symlink_to(archive)
    bad = [
        other / ARCHIVE,  # same name, other directory
        backups_dir / "notes.txt",  # not a backup name
        link,  # symlink inside backups_dir
        backups_dir / "azerothcore-backup-daily-2026-01-01.tar.gz",  # not there
        Path(str(backups_dir) + "/../backups/" + ARCHIVE),  # not the canonical spelling
        Path(ARCHIVE),  # relative
        backups_dir,  # a directory
    ]
    for path in bad:
        with pytest.raises(ValueError):
            cli.upload(path, FOLDER)
    assert argv_log() == []


@pytest.mark.parametrize("folder", ["", "/my-files", "/my-files/", "/elsewhere/B", "/my-files/../x", "/my-files/a//b"])
def test_upload_refuses_a_bad_folder(cli, archive, argv_log, folder):
    with pytest.raises(ValueError):
        cli.upload(archive, folder)
    assert argv_log() == []


# --- trash ---


def test_trash_argv_and_ok_result(cli, respond, argv_log):
    respond(stdout=TRASH_OK)
    assert cli.trash("/my-files/B/", OLD) is None
    assert argv_log()[-1]["argv"] == ["fs", "trash", "--json", f"/my-files/B/{OLD}"]


def test_trash_argv_for_other_kinds(cli, respond, argv_log):
    respond(stdout=TRASH_OK)
    cli.trash(FOLDER, "azerothcore-backup-manual-2026-10-20T01-02-03.tar.gz")
    assert argv_log()[-1]["argv"][-1] == "/my-files/B/azerothcore-backup-manual-2026-10-20T01-02-03.tar.gz"


@pytest.mark.parametrize(
    "stdout",
    [
        '[\n{"ok":false,"uid":"a","error":{}}\n]\n',  # exit 0 but not ok
        "[]",  # nothing was trashed
        '[{"ok":true,"uid":"a"},{"ok":true,"uid":"b"}]',  # we asked for exactly one
        '[{"uid":"a"}]',  # ok missing
        '[{"ok":"yes","uid":"a"}]',  # ok must be exactly true
        "[1]",  # not a result object
    ],
)
def test_trash_not_ok_result_raises(cli, respond, stdout):
    respond(stdout=stdout)
    with pytest.raises(CloudCliError) as exc:
        cli.trash(FOLDER, OLD)
    assert exc.value.kind == "failed"


@pytest.mark.parametrize("stdout", ["", "not json", "{}", '[\n{"ok":true'])
def test_trash_unreadable_result_is_a_parse_error(cli, respond, stdout):
    respond(stdout=stdout)
    with pytest.raises(CloudCliError) as exc:
        cli.trash(FOLDER, OLD)
    assert exc.value.kind == "parse"


@pytest.mark.parametrize(
    "name",
    [
        "../x.tar.gz",
        "holiday.jpg",
        "azerothcore-backup-daily-2026-10-20.tar.gz/..",
        "",
        "azerothcore-backup-daily-a/b.tar.gz",
        "azerothcore-backup-daily-a\\b.tar.gz",
        "azerothcore-backup-daily-a/../../b.tar.gz",
        "azerothcore-backup-daily-a\x01b.tar.gz",
        "azerothcore-backup-daily-2026-10-20.tar.gz\n",
        "/my-files/B/azerothcore-backup-daily-2026-10-20.tar.gz",
        None,
    ],
)
def test_trash_refuses_non_backup_names(cli, name, argv_log):
    with pytest.raises(ValueError):
        cli.trash(FOLDER, name)
    assert argv_log() == []


@pytest.mark.parametrize("folder", ["", "/my-files", "/elsewhere/B", "/my-files/../B"])
def test_trash_refuses_a_bad_folder(cli, argv_log, folder):
    with pytest.raises(ValueError):
        cli.trash(folder, OLD)
    assert argv_log() == []


# --- error classification ---


def test_auth_and_not_found_errors_are_classified(cli, respond):
    respond(stderr="You need to login first\n", exit=1)
    with pytest.raises(CloudCliError) as exc:
        cli.list_folder(FOLDER)
    assert exc.value.kind == "auth"
    assert exc.value.message == "You need to login first"

    respond(stderr="Node not found: B\n", exit=1)
    with pytest.raises(CloudCliError) as exc:
        cli.list_folder(FOLDER)
    assert exc.value.kind == "not_found"
    assert exc.value.message == "Node not found: B"


@pytest.mark.parametrize(
    "stderr", ["Root node not found\n", "node not found\n", "NODE NOT FOUND: x\n", "Error: Node not found: A B\n"]
)
def test_not_found_matches_case_insensitively(cli, respond, stderr):
    respond(stderr=stderr, exit=1)
    with pytest.raises(CloudCliError) as exc:
        cli.info(FOLDER)
    assert exc.value.kind == "not_found"


def test_not_found_is_also_read_from_stdout(cli, respond):
    respond(stdout="Node not found: B\n", exit=1)
    with pytest.raises(CloudCliError) as exc:
        cli.info(FOLDER)
    assert exc.value.kind == "not_found"


def test_auth_text_is_matched_exactly(cli, respond):
    respond(stderr="you need to login first\n", exit=1)
    with pytest.raises(CloudCliError) as exc:
        cli.info(FOLDER)
    assert exc.value.kind == "failed"


def test_other_failures_use_the_last_stderr_line(cli, respond):
    respond(stderr="warming up\n\nValidationError: boom happened\n\n", exit=1)
    with pytest.raises(CloudCliError) as exc:
        cli.info(FOLDER)
    assert exc.value.kind == "failed"
    assert exc.value.message == "ValidationError: boom happened"


def test_a_nonzero_exit_with_no_output_still_has_a_message(cli, respond):
    respond(exit=2)
    with pytest.raises(CloudCliError) as exc:
        cli.info(FOLDER)
    assert exc.value.kind == "failed"
    assert "2" in exc.value.message


def test_error_message_is_clean_and_short(paths):
    noisy = "first\n\x1b[31mboom\x07 with\ttab\x00 ‮\x85end\x1b]0;title\x07\n   \n"
    err = _error_for(_stub(paths, stderr=noisy, returncode=1))
    assert err.message == "boom with tab end"
    assert err.message == str(err)

    long_err = _error_for(_stub(paths, stderr="x" * 1000 + "\n", returncode=1))
    assert 0 < len(long_err.message) <= 300

    only_control = _error_for(_stub(paths, stderr="\x01\x02\n", returncode=3))
    assert only_control.message and "3" in only_control.message
    assert all(ch.isprintable() for ch in only_control.message)


def test_upload_failure_message_is_clean_and_short(paths, archive):
    bad = json.dumps(
        {
            "transferredItems": 0, "transferredBytes": 0, "skippedItems": 0, "failedItems": 1,
            "failures": [{"name": "n", "error": "E: " + "y" * 900 + "\x1b[0m\x07"}],
        }
    )
    with pytest.raises(CloudCliError) as exc:
        _stub(paths, stdout=bad, returncode=1).upload(archive, FOLDER)
    assert len(exc.value.message) <= 300
    assert all(ch.isprintable() for ch in exc.value.message)


def _error_for(cli: ProtonDriveCli) -> CloudCliError:
    with pytest.raises(CloudCliError) as exc:
        cli.info(FOLDER)
    return exc.value


def test_cloud_cli_error_keeps_kind_and_message():
    err = CloudCliError(kind="parse", message="bad\x00 output")
    assert err.kind == "parse"
    assert err.message == "bad output"
    assert str(err) == "bad output"


# --- timeouts and a binary that cannot run ---


def _timeout_run(argv, **kwargs):
    raise subprocess.TimeoutExpired(argv, kwargs.get("timeout", 0))


@pytest.mark.parametrize(
    "call",
    [
        lambda c, a: c.version(),
        lambda c, a: c.info(FOLDER),
        lambda c, a: c.list_folder(FOLDER),
        lambda c, a: c.upload(a, FOLDER),
        lambda c, a: c.trash(FOLDER, OLD),
        lambda c, a: c.create_folder(FOLDER),
        lambda c, a: c.logout(),
    ],
)
def test_timeout_is_classified(paths, archive, call):
    cli = ProtonDriveCli(paths, run=_timeout_run)
    with pytest.raises(CloudCliError) as exc:
        call(cli, archive)
    assert exc.value.kind == "timeout"
    assert exc.value.message


def test_each_call_gets_its_timeout(cli, archive, respond, runs):
    respond(stdout=NODE_FOLDER)
    cli.info(FOLDER)
    cli.create_folder(FOLDER)
    respond(stdout="[]")
    cli.list_folder(FOLDER)
    respond(stdout=TRASH_OK)
    cli.trash(FOLDER, OLD)
    respond(stdout=VERSION_OUT)
    cli.version()
    respond()
    cli.logout()
    respond(stdout=UPLOAD_OK)
    cli.upload(archive, FOLDER)
    by_command = {tuple(argv[1:3]) if argv[1] != "version" else ("version",): kw["timeout"] for argv, kw in runs}
    assert by_command == {
        ("fs", "info"): 120,
        ("fs", "create-folder"): 120,
        ("fs", "list"): 120,
        ("fs", "trash"): 120,
        ("version",): 120,
        ("auth", "logout"): 120,
        ("fs", "upload"): 3600,
    }


def test_run_kwargs_never_inherit_stdin_or_the_app_env(cli, respond, runs, monkeypatch):
    monkeypatch.setenv("ADMIN_SECRET_TOKEN", "s3cret")
    respond(stdout="[]")
    cli.list_folder(FOLDER)
    argv, kwargs = runs[-1]
    assert argv[0] == str(cli.paths.binary)
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True
    assert kwargs["env"] == cli.env()
    assert "ADMIN_SECRET_TOKEN" not in kwargs["env"]
    assert not kwargs.get("shell")


def test_every_cli_process_runs_with_umask_077(cli, archive, respond, runs, paths):
    # Spec 6.2: the CLI's cache, logs and session files are created private (M-2).
    respond(stdout="[]")
    cli.list_folder(FOLDER)
    respond(stdout=UPLOAD_OK)
    cli.upload(archive, FOLDER)
    assert [kwargs["umask"] for _, kwargs in runs] == [0o077, 0o077]

    calls = []

    def popen(argv, **kwargs):
        calls.append(kwargs)
        return object()

    ProtonDriveCli(paths, popen=popen).start_login()
    assert calls[0]["umask"] == 0o077


def test_child_sees_only_the_wrapper_env_and_devnull_stdin(cli, respond, argv_log, paths, monkeypatch):
    monkeypatch.setenv("ADMIN_SECRET_TOKEN", "s3cret")
    respond(stdout="[]")
    cli.list_folder(FOLDER)
    row = argv_log()[-1]
    assert "ADMIN_SECRET_TOKEN" not in row["env"]
    assert {k: row["env"][k] for k in cli.env()} == cli.env()
    assert row["PROTON_DRIVE_CACHE_DIR"] == str(paths.cache_dir)
    assert row["GNUPGHOME"] == str(paths.gnupg_home)
    assert row["stdin_is_devnull"] is True


def test_binary_that_is_missing_or_not_executable_is_a_plain_failure(paths, tmp_path):
    missing = ProtonDriveCli(CliPaths(**{**paths.__dict__, "binary": tmp_path / "nope"}))
    with pytest.raises(CloudCliError) as exc:
        missing.version()
    assert exc.value.kind == "failed"
    assert exc.value.message
    assert str(tmp_path) not in exc.value.message

    plain = tmp_path / "plain"
    plain.write_text("#!/bin/sh\n")
    plain.chmod(0o644)
    no_exec = ProtonDriveCli(CliPaths(**{**paths.__dict__, "binary": plain}))
    with pytest.raises(CloudCliError) as exc:
        no_exec.list_folder(FOLDER)
    assert exc.value.kind == "failed"


# --- version and logout ---


def test_version_returns_first_non_empty_line(cli, respond, argv_log):
    respond(stdout="\n  " + VERSION_OUT)
    assert cli.version() == "Proton Drive CLI drive-cli@0.9.0"
    assert argv_log()[-1]["argv"] == ["version"]


def test_version_with_no_output_is_a_parse_error(cli, respond):
    respond(stdout="\n  \n")
    with pytest.raises(CloudCliError) as exc:
        cli.version()
    assert exc.value.kind == "parse"


def test_logout_argv(cli, respond, argv_log):
    respond()
    assert cli.logout() is None
    assert argv_log()[-1]["argv"] == ["auth", "logout"]


def test_logout_failure_raises(cli, respond):
    respond(stderr="nope\n", exit=1)
    with pytest.raises(CloudCliError) as exc:
        cli.logout()
    assert (exc.value.kind, exc.value.message) == ("failed", "nope")


# --- login ---


def test_start_login_spawns_login_with_a_piped_line_buffered_stdout(paths):
    calls = []
    sentinel = object()

    def popen(argv, **kwargs):
        calls.append((argv, kwargs))
        return sentinel

    cli = ProtonDriveCli(paths, popen=popen)
    assert cli.start_login() is sentinel
    argv, kwargs = calls[0]
    assert argv == [str(paths.binary), "auth", "login", "--json"]
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["stdout"] == subprocess.PIPE
    assert kwargs["stderr"] == subprocess.PIPE
    assert kwargs["text"] is True
    assert kwargs["bufsize"] == 1
    assert kwargs["start_new_session"] is True
    assert kwargs["env"] == cli.env()


def test_start_login_reads_the_sign_in_line(cli, respond, argv_log):
    respond(stdout='{"signInUrl": "https://account.proton.me/x"}\n')
    proc = cli.start_login()
    try:
        first = proc.stdout.readline()
        proc.communicate(timeout=30)
    finally:
        proc.kill()
    assert json.loads(first) == {"signInUrl": "https://account.proton.me/x"}
    assert argv_log()[-1]["argv"] == ["auth", "login", "--json"]
    assert argv_log()[-1]["PROTON_DRIVE_CREDENTIALS_STORE"] == "pass"


# --- confinement ---


def test_every_public_method_stays_inside_the_allowlist(cli, backups_dir, archive, respond, argv_log):
    respond(stdout=VERSION_OUT)
    cli.version()
    respond(stdout=NODE_FOLDER)
    cli.info(FOLDER)
    cli.create_folder("/my-files/WoW Backups")
    respond(stdout=LISTING)
    cli.list_folder(FOLDER)
    respond(stdout=UPLOAD_OK)
    cli.upload(archive, FOLDER)
    respond(stdout=TRASH_OK)
    cli.trash(FOLDER, OLD)
    respond()
    cli.logout()
    respond(stdout='{"signInUrl": "https://x"}\n')
    proc = cli.start_login()
    proc.communicate(timeout=30)

    assert {tuple(a["argv"][:2]) for a in argv_log()} <= {
        ("auth", "login"), ("auth", "logout"), ("version",), ("fs", "info"),
        ("fs", "list"), ("fs", "upload"), ("fs", "trash"), ("fs", "create-folder"),
    }
    assert not any(
        tok in a["argv"]
        for a in argv_log()
        for tok in ("delete", "empty-trash", "replace", "create-new-revision", "rename", "download", "move", "copy",
                    "restore", "sharing", "trash-remote")
    )
    assert [a["argv"] for a in argv_log()] == [
        ["version"],
        ["fs", "info", "--json", FOLDER],
        ["fs", "create-folder", "--json", "/my-files", "WoW Backups"],
        ["fs", "list", "--json", FOLDER],
        ["fs", "upload", "--json", "-f", "skip", "-t", str(archive), FOLDER],
        ["fs", "trash", "--json", f"{FOLDER}/{OLD}"],
        ["auth", "logout"],
        ["auth", "login", "--json"],
    ]


@pytest.mark.parametrize(
    "args",
    [
        ["fs", "delete", "--json", "/my-files/B/x"],
        ["fs", "empty-trash"],
        ["fs", "download", "/my-files/B/x", "/tmp"],
        ["fs", "move", "a", "b"],
        ["fs", "rename", "a", "b"],
        ["fs", "copy", "a", "b"],
        ["fs", "restore", "a"],
        ["sharing", "invite"],
        ["fs"],
        [],
    ],
)
def test_the_internal_runner_refuses_anything_off_the_allowlist(cli, argv_log, args):
    with pytest.raises(RuntimeError):
        cli._invoke(args, 10)
    assert argv_log() == []


def test_upload_flags_cannot_be_changed_by_callers(cli):
    # Only (archive, folder) are accepted; there is no conflict-strategy or extra-flag parameter.
    with pytest.raises(TypeError):
        cli.upload(Path("/x"), FOLDER, "replace")  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        cli.upload(Path("/x"), FOLDER, strategy="replace")  # type: ignore[call-arg]


# --- environment and binary ---


def test_env_is_minimal_fixed_and_not_inherited(cli, paths, monkeypatch):
    monkeypatch.setenv("HOME", "/home/svc")
    monkeypatch.setenv("ADMIN_SECRET_TOKEN", "s3cret")
    monkeypatch.setenv("PATH", "/somewhere/else")
    assert cli.env() == {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": "/home/svc",
        "LANG": "C.UTF-8",
        "PROTON_DRIVE_CREDENTIALS_STORE": "pass",
        "PROTON_DRIVE_CACHE_DIR": str(paths.cache_dir),
        "GNUPGHOME": str(paths.gnupg_home),
        "PASSWORD_STORE_DIR": str(paths.store_dir),
        "PASSWORD_STORE_GPG_OPTS": "--batch --pinentry-mode error",
        "PROTON_DRIVE_LOG_LEVEL": "INFO",
        "TPM2TOOLS_TCTI": "device:/dev/tpmrm0",
    }
    assert cli.env() is not cli.env()  # a fresh dict every call; callers may mutate their copy


def test_env_home_falls_back_to_the_passwd_entry(cli, monkeypatch):
    monkeypatch.delenv("HOME", raising=False)
    assert cli.env()["HOME"] == pwd.getpwuid(os.getuid()).pw_dir
    monkeypatch.setenv("HOME", "")
    assert cli.env()["HOME"] == pwd.getpwuid(os.getuid()).pw_dir


def test_env_home_without_a_passwd_entry_uses_the_cache_dir(cli, paths, monkeypatch):
    def no_such_user(uid):
        raise KeyError(uid)

    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.setattr(cloud_cli.pwd, "getpwuid", no_such_user)
    assert cli.env()["HOME"] == str(paths.cache_dir)


def test_default_binary(monkeypatch):
    monkeypatch.delenv("PROTON_DRIVE_BIN", raising=False)
    assert default_binary() == Path("/usr/local/bin/proton-drive")
    monkeypatch.setenv("PROTON_DRIVE_BIN", "/opt/fake/proton-drive")
    assert default_binary() == Path("/opt/fake/proton-drive")
    monkeypatch.setenv("PROTON_DRIVE_BIN", "")
    assert default_binary() == Path("/usr/local/bin/proton-drive")


def test_paths_are_exposed_and_frozen(cli, paths):
    assert cli.paths is paths
    with pytest.raises(dataclasses.FrozenInstanceError):
        paths.binary = Path("/x")  # type: ignore[misc]


# --- the folder rules apply to every method ---


@pytest.mark.parametrize("folder", ["", "/my-files", "/my-files/", "/elsewhere/B", "/my-files/../B", "relative/B"])
def test_every_folder_argument_is_validated_before_anything_runs(cli, archive, argv_log, folder):
    for call in (
        lambda: cli.info(folder),
        lambda: cli.list_folder(folder),
        lambda: cli.create_folder(folder),
        lambda: cli.upload(archive, folder),
        lambda: cli.trash(folder, OLD),
    ):
        with pytest.raises(ValueError):
            call()
    assert argv_log() == []
