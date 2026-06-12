from fastapi.testclient import TestClient


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("AC_STACK_DIR", str(tmp_path))
    monkeypatch.setenv("ADMIN_DATA_DIR", str(tmp_path / "admin-data"))
    (tmp_path / "backups").mkdir()
    (tmp_path / "logs").mkdir()
    dist = tmp_path / "dist"
    dist.mkdir()
    for name in [
        "worldserver.conf.dist",
        "playerbots.conf.dist",
        "mod_ahbot.conf.dist",
        "individualProgression.conf.dist",
    ]:
        (dist / name).write_text("")
    from app.state import init_state

    init_state(
        dist_dir=dist,
        admin_yml=tmp_path / "docker-compose.admin.yml",
        override_yml=tmp_path / "docker-compose.override.yml",
        configs_dir=tmp_path / "configs",
        snapshots_dir=tmp_path / "snap",
    )
    from app.main import app

    return TestClient(app)


def test_maintenance_page_renders_utc_controls(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)

    resp = client.get("/maintenance")

    assert resp.status_code == 200
    assert "Scheduled Restart" in resp.text
    assert "Stop / Start Window" in resp.text
    assert "UTC" in resp.text
    assert 'name="restart_hour_utc"' in resp.text
    assert 'name="window_stop_hour_utc"' in resp.text
    assert 'name="window_start_hour_utc"' in resp.text


def test_maintenance_nav_is_between_settings_and_backups(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)

    body = client.get("/").text

    assert body.index('href="/settings"') < body.index('href="/maintenance"')
    assert body.index('href="/maintenance"') < body.index('href="/backups"')


def test_maintenance_api_returns_config_and_log(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)

    resp = client.get("/api/maintenance")

    assert resp.status_code == 200
    data = resp.json()
    assert data["config"]["restart_enabled"] is False
    assert data["config"]["restart_hour_utc"] == 4
    assert data["log"] == []


def test_maintenance_post_persists_config(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)

    resp = client.post(
        "/api/maintenance",
        data={
            "restart_enabled": "on",
            "restart_hour_utc": "5",
            "window_enabled": "on",
            "window_stop_hour_utc": "6",
            "window_start_hour_utc": "7",
        },
        follow_redirects=False,
    )

    assert resp.status_code == 303
    cfg = client.get("/api/maintenance").json()["config"]
    assert cfg["restart_enabled"] is True
    assert cfg["restart_hour_utc"] == 5
    assert cfg["window_enabled"] is True
    assert cfg["window_stop_hour_utc"] == 6
    assert cfg["window_start_hour_utc"] == 7


def test_maintenance_post_rejects_invalid_window(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)

    resp = client.post(
        "/api/maintenance",
        data={
            "window_enabled": "on",
            "restart_hour_utc": "4",
            "window_stop_hour_utc": "8",
            "window_start_hour_utc": "7",
        },
    )

    assert resp.status_code == 400
    assert "start hour must be after stop hour" in resp.json()["detail"]
