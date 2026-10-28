"""Cloud backup settings, folder validation and the small JSON stores."""

from __future__ import annotations

import json
import stat

import pytest

from app.services.cloud_config import (
    ALL_LABELS,
    PROTECTED_LABELS,
    RETENTION_MAX,
    RETENTION_MIN,
    CloudConfig,
    CloudLogEntry,
    CloudState,
    CloudStore,
    RemoteFile,
    normalize_folder,
    split_folder,
    store_from_env,
)

# Shapes the Proton CLI resolves as a node UID instead of a name (R-3).
UID_22 = "a" * 22
UID_88 = "B" * 88


def test_constants():
    assert ALL_LABELS == (
        "daily", "manual", "prerestore", "preclear", "preupgrade", "prerollback", "imported",
    )
    assert PROTECTED_LABELS == frozenset({"preupgrade", "prerollback"})
    assert (RETENTION_MIN, RETENTION_MAX) == (1, 90)


def test_default_config_is_off_with_all_labels_and_seven_days(tmp_path):
    cfg = CloudStore(tmp_path).load_config()
    assert cfg == CloudConfig()
    assert cfg.enabled is False and cfg.retention_days == 7 and cfg.labels == ALL_LABELS


def test_config_round_trips_and_files_are_private(tmp_path):
    store = CloudStore(tmp_path)
    store.save_config(CloudConfig(enabled=True, folder="/my-files/WoW Backups", labels=("daily",), retention_days=14))
    assert store.load_config().folder == "/my-files/WoW Backups"
    assert stat.S_IMODE((tmp_path / "cloud").stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / "cloud" / "config.json").stat().st_mode) == 0o600


@pytest.mark.parametrize("raw", ["", "/my-files", "/my-files/", "my-files/x", "/photos/x", "/trash/x",
    "/my-files/../x", "/my-files/./x", "/my-files//x", "/my-files/a\\b", "/my-files/a\x07b", "/my-files/" + "x" * 256])
def test_normalize_folder_rejects(raw):
    with pytest.raises(ValueError):
        normalize_folder(raw)


@pytest.mark.parametrize("raw", [
    "/my-files/" + UID_22 + "~" + UID_22,
    "/my-files/ok/" + UID_88 + "~" + UID_22,
    "/shared-with-me/" + UID_22 + "~" + UID_88,
    "/my-files/" + UID_88 + "~" + UID_88,
    "/my-files/" + "a-_=" * 22 + "~" + "b" * 22,
])
def test_normalize_folder_rejects_node_uid_segments(raw):
    """The CLI would resolve such a segment as a node UID, escaping the folder (R-3)."""
    with pytest.raises(ValueError):
        normalize_folder(raw)


@pytest.mark.parametrize("raw", [
    "/my-files/" + "a" * 21 + "~" + "b" * 22,    # one short of the UID shape
    "/my-files/" + "a" * 22 + "~",              # no second half
    "/my-files/" + "a" * 22,                    # no tilde
    "/my-files/" + "a" * 23 + "~" + "b" * 22,   # between the two accepted lengths
    "/my-files/a=b~c",
])
def test_normalize_folder_accepts_near_miss_uid_shapes(raw):
    assert normalize_folder(raw) == raw


@pytest.mark.parametrize("raw,expected", [("/my-files/WoW Backups/", "/my-files/WoW Backups"),
    ("/my-files/a/b", "/my-files/a/b"), ("/shared-with-me/Backups", "/shared-with-me/Backups")])
def test_normalize_folder_accepts(raw, expected):
    assert normalize_folder(raw) == expected


@pytest.mark.parametrize("raw", [
    "/my-files/a//",              # only one trailing slash is stripped; the other leaves an empty segment
    "/my-files/a\x7fb",
    "/my-files/a\x00b",
    "/my-files/" + "é" * 128,     # 256 bytes
    "/my-files/\ud800",           # lone surrogate cannot be UTF-8 encoded
    "/My-Files/x",
    " /my-files/x",
])
def test_normalize_folder_rejects_more(raw):
    with pytest.raises(ValueError):
        normalize_folder(raw)


def test_normalize_folder_segment_limit_is_bytes_not_characters():
    assert normalize_folder("/my-files/" + "é" * 127) == "/my-files/" + "é" * 127
    assert normalize_folder("/my-files/" + "x" * 255) == "/my-files/" + "x" * 255


def test_normalize_folder_rejects_non_strings():
    with pytest.raises(ValueError):
        normalize_folder(None)  # type: ignore[arg-type]


def test_split_folder():
    assert split_folder("/my-files/a/b") == ("/my-files/a", "b")
    assert split_folder("/my-files/WoW Backups") == ("/my-files", "WoW Backups")


def test_config_rejects_unknown_label_bad_retention_and_enabled_without_folder():
    for raw in ({"labels": ["daily", "nightly"]}, {"retention_days": 0}, {"retention_days": 91},
                {"retention_days": "7"}, {"enabled": True, "folder": ""}):
        with pytest.raises(ValueError):
            CloudConfig.from_dict(raw).validate()


@pytest.mark.parametrize("raw", [
    {"retention_days": True},
    {"retention_days": False},
    {"retention_days": 7.0},
    {"retention_days": None},
    {"enabled": "yes"},
    {"enabled": 1},
    {"folder": 5},
    {"folder": "/photos/x"},
    {"labels": "daily"},
    {"labels": [1]},
    {"labels": None},
    [],
    "x",
    None,
])
def test_config_from_dict_rejects_bad_types(raw):
    with pytest.raises(ValueError):
        CloudConfig.from_dict(raw).validate()


def test_validate_rejects_directly_constructed_bad_values():
    for cfg in (CloudConfig(retention_days=True), CloudConfig(retention_days=0),
                CloudConfig(retention_days=91), CloudConfig(labels=("nightly",)),
                CloudConfig(folder="/photos/x"), CloudConfig(enabled=True)):
        with pytest.raises(ValueError):
            cfg.validate()
    CloudConfig(retention_days=RETENTION_MIN).validate()
    CloudConfig(retention_days=RETENTION_MAX).validate()


def test_config_from_dict_normalises_folder_and_orders_labels():
    cfg = CloudConfig.from_dict({
        "enabled": True,
        "folder": "/my-files/WoW Backups/",
        "labels": ["imported", "daily", "daily", "manual"],
        "retention_days": 30,
    })
    cfg.validate()
    assert cfg.folder == "/my-files/WoW Backups"
    assert cfg.labels == ("daily", "manual", "imported")
    assert cfg.retention_days == 30


def test_config_from_dict_ignores_unknown_keys_and_uses_defaults():
    assert CloudConfig.from_dict({"something_new": 1}) == CloudConfig()


def test_config_to_dict_is_json_ready_and_canonical():
    cfg = CloudConfig(enabled=True, folder="/my-files/x", labels=("manual", "daily", "daily"), retention_days=3)
    d = cfg.to_dict()
    assert d == {"enabled": True, "folder": "/my-files/x", "labels": ["daily", "manual"], "retention_days": 3}
    assert CloudConfig.from_dict(json.loads(json.dumps(d))) == CloudConfig(
        enabled=True, folder="/my-files/x", labels=("daily", "manual"), retention_days=3)


def test_empty_label_selection_is_allowed():
    cfg = CloudConfig.from_dict({"labels": []})
    cfg.validate()
    assert cfg.labels == ()


def test_save_config_validates_and_writes_nothing_on_error(tmp_path):
    store = CloudStore(tmp_path)
    with pytest.raises(ValueError):
        store.save_config(CloudConfig(enabled=True, folder=""))
    assert not (tmp_path / "cloud" / "config.json").exists()
    assert store.load_config() == CloudConfig()


def test_save_config_stores_canonical_form(tmp_path):
    store = CloudStore(tmp_path)
    store.save_config(CloudConfig(folder="/my-files/x/", labels=("manual", "daily")))
    raw = json.loads((tmp_path / "cloud" / "config.json").read_text())
    assert raw["folder"] == "/my-files/x" and raw["labels"] == ["daily", "manual"]


def test_config_with_invalid_values_on_disk_loads_as_defaults(tmp_path, caplog):
    root = tmp_path / "cloud"
    root.mkdir()
    (root / "config.json").write_text(json.dumps({"enabled": True, "folder": "/photos/x"}))
    with caplog.at_level("WARNING"):
        assert CloudStore(tmp_path).load_config() == CloudConfig()
    assert "config" in caplog.text


def test_saving_again_leaves_no_tmp_files_and_keeps_modes(tmp_path):
    store = CloudStore(tmp_path)
    store.save_config(CloudConfig())
    store.save_config(CloudConfig(retention_days=9))
    store.save_state(CloudState())
    assert sorted(p.name for p in (tmp_path / "cloud").iterdir()) == ["config.json", "state.json"]
    assert stat.S_IMODE((tmp_path / "cloud" / "state.json").stat().st_mode) == 0o600


def test_root_mode_is_fixed_even_if_it_already_exists_with_wider_perms(tmp_path):
    root = tmp_path / "cloud"
    root.mkdir(mode=0o755)
    root.chmod(0o755)
    CloudStore(tmp_path).save_config(CloudConfig())
    assert stat.S_IMODE(root.stat().st_mode) == 0o700


def test_store_paths(tmp_path):
    store = CloudStore(tmp_path)
    assert store.root == tmp_path / "cloud"
    assert store.secrets_dir == tmp_path / "cloud" / "secrets"


def test_store_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_DATA_DIR", str(tmp_path))
    assert store_from_env().root == tmp_path / "cloud"
    monkeypatch.delenv("ADMIN_DATA_DIR")
    assert str(store_from_env().root) == "/admin-data/cloud"


def test_corrupt_files_load_as_defaults(tmp_path):
    root = tmp_path / "cloud"; root.mkdir()
    (root / "config.json").write_text("{nope"); (root / "state.json").write_text("[]")
    store = CloudStore(tmp_path)
    assert store.load_config() == CloudConfig() and store.load_state() == CloudState()


def test_state_defaults():
    st = CloudState()
    assert st.connection == "disconnected" and st.folder_status is None
    assert st.remote == [] and st.issues == []
    assert CloudState().remote is not CloudState().remote  # no shared mutable defaults


def test_state_round_trips_with_remote_files_rebuilt(tmp_path):
    store = CloudStore(tmp_path)
    state = CloudState(
        connection="connected",
        last_attempt="2026-10-28T03:00:00Z",
        last_success="2026-10-28T03:00:09Z",
        last_result="ok",
        last_error=None,
        next_auto_attempt="2026-10-29T03:00:00Z",
        folder_status="ok",
        remote=[RemoteFile("azerothcore-backup-daily-20261028.tar.gz", 1234, "ab" * 20),
                RemoteFile("other.txt", None, None)],
        remote_listed_at="2026-10-28T03:00:09Z",
        issues=[{"name": "x.tar.gz", "kind": "conflict", "detail": "already exists"}],
        cli_version="0.9.0",
    )
    store.save_state(state)
    loaded = store.load_state()
    assert loaded == state
    assert all(isinstance(r, RemoteFile) for r in loaded.remote)
    assert stat.S_IMODE((tmp_path / "cloud" / "state.json").stat().st_mode) == 0o600


def test_remote_entry_without_size_or_sha1_loads_with_none(tmp_path):
    root = tmp_path / "cloud"; root.mkdir()
    (root / "state.json").write_text(json.dumps({"remote": [{"name": "a"}]}))
    assert CloudStore(tmp_path).load_state().remote == [RemoteFile("a", None, None)]


def test_state_with_unknown_keys_loads_ignoring_them(tmp_path):
    root = tmp_path / "cloud"; root.mkdir()
    (root / "state.json").write_text(json.dumps({"connection": "connected", "future": 1}))
    assert CloudStore(tmp_path).load_state() == CloudState(connection="connected")


@pytest.mark.parametrize("raw", [
    {"connection": "weird"},
    {"folder_status": "gone"},
    {"remote": "x"},
    {"remote": ["x"]},
    {"remote": [{"name": 1, "size": None, "sha1": None}]},
    {"remote": [{"name": "a", "size": "big", "sha1": None}]},
    {"remote": [{"name": "a", "size": True, "sha1": None}]},
    {"issues": [{"name": "a", "kind": "boom", "detail": ""}]},
    {"issues": [{"name": "a", "kind": "failed"}]},
    {"issues": "x"},
    {"last_success": 5},
    {"cli_version": ["0.9"]},
])
def test_invalid_state_loads_as_defaults_with_warning(tmp_path, caplog, raw):
    root = tmp_path / "cloud"; root.mkdir()
    (root / "state.json").write_text(json.dumps(raw))
    with caplog.at_level("WARNING"):
        assert CloudStore(tmp_path).load_state() == CloudState()
    assert "state" in caplog.text


def test_state_file_that_is_not_json_loads_as_defaults(tmp_path):
    root = tmp_path / "cloud"; root.mkdir()
    (root / "state.json").write_bytes(b"\xff\xfe\x00")
    assert CloudStore(tmp_path).load_state() == CloudState()


def test_missing_files_load_as_defaults_without_creating_anything(tmp_path):
    store = CloudStore(tmp_path)
    assert store.load_state() == CloudState() and store.read_log() == []
    assert not (tmp_path / "cloud").exists()


def test_log_is_trimmed_to_limit_and_newest_first(tmp_path):
    store = CloudStore(tmp_path)
    for i in range(25):
        store.append_log(CloudLogEntry(f"t{i}", f"t{i}", "auto", "ok", [], [], [], [], None))
    entries = store.read_log()
    assert len(entries) == 20 and entries[0].started == "t24"


def test_log_entries_round_trip_and_file_is_private(tmp_path):
    store = CloudStore(tmp_path)
    entry = CloudLogEntry(
        started="2026-10-28T03:00:00Z", finished="2026-10-28T03:00:09Z", trigger="manual",
        result="warning", uploaded=["a.tar.gz"], conflicts=["b.tar.gz"], failed=["c.tar.gz"],
        trashed=["d.tar.gz"], error="quota",
    )
    store.append_log(entry)
    assert store.read_log() == [entry]
    assert stat.S_IMODE((tmp_path / "cloud" / "log.jsonl").stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "cloud").stat().st_mode) == 0o700


def test_log_limit_argument_is_honoured(tmp_path):
    store = CloudStore(tmp_path)
    for i in range(5):
        store.append_log(CloudLogEntry(f"t{i}", f"t{i}", "auto", "ok", [], [], [], [], None), limit=3)
    assert [e.started for e in store.read_log()] == ["t4", "t3", "t2"]


def test_bad_log_lines_are_skipped(tmp_path):
    root = tmp_path / "cloud"; root.mkdir()
    good = json.dumps({"started": "a", "finished": "b", "trigger": "auto", "result": "ok",
                       "uploaded": [], "conflicts": [], "failed": [], "trashed": [], "error": None})
    (root / "log.jsonl").write_text("\n".join([
        "{nope", good, json.dumps({"started": "only"}), json.dumps(["x"]),
        json.dumps({"started": "a", "finished": "b", "trigger": "auto", "result": "ok",
                    "uploaded": "oops", "conflicts": [], "failed": [], "trashed": [], "error": None}),
        "",
    ]) + "\n")
    entries = CloudStore(tmp_path).read_log()
    assert len(entries) == 1 and entries[0].started == "a"
