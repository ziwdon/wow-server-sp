from pathlib import Path


ADMIN_ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (ADMIN_ROOT / rel).read_text()


def test_action_request_helper_reports_http_network_and_invalid_json_failures():
    script = _read("app/static/action-ui.js")

    assert "async function requestActionJson" in script
    assert "await response.text()" in script
    assert "JSON.parse(text)" in script
    assert "kind: 'network'" in script
    assert "kind: 'invalid-json'" in script
    assert "if (!response.ok)" in script
    assert "HTTP " in script
    assert "Try again" in script


def test_action_request_helper_handles_null_json_error_bodies_without_throwing():
    script = _read("app/static/action-ui.js")

    # JSON.parse("null") is valid, so HTTP error handling must not dereference
    # data before confirming that it is an object.
    assert "data && typeof data.detail === 'string'" in script


def test_backup_actions_clear_activity_only_after_an_action_id_is_accepted():
    script = _read("app/static/backups.js")

    assert "async function requestBackupAction" in script
    assert "window.requestActionJson(url, options)" in script
    assert "'/api/action/restore'" in script
    assert "'/api/action/import-restore'" in script
    assert "data-action-endpoint" in script
    assert "if (!result.ok)" in script
    assert "if (!result.data || !result.data.id)" in script
    assert "clearActionLog();" in script
    assert script.index("if (!result.ok)") < script.index("clearActionLog();")
    assert "button.disabled = true" in script
    assert "button.disabled = false" in script


def test_dashboard_and_stats_surface_failed_or_disconnected_action_requests():
    dashboard = _read("app/templates/dashboard.html")
    stats = _read("app/static/stats.js")
    action_ui = _read("app/static/action-ui.js")

    assert "data-action-endpoint" in dashboard
    assert "window.requestActionJson" in dashboard
    assert "window.requestActionJson('/api/stats/refresh'" in stats
    assert "refresh failed" in stats
    assert "htmx:sseError" in action_ui
    assert "Refresh the page to reconnect" in action_ui


def test_settings_reuses_the_shared_request_parser_for_apply_and_rollback():
    script = _read("app/static/settings.js")

    assert script.count("window.requestActionJson(") >= 2
    assert "window.showActionFailure('Apply', result)" in script
    assert "window.showActionFailure('Rollback', result)" in script
