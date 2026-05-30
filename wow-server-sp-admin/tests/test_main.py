from app.main import _format_started_at
from unittest.mock import patch
from fastapi.testclient import TestClient
from app.main import app
from app.services.backups import BackupStatus


def test_format_started_at_nanosecond_docker_timestamp():
    result = _format_started_at("2026-05-19T19:31:05.518411494Z")
    assert result == "2026-05-19 19:31 UTC"


def test_format_started_at_none_returns_dash():
    assert _format_started_at(None) == "—"


def test_format_started_at_empty_string_returns_dash():
    assert _format_started_at("") == "—"


def test_format_started_at_bad_input_returns_dash():
    assert _format_started_at("not-a-timestamp") == "—"


def test_format_started_at_no_subseconds():
    result = _format_started_at("2026-05-19T19:31:05Z")
    assert result == "2026-05-19 19:31 UTC"


def test_api_backups_timestamp_includes_utc():
    # unix timestamp 1716144665 = 2024-05-19 18:51:05 UTC
    mock_status = BackupStatus(last_backup_unix=1716144665.0, last_error=None)
    with patch("app.main.backups_svc.backup_status", return_value=mock_status):
        client = TestClient(app)
        resp = client.get("/api/backups")
    assert resp.status_code == 200
    assert "UTC" in resp.text


def test_api_logs_requests_forty_lines():
    with patch("app.main.logs_svc.tail_filtered", return_value=[]) as mock_tail, \
         patch("app.main.logs_svc.file_size", return_value=0):
        client = TestClient(app)
        resp = client.get("/api/logs")
    assert resp.status_code == 200
    assert mock_tail.call_count == 3  # Server.log, Playerbots.log, Errors.log
    for c in mock_tail.call_args_list:
        assert c.kwargs["n"] == 40


def test_api_logs_errors_tab_present_and_dirty_when_errors_exist():
    with patch("app.main.logs_svc.tail_filtered", return_value=["an error line"]), \
         patch("app.main.logs_svc.file_size", return_value=172):
        client = TestClient(app)
        resp = client.get("/api/logs")
    assert resp.status_code == 200
    assert 'id="errors-log"' in resp.text
    assert "log-tab-dirty" in resp.text
    assert "Runtime errors detected" in resp.text


def test_api_logs_clean_bar_when_errors_log_empty():
    with patch("app.main.logs_svc.tail_filtered", return_value=[]), \
         patch("app.main.logs_svc.file_size", return_value=0):
        client = TestClient(app)
        resp = client.get("/api/logs")
    assert resp.status_code == 200
    assert "No runtime errors" in resp.text
    assert "log-tab-dirty" not in resp.text


def test_stats_page_renders_and_nav_between_dashboard_and_settings():
    client = TestClient(app)
    resp = client.get("/stats")
    assert resp.status_code == 200
    body = resp.text
    # Nav order: Dashboard, Stats, Settings.
    assert body.index('href="/stats"') < body.index('href="/settings"')
    assert body.index('href="/"') < body.index('href="/stats"')


def test_api_stats_refresh_returns_immediately():
    with patch("app.main.stats_refresher.refresh_async", return_value=True) as mock_ref, \
         patch("app.main.db_credentials", return_value={"host": "h", "port": 3306, "user": "u", "password": "p"}):
        client = TestClient(app)
        resp = client.post("/api/stats/refresh")
    assert resp.status_code == 200
    mock_ref.assert_called_once()


def test_api_stats_data_renders_with_snapshot():
    from app.services.stats import Bucket, StatsSnapshot
    snap = StatsSnapshot(
        fetched_at=1716144665.0, bots_total=2500, bots_online=200,
        players_total=3, players_online=1, ahbot_total=4, ahbot_online=0,
        bots_by_class=[Bucket("Warrior", 400)],
    )
    with patch("app.main.stats_refresher.get", return_value=snap), \
         patch("app.main.stats_refresher.is_stale", return_value=False):
        client = TestClient(app)
        resp = client.get("/api/stats/data")
    assert resp.status_code == 200
    assert "2500" in resp.text
    assert "Warrior" in resp.text


def test_existing_resources_card_endpoint_still_works():
    # /api/stats (CPU/mem card) must not collide with the new page endpoints.
    with patch("app.main.docker_client.inspect_worldserver") as mock_i, \
         patch("app.main.docker_client.stats_worldserver", return_value=None):
        mock_i.return_value = type("I", (), {"started_at": None})()
        client = TestClient(app)
        resp = client.get("/api/stats")
    assert resp.status_code == 200
