import ast
import datetime as dt
import asyncio
from pathlib import Path
import threading
import time

import app.main as main
from app.main import _format_started_at, _render_done, _render_progress
from app.services.runner import ActionRecord
from unittest.mock import patch
from fastapi.testclient import TestClient
from app.main import app
from app.services.backups import BackupStatus
from starlette.requests import Request
import pytest


def test_template_responses_use_the_request_first_signature():
    source = (Path(main.__file__).resolve()).read_text()
    calls = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "TemplateResponse"
    ]

    assert calls
    assert all(
        call.args
        and isinstance(call.args[0], ast.Name)
        and call.args[0].id == "request"
        for call in calls
    )


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


def test_render_progress_uses_event_timestamp():
    event_time = dt.datetime(2026, 6, 19, 5, 0, tzinfo=dt.timezone.utc)

    result = _render_progress("wait_init", "waiting", event_time)

    assert "[19 Jun 05:00]" in result


def test_render_done_is_a_list_item_for_the_shared_sse_activity_log():
    result = _render_done(ActionRecord(id="done", name="restart", status="ok"))

    assert result.startswith('<li class="action-done action-ok"')
    assert result.endswith("</li>")


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


def test_stats_player_card_keeps_its_polling_wrapper_after_a_swap():
    template = (Path(__file__).resolve().parents[1] / "app/templates/partials/stats_page.html").read_text()

    assert 'hx-get="/api/players"' in template
    assert 'hx-trigger="load, every 10s"' in template
    assert 'hx-swap="innerHTML"' in template


@pytest.mark.asyncio
async def test_blocked_online_count_keeps_liveness_and_sse_responsive_then_renders_unavailable():
    closed = threading.Event()

    def blocked_count(**_):
        try:
            time.sleep(0.2)
            raise TimeoutError("query timed out")
        finally:
            closed.set()

    request = Request({"type": "http", "method": "GET", "path": "/api/players", "headers": []})
    with patch("app.main.db_credentials", return_value={"host": "h", "port": 3306, "user": "u", "password": "p"}), \
         patch("app.main.db_stats.count_online", side_effect=blocked_count), \
         patch("app.main.runner.current", return_value=None), \
         patch("app.main.runner.last", return_value=None):
        started = time.monotonic()
        players_task = asyncio.create_task(main.api_players(request))
        await asyncio.sleep(0)
        health = await main.healthz()
        stream = await main.stream_action()
        heartbeat = await anext(stream.body_iterator)
        responsive_after = time.monotonic()
        players_response = await players_task

    assert responsive_after - started < 0.1
    assert health == {"status": "ok"}
    assert heartbeat["event"] == "heartbeat"
    assert "DB unreachable" in players_response.body.decode()
    assert closed.is_set()


def test_progression_page_renders_and_nav_between_stats_and_settings():
    client = TestClient(app)
    resp = client.get("/progression")
    assert resp.status_code == 200
    body = resp.text
    assert 'id="progression-data"' in body
    assert body.index('href="/stats"') < body.index('href="/progression"') < body.index('href="/settings"')
    assert 'class="nav-link active" href="/progression"' in body


def test_api_progression_apply_uses_service():
    from app.services.progression import ApplyProgressionResult, ProgressionConfig

    with patch("app.main.progression_svc.config_from_resolved_keys", return_value=ProgressionConfig()), \
         patch("app.main.list_keys_resolved", return_value=[]), \
         patch("app.main.db_credentials", return_value={"host": "h", "port": 3306, "user": "u", "password": "p"}), \
         patch("app.main.progression_svc.apply_progression", return_value=ApplyProgressionResult("applied", 8, 8)) as mock_apply:
        client = TestClient(app)
        resp = client.post("/api/progression/apply", json={"guid": 101, "target_expansion": "tbc"})

    assert resp.status_code == 200
    assert resp.json()["status"] == "applied"
    assert mock_apply.call_args.kwargs["guid"] == 101
    assert mock_apply.call_args.kwargs["target_expansion"] == "tbc"


def test_api_progression_apply_rejects_while_an_action_is_running():
    from app.services.progression import ApplyProgressionResult, ProgressionConfig

    with patch("app.main.runner.try_acquire_mutation", return_value=False) as acquire, \
         patch("app.main.progression_svc.config_from_resolved_keys", return_value=ProgressionConfig()), \
         patch("app.main.list_keys_resolved", return_value=[]), \
         patch("app.main.db_credentials", return_value={"host": "h", "port": 3306, "user": "u", "password": "p"}), \
         patch("app.main.progression_svc.apply_progression", return_value=ApplyProgressionResult("applied", 8, 8)) as apply:
        client = TestClient(app)
        resp = client.post("/api/progression/apply", json={"guid": 101, "target_expansion": "tbc"})

    assert resp.status_code == 409
    acquire.assert_called_once_with()
    apply.assert_not_called()


def test_api_stats_refresh_returns_immediately():
    with patch("app.main.stats_refresher.refresh_async", return_value=True) as mock_ref, \
         patch("app.main.db_credentials", return_value={"host": "h", "port": 3306, "user": "u", "password": "p"}):
        client = TestClient(app)
        resp = client.post("/api/stats/refresh")
    assert resp.status_code == 200
    mock_ref.assert_called_once()


def test_api_stats_data_renders_with_snapshot():
    from app.services.stats import Bucket, StatsSnapshot
    from app.services.players import PvpRankRow, RankRow
    snap = StatsSnapshot(
        fetched_at=1716144665.0, bots_total=2500, bots_online=200,
        players_total=3, players_online=1, ahbot_total=4, ahbot_online=0,
        bots_by_class=[Bucket("Warrior", 400)],
        top_pve=(RankRow(1, "Sariel", "Druid", "#FF7C0A", "Night Elf", "Alliance", "#4080C0", 80, 251),),
        top_pvp=(PvpRankRow(1, "Rndslayer", "Hunter", "#AAD372", "Orc", "Horde", "#C03030", 99, 1200),),
    )
    with patch("app.main.stats_refresher.get", return_value=snap), \
         patch("app.main.stats_refresher.is_stale", return_value=False):
        client = TestClient(app)
        resp = client.get("/api/stats/data")
    assert resp.status_code == 200
    assert "2500" in resp.text
    assert "Warrior" in resp.text
    assert "Top PvE" in resp.text
    assert "Top PvP" in resp.text
    assert "Sariel" in resp.text
    assert "Rndslayer" in resp.text
    assert resp.text.index("Top PvE") < resp.text.index("Online now")
    assert resp.text.index("Top PvP") < resp.text.index("Bot pool")


def test_api_stats_data_hides_stale_error_during_retry():
    with patch("app.main.stats_refresher.get", return_value=None), \
         patch("app.main.stats_refresher.is_stale", return_value=False), \
         patch.object(main.stats_refresher, "status", "refreshing"), \
         patch.object(main.stats_refresher, "error", "first failure"):
        client = TestClient(app)
        resp = client.get("/api/stats/data")

    assert resp.status_code == 200
    assert 'data-status="refreshing"' in resp.text
    assert "Last refresh failed" not in resp.text
    assert "first failure" not in resp.text


def test_api_stats_data_renders_latest_error_after_retry_failure():
    with patch("app.main.stats_refresher.get", return_value=None), \
         patch("app.main.stats_refresher.is_stale", return_value=False), \
         patch.object(main.stats_refresher, "status", "idle"), \
         patch.object(main.stats_refresher, "error", "latest failure"):
        client = TestClient(app)
        resp = client.get("/api/stats/data")

    assert resp.status_code == 200
    assert 'data-status="idle"' in resp.text
    assert "Last refresh failed: latest failure" in resp.text


def test_existing_resources_card_endpoint_still_works():
    # /api/stats (CPU/mem card) must not collide with the new page endpoints.
    with patch("app.main.docker_client.inspect_worldserver") as mock_i, \
         patch("app.main.docker_client.stats_worldserver", return_value=None):
        mock_i.return_value = type("I", (), {"started_at": None})()
        client = TestClient(app)
        resp = client.get("/api/stats")
    assert resp.status_code == 200


def test_reset_bots_route_kicks_action():
    with patch("app.main.runner.start") as mock_start:
        mock_start.return_value = type("R", (), {"id": "abc"})()
        client = TestClient(app)
        resp = client.post("/api/action/reset-bots")
    assert resp.status_code == 200
    assert resp.json()["id"] == "abc"
    assert mock_start.call_args.args[0] == "reset_bots"


def test_clear_bots_route_kicks_action():
    with patch("app.main.runner.start") as mock_start:
        mock_start.return_value = type("R", (), {"id": "def"})()
        client = TestClient(app)
        resp = client.post("/api/action/clear-bots")
    assert resp.status_code == 200
    assert resp.json()["id"] == "def"
    assert mock_start.call_args.args[0] == "clear_bots"
