from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app
from app.state import init_state


def test_api_keys_returns_json_array(tmp_path, monkeypatch):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "worldserver.conf.dist").write_text(
        "#\n#    Foo.Enable - bool\n#\n\nFoo.Enable = 1\n"
    )
    (dist / "playerbots.conf.dist").write_text("")
    (dist / "mod_ahbot.conf.dist").write_text("")
    (dist / "individualProgression.conf.dist").write_text("")
    admin_yml = tmp_path / "docker-compose.admin.yml"
    admin_yml.write_text("services:\n  ac-worldserver:\n    environment: {}\n")
    override_yml = tmp_path / "docker-compose.override.yml"
    override_yml.write_text(
        "services:\n  ac-worldserver:\n    environment:\n      AC_FOO_ENABLE: '2'\n"
    )
    configs_dir = tmp_path / "configs"
    configs_dir.mkdir()

    snapshots_dir = tmp_path / "snapshots"
    snapshots_dir.mkdir()
    init_state(
        dist_dir=dist,
        admin_yml=admin_yml,
        override_yml=override_yml,
        configs_dir=configs_dir,
        snapshots_dir=snapshots_dir,
    )

    client = TestClient(app)
    r = client.get("/api/keys")
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, list)
    assert len(data) == 1
    assert data[0]["key"] == "Foo.Enable"
    assert data[0]["env_var"] == "AC_FOO_ENABLE"
    assert data[0]["effective_value"] == "2"
    assert data[0]["source"] == "installer"
    assert data[0]["default_value"] == "1"
    assert data[0]["default"] == "1"


def test_api_keys_marks_installer_managed_keys_read_only(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "worldserver.conf.dist").write_text(
        "#\n#    Foo.Enable\n#\n\nFoo.Enable = 1\n"
    )
    (dist / "playerbots.conf.dist").write_text("")
    (dist / "mod_ahbot.conf.dist").write_text(
        "#\n#    AuctionHouseBot.GUIDs\n#\n\nAuctionHouseBot.GUIDs = 0\n"
    )
    (dist / "individualProgression.conf.dist").write_text("")
    admin_yml = tmp_path / "docker-compose.admin.yml"
    admin_yml.write_text("services:\n  ac-worldserver:\n    environment: {}\n")
    override_yml = tmp_path / "docker-compose.override.yml"
    override_yml.write_text("services:\n  ac-worldserver:\n    environment: {}\n")
    configs_dir = tmp_path / "configs"
    configs_dir.mkdir()
    snapshots_dir = tmp_path / "snapshots"
    snapshots_dir.mkdir()
    init_state(
        dist_dir=dist,
        admin_yml=admin_yml,
        override_yml=override_yml,
        configs_dir=configs_dir,
        snapshots_dir=snapshots_dir,
    )

    client = TestClient(app)
    data = {row["key"]: row for row in client.get("/api/keys").json()}

    assert data["Foo.Enable"]["read_only"] is False
    assert data["Foo.Enable"]["read_only_reason"] == ""
    assert data["AuctionHouseBot.GUIDs"]["read_only"] is True
    assert data["AuctionHouseBot.GUIDs"]["read_only_reason"] == "installer-managed"
