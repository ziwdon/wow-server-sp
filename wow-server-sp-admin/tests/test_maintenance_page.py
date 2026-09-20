import re
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


def test_maintenance_page_and_api_warn_about_corrupt_saved_state(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    data_dir = tmp_path / "admin-data"
    data_dir.mkdir()
    (data_dir / "maintenance.json").write_text("{not json")

    page = client.get("/maintenance")
    api = client.get("/api/maintenance")

    assert page.status_code == 200
    assert "Maintenance state is corrupt" in page.text
    assert api.json()["diagnostic"] == (
        "Maintenance state is corrupt and was preserved as maintenance.json.corrupt. "
        "Save maintenance settings to repair it."
    )


def test_maintenance_post_repairs_corrupt_saved_state(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    data_dir = tmp_path / "admin-data"
    data_dir.mkdir()
    (data_dir / "maintenance.json").write_text("{not json")

    assert client.get("/api/maintenance").json()["diagnostic"] is not None
    resp = client.post(
        "/api/maintenance",
        data={
            "restart_enabled": "on",
            "restart_hour_utc": "5",
            "window_stop_hour_utc": "3",
            "window_start_hour_utc": "8",
        },
        follow_redirects=False,
    )

    assert resp.status_code == 303
    repaired = client.get("/api/maintenance").json()
    assert repaired["diagnostic"] is None
    assert repaired["config"]["restart_enabled"] is True


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

    assert resp.status_code == 200
    assert "start hour must be after stop hour" in resp.text


def test_maintenance_page_has_player_announcements_card_beside_bot_control(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)

    body = client.get("/maintenance").text

    assert "Player Announcements" in body
    assert 'id="presence-announce-toggle"' in body
    # Both cards share one grid row: Bot Control first, announcements to its right.
    grid_start = body.index('class="maintenance-grid maintenance-top-grid"')
    bot = body.index("Bot Control")
    presence = body.index("Player Announcements")
    assert grid_start < bot < presence
    assert "checked" not in body[body.index('id="presence-announce-toggle"') - 200:presence]


def test_presence_api_round_trips_toggle(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)

    assert client.get("/api/presence").json() == {"announce_enabled": False}

    resp = client.post("/api/presence", json={"announce_enabled": True})
    assert resp.status_code == 200
    assert resp.json() == {"announce_enabled": True}
    assert client.get("/api/presence").json() == {"announce_enabled": True}
    assert (tmp_path / "admin-data" / "presence.json").exists()

    body = client.get("/maintenance").text
    toggle = body.index('id="presence-announce-toggle"')
    assert "checked" in body[toggle:toggle + 200]


def test_presence_api_rejects_non_boolean(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)

    resp = client.post("/api/presence", json={"announce_enabled": "yes"})

    assert resp.status_code == 422


def test_lifespan_starts_presence_announcer(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    from app.services.presence import PresenceAnnouncer

    with client:
        announcer = client.app.state.presence_announcer
        assert isinstance(announcer, PresenceAnnouncer)
        assert announcer.interval_seconds == 15
        assert announcer.store.data_dir == tmp_path / "admin-data"


def test_bot_control_card_describes_reset_and_clear(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)

    body = client.get("/maintenance").text

    card_start = body.index("Bot Control")
    card_end = body.index("Player Announcements")
    card = body[card_start:card_end]

    reset_btn = card.index('id="reset-bots-btn"')
    clear_btn = card.index('id="clear-bots-btn"')
    reset_desc = card.index("playerbot rndbot init")
    clear_desc = card.index("permanently deletes every RNDBOT account")
    # Each button is immediately followed by its own explanation, inside the card.
    assert reset_btn < reset_desc < clear_btn < clear_desc
    assert "keeps its name, race and class" in card
    assert "Only bots currently logged in are affected" in card
    assert "No accounts or characters are deleted" in card
    assert "safety backup" in card
    text = re.sub(r"<[^>]+>", "", card)
    assert "Requires typing CLEAR to confirm" in text
    # Screen readers announce each description with its button.
    assert 'id="reset-bots-btn" aria-describedby="reset-bots-desc"' in card
    assert 'id="clear-bots-btn" aria-describedby="clear-bots-desc"' in card
    assert 'id="reset-bots-desc"' in card and 'id="clear-bots-desc"' in card
