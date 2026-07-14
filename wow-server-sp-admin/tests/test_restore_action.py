import fcntl
import io
import json
import os
import subprocess
import tarfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.services import actions
from app.services.actions import ActionResult, KNOWN_DBS, SQL_FOOTER_TAIL_BYTES
from app.services.compose_admin import validate_restored_overlay
from app.services.config_index import KeyEntry

INT_ENTRY = KeyEntry(
    key="AiPlayerbot.MinRandomBots",
    default="1000",
    inferred_type="int",
    comment="",
    source_file="playerbots.conf.dist",
    line_number=1,
    env_var="AC_AI_PLAYERBOT_MIN_RANDOM_BOTS",
)


def _make_archive(
    backups: Path,
    name: str,
    dbs=KNOWN_DBS,
    with_admin_yml=True,
    *,
    skipped=(),
    dump_text="-- dump --\n-- Dump completed on 2026-07-11  3:00:01\n",
    admin_yml_text="services: {}\n",
) -> Path:
    backups.mkdir(parents=True, exist_ok=True)
    stage = backups.parent / "stage"
    (stage / "sql").mkdir(parents=True, exist_ok=True)
    (stage / "config").mkdir(parents=True, exist_ok=True)
    for db in dbs:
        (stage / "sql" / f"{db}.sql").write_text(dump_text)
    if with_admin_yml:
        (stage / "config" / "docker-compose.admin.yml").write_text(admin_yml_text)
    (stage / "manifest.json").write_text(json.dumps({
        "format_version": 1, "label": "manual", "databases": list(dbs),
        "skipped_databases": list(skipped), "git_revisions": {}, "ac_image": "x", "stack_dir": "/x",
    }))
    archive = backups / name
    with tarfile.open(archive, "w:gz") as tf:
        tf.add(stage / "manifest.json", arcname="manifest.json")
        tf.add(stage / "sql", arcname="sql")
        tf.add(stage / "config", arcname="config")
    return archive


def _append_member(archive: Path, name: str, payload: bytes = b"x", *, type=None) -> None:
    with tarfile.open(archive, "r:gz") as source:
        existing = [
            (member, source.extractfile(member).read() if member.isfile() else None)
            for member in source.getmembers()
        ]
    with tarfile.open(archive, "w:gz") as tf:
        for member, contents in existing:
            tf.addfile(member, io.BytesIO(contents) if contents is not None else None)
        member = tarfile.TarInfo(name)
        member.size = len(payload)
        if type is not None:
            member.type = type
            member.linkname = "target"
        tf.addfile(member, io.BytesIO(payload) if member.isfile() else None)


def _append_empty_directories(archive: Path, count: int) -> None:
    """Add real tar directory headers without allocating archive payloads."""
    replacement = archive.with_suffix(".replacement.tar.gz")
    with tarfile.open(archive, "r:gz") as source, tarfile.open(replacement, "w:gz") as target:
        for member in source:
            contents = source.extractfile(member)
            target.addfile(member, contents if member.isfile() else None)
        for index in range(count):
            directory = tarfile.TarInfo(f"padding/{index}")
            directory.type = tarfile.DIRTYPE
            target.addfile(directory)
    os.replace(replacement, archive)


V2_DUMP = (
    b"CREATE DATABASE /*!32312 IF NOT EXISTS*/ `acore_auth` /*!40100 DEFAULT CHARACTER SET utf8mb4 */;\n"
    b"-- Current Database: `acore_auth`\n"
    b"USE `acore_auth`;\n"
    b"CREATE DATABASE /*!32312 IF NOT EXISTS*/ `acore_characters` /*!40100 DEFAULT CHARACTER SET utf8mb4 */;\n"
    b"-- Current Database: `acore_characters`\n"
    b"USE `acore_characters`;\n"
    b"CREATE DATABASE /*!32312 IF NOT EXISTS*/ `acore_world` /*!40100 DEFAULT CHARACTER SET utf8mb4 */;\n"
    b"-- Current Database: `acore_world`\n"
    b"USE `acore_world`;\n"
    b"CREATE DATABASE /*!32312 IF NOT EXISTS*/ `acore_playerbots` /*!40100 DEFAULT CHARACTER SET utf8mb4 */;\n"
    b"-- Current Database: `acore_playerbots`\n"
    b"USE `acore_playerbots`;\n"
    b"-- Dump completed on 2026-07-11  3:00:01\n"
)


def _make_v2_archive(
    backups: Path, name: str, *, dump: bytes = V2_DUMP, manifest: bytes | None = None,
) -> Path:
    backups.mkdir(parents=True, exist_ok=True)
    archive = backups / name
    if manifest is None:
        manifest = json.dumps({
            "format_version": 2,
            "databases": list(KNOWN_DBS),
            "skipped_databases": [],
            "dump_layout": "single-multi-database",
        }).encode()
    overlay = b"services: {}\n"
    with tarfile.open(archive, "w:gz") as tf:
        for name, payload in (
            ("manifest.json", manifest),
            ("sql/azerothcore.sql", dump),
            ("config/docker-compose.admin.yml", overlay),
        ):
            member = tarfile.TarInfo(name)
            member.size = len(payload)
            tf.addfile(member, io.BytesIO(payload))
    return archive


def _v2_dump_for_sections(sections: tuple[str, ...]) -> bytes:
    body = b"".join(
        (
            f"CREATE DATABASE /*!32312 IF NOT EXISTS*/ `{database}` "
            "/*!40100 DEFAULT CHARACTER SET utf8mb4 */;\n"
            f"-- Current Database: `{database}`\n"
            f"USE `{database}`;\n"
        ).encode()
        for database in sections
    )
    return body + b"-- Dump completed on 2026-07-11  3:00:01\n"


@pytest.mark.parametrize("format_version", [True, 1.0])
def test_validate_canonical_backup_rejects_non_integer_format_version(
    tmp_path, format_version,
):
    manifest = json.dumps({
        "format_version": format_version,
        "databases": list(KNOWN_DBS),
        "skipped_databases": [],
    }).encode()
    archive = _make_v2_archive(
        tmp_path / "backups", "azerothcore-backup-manual-x.tar.gz", manifest=manifest,
    )
    assert "unsupported manifest format" in actions.validate_canonical_backup(archive)


@pytest.mark.parametrize("databases", [True, 1, "acore_auth", {"acore_auth": 1}])
def test_validate_canonical_backup_rejects_non_list_inventory(tmp_path, databases):
    manifest = json.dumps({
        "format_version": 2,
        "databases": databases,
        "skipped_databases": [],
        "dump_layout": "single-multi-database",
    }).encode()
    archive = _make_v2_archive(
        tmp_path / "backups", "azerothcore-backup-manual-x.tar.gz", manifest=manifest,
    )
    assert "canonical databases" in actions.validate_canonical_backup(archive)


def test_validate_canonical_backup_rejects_oversized_manifest_before_read(tmp_path):
    payload = b" " * (1024 ** 2 + 1)
    archive = _make_v2_archive(
        tmp_path / "backups", "azerothcore-backup-manual-x.tar.gz", manifest=payload,
    )
    assert "manifest" in actions.validate_canonical_backup(archive)
    assert "size limit" in actions.validate_canonical_backup(archive)


def test_validate_canonical_backup_rejects_oversized_admin_overlay(tmp_path):
    archive = _make_v2_archive(
        tmp_path / "backups", "azerothcore-backup-manual-x.tar.gz",
    )
    _append_member(
        archive,
        "config/oversized-marker",
        b"x",
    )
    # Rebuild with one unique oversized canonical overlay, not a duplicate member.
    replacement = archive.with_name("replacement.tar.gz")
    with tarfile.open(archive, "r:gz") as source, tarfile.open(replacement, "w:gz") as target:
        for member in source:
            if member.name == "config/docker-compose.admin.yml":
                continue
            target.addfile(member, source.extractfile(member) if member.isfile() else None)
        payload = b"services: {}\n#" + b"x" * (1024 ** 2)
        member = tarfile.TarInfo("config/docker-compose.admin.yml")
        member.size = len(payload)
        target.addfile(member, io.BytesIO(payload))
    os.replace(replacement, archive)
    assert "admin overlay" in actions.validate_canonical_backup(archive)
    assert "size limit" in actions.validate_canonical_backup(archive)


def test_restored_overlay_rejects_invalid_typed_and_empty_values(tmp_path):
    path = tmp_path / "admin.yml"
    entries = {INT_ENTRY.env_var: INT_ENTRY}
    for value in ("not-an-int", ""):
        path.write_text(
            "services:\n  ac-worldserver:\n    environment:\n"
            f"      {INT_ENTRY.env_var}: '{value}'\n"
        )
        assert "invalid value" in validate_restored_overlay(path, entries_by_env=entries)


@patch("app.services.actions.run_stop")
def test_run_restore_rejects_invalid_typed_overlay_before_stop(
    mock_stop, tmp_path, monkeypatch,
):
    from app.services.compose_admin import validate_restored_overlay
    from app.services.config_index import KeyEntry
    monkeypatch.setenv("AC_STACK_DIR", str(tmp_path))
    entry = KeyEntry(
        key="AiPlayerbot.MinRandomBots",
        default="1000",
        inferred_type="int",
        comment="",
        source_file="playerbots.conf.dist",
        line_number=1,
        env_var="AC_AI_PLAYERBOT_MIN_RANDOM_BOTS",
    )
    archive = _make_archive(
        tmp_path / "backups",
        "azerothcore-backup-manual-x.tar.gz",
        admin_yml_text=(
            "services:\n  ac-worldserver:\n    environment:\n"
            "      AC_AI_PLAYERBOT_MIN_RANDOM_BOTS: not-an-int\n"
        ),
    )
    monkeypatch.setattr(
        "app.services.actions._validate_restored_admin_yml",
        lambda path: validate_restored_overlay(path, entries_by_env={entry.env_var: entry}),
    )
    result = actions.run_restore(archive.name, on_progress=lambda *_: None)
    assert result == ActionResult.ERROR
    mock_stop.assert_not_called()


def _make_archive_with_overlay_directory(backups: Path, name: str) -> Path:
    """Build an otherwise canonical v1 archive whose overlay path is a directory."""
    backups.mkdir(parents=True, exist_ok=True)
    stage = backups.parent / "directory-overlay-stage"
    (stage / "sql").mkdir(parents=True)
    (stage / "config").mkdir()
    for db in KNOWN_DBS:
        (stage / "sql" / f"{db}.sql").write_text(
            "-- dump --\n-- Dump completed on 2026-07-11  3:00:01\n"
        )
    (stage / "manifest.json").write_text(json.dumps({
        "format_version": 1,
        "databases": list(KNOWN_DBS),
        "skipped_databases": [],
    }))
    archive = backups / name
    with tarfile.open(archive, "w:gz") as tf:
        tf.add(stage / "manifest.json", arcname="manifest.json")
        tf.add(stage / "sql", arcname="sql")
        tf.add(stage / "config", arcname="config")
        overlay_dir = tarfile.TarInfo("config/docker-compose.admin.yml")
        overlay_dir.type = tarfile.DIRTYPE
        tf.addfile(overlay_dir)
    return archive


def test_run_restore_rejects_path_traversal(tmp_path, monkeypatch):
    monkeypatch.setenv("AC_STACK_DIR", str(tmp_path))
    r = actions.run_restore("../../etc/passwd", on_progress=lambda *a: None)
    assert r == ActionResult.ERROR


@patch("app.services.actions.run_stop")
def test_run_restore_rejects_backup_symlink_before_archive_open(
    mock_stop, tmp_path, monkeypatch,
):
    monkeypatch.setenv("AC_STACK_DIR", str(tmp_path))
    backups = tmp_path / "backups"
    backups.mkdir()
    outside = tmp_path / "outside.tar.gz"
    outside.write_bytes(b"not an archive")
    link = backups / "azerothcore-backup-manual-link.tar.gz"
    link.symlink_to(outside)

    with patch("app.services.actions.tarfile.open", side_effect=AssertionError("archive opened")):
        result = actions.run_restore(link.name, on_progress=lambda *_: None)

    assert result == ActionResult.ERROR
    mock_stop.assert_not_called()


def test_run_restore_rejects_unknown_db_in_manifest(tmp_path, monkeypatch):
    monkeypatch.setenv("AC_STACK_DIR", str(tmp_path))
    _make_archive(tmp_path / "backups", "azerothcore-backup-manual-x.tar.gz", ["evil_db"])
    r = actions.run_restore("azerothcore-backup-manual-x.tar.gz", on_progress=lambda *a: None)
    assert r == ActionResult.ERROR


def test_validate_canonical_backup_accepts_canonical_v2_archive(tmp_path):
    archive = _make_v2_archive(tmp_path / "backups", "azerothcore-backup-manual-x.tar.gz")

    assert actions.validate_canonical_backup(archive) is None


def test_validate_canonical_backup_never_extracts_archive(tmp_path, monkeypatch):
    archive = _make_v2_archive(tmp_path / "backups", "azerothcore-backup-manual-x.tar.gz")
    monkeypatch.setattr(
        tarfile.TarFile,
        "extract",
        lambda *_a, **_k: pytest.fail("archive member extracted"),
    )
    monkeypatch.setattr(
        tarfile.TarFile,
        "extractall",
        lambda *_a, **_k: pytest.fail("archive extracted"),
    )

    assert actions.validate_canonical_backup(archive) is None


@patch("app.services.actions.run_stop")
@pytest.mark.parametrize(
    "format_values",
    [(2, 99), (99, 2)],
    ids=["canonical-then-conflicting", "conflicting-then-canonical"],
)
def test_run_restore_rejects_duplicate_v2_manifest_keys_before_stop(
    mock_stop, tmp_path, monkeypatch, format_values,
):
    monkeypatch.setenv("AC_STACK_DIR", str(tmp_path))
    manifest = (
        f'{{"format_version":{format_values[0]},"format_version":{format_values[1]},'
        '"databases":["acore_auth","acore_characters","acore_world","acore_playerbots"],'
        '"skipped_databases":[],"dump_layout":"single-multi-database"}'
    ).encode()
    archive = _make_v2_archive(
        tmp_path / "backups", "azerothcore-backup-manual-x.tar.gz", manifest=manifest,
    )

    result = actions.run_restore(archive.name, on_progress=lambda *args: None)

    assert result == ActionResult.ERROR
    mock_stop.assert_not_called()


def test_v2_inventory_validator_stops_after_the_first_extra_section():
    class Stream:
        calls = 0

        def readline(self, _limit):
            self.calls += 1
            if self.calls <= len(KNOWN_DBS):
                return f"-- Current Database: `{KNOWN_DBS[self.calls - 1]}`\n".encode()
            if self.calls == len(KNOWN_DBS) + 1:
                return b"-- Current Database: `acore_auth`\n"
            raise AssertionError("validator read beyond the first invalid section")

    stream = Stream()

    assert "database sections" in actions._validate_v2_sql_stream_inventory(stream)
    assert stream.calls == len(KNOWN_DBS) + 1


@pytest.mark.parametrize(
    "malformed_marker",
    [
        b"-- Current Database: acore_auth\n",
        b"-- Current Database: `acore_auth` trailing\n",
    ],
    ids=["missing-backticks", "trailing-content"],
)
def test_v2_inventory_validator_rejects_malformed_marker_prefixed_lines(malformed_marker):
    stream = io.BytesIO(malformed_marker + V2_DUMP)

    error = actions._validate_v2_sql_stream_inventory(stream)

    assert error == "v2 multi-database SQL dump has malformed database section header"


@patch("app.services.actions.run_stop")
@pytest.mark.parametrize(
    "sections",
    [
        KNOWN_DBS[:-1],
        (*KNOWN_DBS, "unexpected_schema"),
        (KNOWN_DBS[1], KNOWN_DBS[0], *KNOWN_DBS[2:]),
    ],
    ids=["missing", "extra", "reordered"],
)
def test_run_restore_rejects_noncanonical_v2_stream_sections_before_stop(
    mock_stop, tmp_path, monkeypatch, sections,
):
    monkeypatch.setenv("AC_STACK_DIR", str(tmp_path))
    archive = _make_v2_archive(
        tmp_path / "backups",
        "azerothcore-backup-manual-x.tar.gz",
        dump=_v2_dump_for_sections(sections),
    )

    result = actions.run_restore(archive.name, on_progress=lambda *args: None)

    assert result == ActionResult.ERROR
    mock_stop.assert_not_called()


@patch("app.services.actions.run_stop")
@pytest.mark.parametrize(
    ("dbs", "skipped"),
    [
        (["acore_auth"], ()),
        ([*KNOWN_DBS, "evil_db"], ()),
        (["acore_auth", "acore_auth", "acore_world", "acore_playerbots"], ()),
        (KNOWN_DBS, ("acore_world",)),
    ],
)
def test_run_restore_rejects_noncanonical_manifest_before_stop(
    mock_stop, tmp_path, monkeypatch, dbs, skipped,
):
    monkeypatch.setenv("AC_STACK_DIR", str(tmp_path))
    _make_archive(tmp_path / "backups", "azerothcore-backup-manual-x.tar.gz", dbs, skipped=skipped)

    result = actions.run_restore("azerothcore-backup-manual-x.tar.gz", on_progress=lambda *a: None)

    assert result == ActionResult.ERROR
    mock_stop.assert_not_called()


def test_sql_footer_validation_reads_only_a_bounded_tail():
    class RecordingBytesIO(io.BytesIO):
        def __init__(self, payload):
            super().__init__(payload)
            self.read_sizes = []

        def read(self, size=-1):
            self.read_sizes.append(size)
            return super().read(size)

    payload = b"x" * (SQL_FOOTER_TAIL_BYTES + 10) + b"\n-- Dump completed on 2026-07-11  3:00:01\n"
    stream = RecordingBytesIO(payload)

    assert actions._sql_has_canonical_completion_footer(stream, len(payload))
    assert stream.read_sizes == [SQL_FOOTER_TAIL_BYTES]


@patch("app.services.actions.run_stop")
@pytest.mark.parametrize("dump_text", ["", "-- dump --\n-- Dump completed\n"])
def test_run_restore_rejects_incomplete_dump_before_stop(
    mock_stop, tmp_path, monkeypatch, dump_text,
):
    monkeypatch.setenv("AC_STACK_DIR", str(tmp_path))
    _make_archive(
        tmp_path / "backups",
        "azerothcore-backup-manual-x.tar.gz",
        dump_text=dump_text,
    )

    result = actions.run_restore("azerothcore-backup-manual-x.tar.gz", on_progress=lambda *a: None)

    assert result == ActionResult.ERROR
    mock_stop.assert_not_called()


@patch("app.services.actions.run_stop")
def test_run_restore_rejects_expanded_archive_limit_violations_before_stop(
    mock_stop, tmp_path, monkeypatch,
):
    monkeypatch.setenv("AC_STACK_DIR", str(tmp_path))

    oversized = _make_archive(tmp_path / "backups", "azerothcore-backup-manual-x.tar.gz")
    _append_member(oversized, "large-extra", b"x" * 100)
    monkeypatch.setattr(actions, "MAX_EXPANDED_MEMBER_BYTES", 50)
    assert actions.run_restore(oversized.name, on_progress=lambda *a: None) == ActionResult.ERROR
    mock_stop.assert_not_called()

    many_members = _make_archive(tmp_path / "backups", "azerothcore-backup-manual-y.tar.gz")
    _append_member(many_members, "extra-member")
    monkeypatch.setattr(actions, "MAX_EXPANDED_MEMBER_BYTES", 8 * 1024 ** 3)
    monkeypatch.setattr(actions, "MAX_ARCHIVE_MEMBER_COUNT", 8)
    assert actions.run_restore(many_members.name, on_progress=lambda *a: None) == ActionResult.ERROR
    mock_stop.assert_not_called()

    total_too_large = _make_archive(tmp_path / "backups", "azerothcore-backup-manual-z.tar.gz")
    _append_member(total_too_large, "total-extra", b"1234")
    monkeypatch.setattr(actions, "MAX_ARCHIVE_MEMBER_COUNT", 10_000)
    with tarfile.open(total_too_large, "r:gz") as tf:
        expected_total = sum(member.size for member in tf.getmembers() if member.isfile())
    monkeypatch.setattr(actions, "MAX_EXPANDED_ARCHIVE_BYTES", expected_total - 1)
    assert actions.run_restore(total_too_large.name, on_progress=lambda *a: None) == ActionResult.ERROR
    mock_stop.assert_not_called()


def test_d02_archive_limits_reject_the_10001st_real_tar_member(tmp_path):
    assert actions.MAX_EXPANDED_ARCHIVE_BYTES == 16 * 1024 ** 3
    assert actions.MAX_ARCHIVE_MEMBER_COUNT == 10_000
    assert actions.MAX_EXPANDED_MEMBER_BYTES == 8 * 1024 ** 3

    archive = _make_archive(tmp_path / "backups", "azerothcore-backup-manual-x.tar.gz")
    with tarfile.open(archive, "r:gz") as tf:
        initial_count = len(tf.getmembers())
    _append_empty_directories(archive, actions.MAX_ARCHIVE_MEMBER_COUNT - initial_count + 1)

    assert actions.validate_canonical_backup(archive) == "archive has too many members"


@patch("app.services.actions.run_stop")
def test_run_restore_rejects_special_archive_member_before_stop(mock_stop, tmp_path, monkeypatch):
    monkeypatch.setenv("AC_STACK_DIR", str(tmp_path))
    archive = _make_archive(tmp_path / "backups", "azerothcore-backup-manual-x.tar.gz")
    _append_member(archive, "link", type=tarfile.SYMTYPE)

    assert actions.run_restore(archive.name, on_progress=lambda *a: None) == ActionResult.ERROR
    mock_stop.assert_not_called()


@patch("app.services.actions.run_stop")
def test_run_restore_rejects_admin_overlay_directory_before_stop(mock_stop, tmp_path, monkeypatch):
    monkeypatch.setenv("AC_STACK_DIR", str(tmp_path))
    archive = _make_archive_with_overlay_directory(
        tmp_path / "backups", "azerothcore-backup-manual-x.tar.gz",
    )

    assert actions.run_restore(archive.name, on_progress=lambda *a: None) == ActionResult.ERROR
    mock_stop.assert_not_called()


@patch("app.services.actions.run_stop")
@pytest.mark.parametrize(
    "admin_yml_text",
    [
        "services: [broken\n",
        "services:\n  ac-database:\n    environment: {}\n",
        (
            "services:\n"
            "  ac-worldserver:\n"
            "    environment:\n"
            "      AC_AUCTION_HOUSE_BOT_GUIDS: '1'\n"
        ),
    ],
)
def test_run_restore_rejects_invalid_admin_overlay_before_stop(
    mock_stop, tmp_path, monkeypatch, admin_yml_text,
):
    monkeypatch.setenv("AC_STACK_DIR", str(tmp_path))
    archive = _make_archive(
        tmp_path / "backups", "azerothcore-backup-manual-x.tar.gz", admin_yml_text=admin_yml_text,
    )

    assert actions.run_restore(archive.name, on_progress=lambda *a: None) == ActionResult.ERROR
    mock_stop.assert_not_called()


@patch("app.services.actions.run_start", return_value=ActionResult.OK)
@patch("app.services.actions.run_stop", return_value=ActionResult.OK)
@patch("app.services.backup.run_backup")
@patch("app.services.actions.db_credentials", return_value={"password": "pw"})
def test_run_restore_aborts_if_manifest_db_sql_is_missing(
    mock_creds, mock_backup, mock_stop, mock_start, tmp_path, monkeypatch
):
    monkeypatch.setenv("AC_STACK_DIR", str(tmp_path))
    mock_backup.return_value = type("R", (), {"ok": True, "archive": "safety", "output": ""})()
    archive = _make_archive(tmp_path / "backups", "azerothcore-backup-manual-x.tar.gz")
    with tarfile.open(archive, "r:gz") as src:
        members = [
            (m, src.extractfile(m).read() if m.isfile() else None)
            for m in src.getmembers()
            if m.name != "sql/acore_auth.sql"
        ]
    with tarfile.open(archive, "w:gz") as dst:
        for member, data in members:
            if data is None:
                dst.addfile(member)
            else:
                import io
                dst.addfile(member, io.BytesIO(data))

    r = actions.run_restore("azerothcore-backup-manual-x.tar.gz", on_progress=lambda *a: None)

    assert r == ActionResult.ERROR
    mock_stop.assert_not_called()
    mock_start.assert_not_called()


@patch("app.services.actions.run_start", return_value=ActionResult.OK)
@patch("app.services.actions.run_stop", return_value=ActionResult.OK)
@patch("app.services.backup.run_backup")
@patch("app.services.actions.subprocess.run")
@patch("app.services.actions.db_credentials", return_value={"password": "pw"})
def test_run_restore_happy_path_imports_and_takes_safety_backup(
    mock_creds, mock_run, mock_backup, mock_stop, mock_start, tmp_path, monkeypatch
):
    monkeypatch.setenv("AC_STACK_DIR", str(tmp_path))
    mock_backup.return_value = type("R", (), {"ok": True, "archive": "safety", "output": ""})()
    mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
    _make_archive(tmp_path / "backups", "azerothcore-backup-manual-x.tar.gz")

    r = actions.run_restore("azerothcore-backup-manual-x.tar.gz", on_progress=lambda *a: None)

    assert r == ActionResult.OK
    # Pre-restore safety backup taken with the prerestore label.
    assert mock_backup.call_args.args[0] == "prerestore"
    mock_stop.assert_called_once()
    mock_start.assert_called_once()
    # A drop/create + import happened for each DB (2 docker calls per DB).
    assert mock_run.call_count >= len(KNOWN_DBS)


@patch("app.services.actions.run_start", return_value=ActionResult.OK)
@patch("app.services.actions.run_stop", return_value=ActionResult.OK)
@patch("app.services.backup.run_backup")
@patch("app.services.actions.db_credentials", return_value={"password": "pw"})
def test_run_restore_aborts_if_safety_backup_fails(
    mock_creds, mock_backup, mock_stop, mock_start, tmp_path, monkeypatch
):
    monkeypatch.setenv("AC_STACK_DIR", str(tmp_path))
    mock_backup.return_value = type("R", (), {"ok": False, "archive": None, "output": ""})()
    _make_archive(tmp_path / "backups", "azerothcore-backup-manual-x.tar.gz")
    r = actions.run_restore("azerothcore-backup-manual-x.tar.gz", on_progress=lambda *a: None)
    assert r == ActionResult.ERROR
    mock_start.assert_called_once()  # server brought back up after abort


@patch("app.services.actions.run_start", return_value=ActionResult.OK)
@patch("app.services.actions.run_stop", return_value=ActionResult.OK)
@patch("app.services.backup.run_backup")
@patch("app.services.actions.subprocess.run")
def test_in_app_restore_refuses_backup_lock_before_database_mutation(
    mock_run, mock_backup, mock_stop, mock_start, tmp_path, monkeypatch,
):
    monkeypatch.setenv("AC_STACK_DIR", str(tmp_path))
    archive = _make_v2_archive(
        tmp_path / "backups", "azerothcore-backup-manual-x.tar.gz",
    )
    mock_backup.return_value = type(
        "Result", (), {"ok": True, "archive": "safety.tar.gz", "output": ""}
    )()
    mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
    progress = []
    lock_path = archive.parent / ".backup.lock"
    lock_path.touch()

    with lock_path.open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = actions.run_restore(
            archive.name,
            on_progress=lambda step, message: progress.append((step, message)),
        )

    assert result == ActionResult.ERROR
    mock_stop.assert_called_once()
    mock_backup.assert_called_once()
    mock_run.assert_not_called()
    mock_start.assert_called_once()
    assert any("backup or restore is already running" in message for _, message in progress)


@patch("app.services.actions.run_stop", return_value=ActionResult.OK)
@patch("app.services.backup.run_backup")
@patch("app.services.actions._restore_admin_yml", side_effect=OSError("write failed"))
@patch("app.services.actions.subprocess.run")
@patch("app.services.actions.db_credentials", return_value={"password": "pw"})
def test_run_restore_leaves_server_stopped_if_restore_step_raises_after_stop(
    mock_creds, mock_run, mock_restore_admin, mock_backup, mock_stop, tmp_path, monkeypatch
):
    monkeypatch.setenv("AC_STACK_DIR", str(tmp_path))
    mock_backup.return_value = type("R", (), {"ok": True, "archive": "safety", "output": ""})()
    mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
    _make_archive(tmp_path / "backups", "azerothcore-backup-manual-x.tar.gz")

    r = actions.run_restore("azerothcore-backup-manual-x.tar.gz", on_progress=lambda *a: None)

    assert r == ActionResult.ERROR
    mock_stop.assert_called_once()
    mock_restore_admin.assert_called_once()


@pytest.mark.parametrize("phase", ["drop", "create", "import"])
@patch("app.services.actions.run_start")
@patch("app.services.actions.run_stop", return_value=ActionResult.OK)
@patch("app.services.backup.run_backup")
@patch("app.services.actions.subprocess.run")
@patch("app.services.actions.db_credentials", return_value={"password": "pw"})
def test_run_restore_keeps_recovery_state_after_destructive_command_nonzero_exit(
    _mock_creds, mock_run, mock_backup, mock_stop, mock_start, phase, tmp_path, monkeypatch,
):
    """Each failed destructive subprocess leaves the stopped stack recoverable."""
    monkeypatch.setenv("AC_STACK_DIR", str(tmp_path))
    mock_backup.return_value = type("R", (), {"ok": True, "archive": "safety", "output": ""})()
    ok = MagicMock(returncode=0, stdout="", stderr="")
    failed = MagicMock(returncode=1, stdout="", stderr=f"{phase} failed")
    calls_before_failure = {"drop": 0, "create": 1, "import": 2}[phase]
    mock_run.side_effect = [ok] * calls_before_failure + [failed]
    _make_archive(tmp_path / "backups", "azerothcore-backup-manual-x.tar.gz")
    progress: list[tuple[str, str]] = []

    result = actions.run_restore(
        "azerothcore-backup-manual-x.tar.gz",
        on_progress=lambda step, message: progress.append((step, message)),
    )

    assert result == ActionResult.ERROR
    assert mock_run.call_count == calls_before_failure + 1
    command = mock_run.call_args_list[calls_before_failure].args[0]
    if phase == "import":
        assert "-i" in command
    else:
        sql = command[command.index("-e") + 1]
        assert sql == (
            f"DROP DATABASE IF EXISTS acore_auth;"
            if phase == "drop" else "CREATE DATABASE acore_auth;"
        )
    mock_stop.assert_called_once()
    mock_start.assert_not_called()
    assert any("server remains stopped" in message for _step, message in progress)
    assert any("safety" in message for _step, message in progress)


@pytest.mark.parametrize("phase", ["drop", "create", "import"])
@patch("app.services.actions.run_start")
@patch("app.services.actions.run_stop", return_value=ActionResult.OK)
@patch("app.services.backup.run_backup")
@patch("app.services.actions.subprocess.run")
@patch("app.services.actions.db_credentials", return_value={"password": "pw"})
def test_run_restore_keeps_recovery_state_after_destructive_command_timeout(
    _mock_creds, mock_run, mock_backup, mock_stop, mock_start, phase, tmp_path, monkeypatch,
):
    """Timeouts after Stop report the same stopped-stack recovery state."""
    monkeypatch.setenv("AC_STACK_DIR", str(tmp_path))
    mock_backup.return_value = type("R", (), {"ok": True, "archive": "safety", "output": ""})()
    ok = MagicMock(returncode=0, stdout="", stderr="")
    timeout = subprocess.TimeoutExpired(cmd="mysql", timeout=actions.MYSQL_TIMEOUT)
    calls_before_timeout = {"drop": 0, "create": 1, "import": 2}[phase]
    mock_run.side_effect = [ok] * calls_before_timeout + [timeout]
    _make_archive(tmp_path / "backups", "azerothcore-backup-manual-x.tar.gz")
    progress: list[tuple[str, str]] = []

    result = actions.run_restore(
        "azerothcore-backup-manual-x.tar.gz",
        on_progress=lambda step, message: progress.append((step, message)),
    )

    assert result == ActionResult.TIMEOUT
    assert mock_run.call_count == calls_before_timeout + 1
    mock_stop.assert_called_once()
    mock_start.assert_not_called()
    assert any(f"{phase} timed out" in message for _step, message in progress)
    assert any("server remains stopped" in message for _step, message in progress)
    assert any("pre-restore archive" in message for _step, message in progress)


@pytest.mark.parametrize("start_result", [ActionResult.ERROR, ActionResult.TIMEOUT])
@patch("app.services.actions.run_start")
@patch("app.services.actions.run_stop", return_value=ActionResult.OK)
@patch("app.services.backup.run_backup")
@patch("app.services.actions.subprocess.run")
@patch("app.services.actions.db_credentials", return_value={"password": "pw"})
def test_run_restore_reports_recovery_state_when_start_fails_after_import(
    _mock_creds, mock_run, mock_backup, mock_stop, mock_start, start_result, tmp_path, monkeypatch,
):
    monkeypatch.setenv("AC_STACK_DIR", str(tmp_path))
    mock_backup.return_value = type("R", (), {"ok": True, "archive": "safety", "output": ""})()
    mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
    mock_start.return_value = start_result
    _make_archive(tmp_path / "backups", "azerothcore-backup-manual-x.tar.gz")
    progress: list[tuple[str, str]] = []

    result = actions.run_restore(
        "azerothcore-backup-manual-x.tar.gz",
        on_progress=lambda step, message: progress.append((step, message)),
    )

    assert result == start_result
    mock_stop.assert_called_once()
    mock_start.assert_called_once()
    assert any("server remains stopped" in message for _step, message in progress)
    assert any("pre-restore archive" in message for _step, message in progress)


@pytest.mark.parametrize("phase", ["drop", "create", "import"])
@pytest.mark.parametrize("failure_kind", ["nonzero", "timeout"])
@patch("app.services.actions.run_start")
@patch("app.services.actions.run_stop", return_value=ActionResult.OK)
@patch("app.services.backup.run_backup")
@patch("app.services.actions.subprocess.run")
@patch("app.services.actions.db_credentials", return_value={"password": "pw"})
def test_v2_restore_reports_each_destructive_phase_failure_and_recovery_state(
    _mock_creds, mock_run, mock_backup, mock_stop, mock_start,
    phase, failure_kind, tmp_path, monkeypatch,
):
    """Current v2 backups create schemas inside their idempotent SQL stream."""
    monkeypatch.setenv("AC_STACK_DIR", str(tmp_path))
    mock_backup.return_value = type("R", (), {"ok": True, "archive": "safety", "output": ""})()
    archive = _make_v2_archive(tmp_path / "backups", "azerothcore-backup-manual-x.tar.gz")
    with tarfile.open(archive, "r:gz") as tf:
        dump = tf.extractfile("sql/azerothcore.sql").read()
    for db in KNOWN_DBS:
        assert f"CREATE DATABASE /*!32312 IF NOT EXISTS*/ `{db}`".encode() in dump

    calls_before_failure = {"drop": 0, "create": 1, "import": 2}[phase]
    ok = MagicMock(returncode=0, stdout="", stderr="")
    failure = (
        MagicMock(returncode=1, stdout="", stderr=f"{phase} failed")
        if failure_kind == "nonzero"
        else subprocess.TimeoutExpired(cmd="mysql", timeout=actions.MYSQL_TIMEOUT)
    )
    mock_run.side_effect = [ok] * calls_before_failure + [failure]
    progress: list[tuple[str, str]] = []

    result = actions.run_restore(
        archive.name,
        on_progress=lambda step, message: progress.append((step, message)),
    )

    assert result == (
        ActionResult.ERROR if failure_kind == "nonzero" else ActionResult.TIMEOUT
    )
    assert mock_run.call_count == calls_before_failure + 1
    command = mock_run.call_args_list[calls_before_failure].args[0]
    if phase == "import":
        assert "-i" in command
    else:
        sql = command[command.index("-e") + 1]
        expected = "DROP DATABASE IF EXISTS" if phase == "drop" else "CREATE DATABASE"
        assert expected in sql
        assert all(db in sql for db in KNOWN_DBS)
    mock_stop.assert_called_once()
    mock_start.assert_not_called()
    detail = f"v2 {phase} {'failed' if failure_kind == 'nonzero' else 'timed out'}"
    assert any(detail in message for _step, message in progress)
    assert any("server remains stopped" in message for _step, message in progress)
    assert any("safety" in message for _step, message in progress)


@patch("app.services.actions.run_start", return_value=ActionResult.OK)
@patch("app.services.actions.run_stop", return_value=ActionResult.OK)
@patch("app.services.backup.run_backup")
@patch("app.services.actions.subprocess.run")
@patch("app.services.actions.db_credentials", return_value={"password": "pw"})
def test_v2_create_phase_replays_dump_defined_charset_and_collation(
    _mock_creds, mock_run, mock_backup, _mock_stop, _mock_start, tmp_path, monkeypatch,
):
    monkeypatch.setenv("AC_STACK_DIR", str(tmp_path))
    mock_backup.return_value = type("R", (), {"ok": True, "archive": "safety", "output": ""})()
    custom_definition = (
        b"CREATE DATABASE /*!32312 IF NOT EXISTS*/ `acore_auth` "
        b"/*!40100 DEFAULT CHARACTER SET latin1 COLLATE latin1_swedish_ci */;"
    )
    archive = _make_v2_archive(
        tmp_path / "backups",
        "azerothcore-backup-manual-x.tar.gz",
        dump=V2_DUMP.replace(
            b"CREATE DATABASE /*!32312 IF NOT EXISTS*/ `acore_auth` "
            b"/*!40100 DEFAULT CHARACTER SET utf8mb4 */;",
            custom_definition,
        ),
    )
    mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

    assert actions.run_restore(archive.name, on_progress=lambda *_: None) == ActionResult.OK

    create_sql = mock_run.call_args_list[1].args[0]
    assert custom_definition.decode() in create_sql[create_sql.index("-e") + 1]
    assert "CREATE DATABASE acore_auth;" not in create_sql[create_sql.index("-e") + 1]


def test_v2_create_statement_extraction_does_not_read_the_entire_dump(tmp_path):
    sql_path = tmp_path / "azerothcore.sql"
    sql_path.write_bytes(V2_DUMP)

    with patch.object(Path, "read_text", side_effect=AssertionError("whole-file read")):
        create_sql = actions._v2_create_database_statements(sql_path)

    assert "DEFAULT CHARACTER SET utf8mb4" in create_sql
