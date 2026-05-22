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
