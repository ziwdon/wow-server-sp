"""How the dashboard shows a world server that is not running.

The world server no longer starts at boot, so "not running" is a normal state:
the status must not read as an error, and the log tabs (truncated only by the
next world-server start) must say they hold the previous session.
"""

import datetime as dt
import os
import re
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import logs as logs_svc
from app.services.docker_client import ContainerInfo
from app.services.server_display import status_display


@pytest.mark.parametrize(
    ("status", "exit_code", "expected"),
    [
        ("running", 0, ("RUNNING", "running")),
        ("missing", None, ("NOT STARTED", "stopped")),
        ("created", None, ("NOT STARTED", "stopped")),
        ("exited", 0, ("STOPPED", "stopped")),
        ("exited", 137, ("STOPPED", "failed")),
        ("exited", None, ("STOPPED", "failed")),
        ("dead", 1, ("STOPPED", "failed")),
        ("restarting", None, ("RESTARTING", "restarting")),
        ("paused", 0, ("PAUSED", "paused")),
        ("unknown", None, ("UNKNOWN", "unknown")),
    ],
)
def test_status_display_labels_each_container_state(status, exit_code, expected):
    assert status_display(status, exit_code) == expected


def _status_body(info):
    with patch("app.main.docker_client.inspect_worldserver", return_value=info):
        resp = TestClient(app).get("/api/status")
    assert resp.status_code == 200
    return resp.text


def _stat_value(body):
    match = re.search(r'<div class="stat-value ([^"]+)">([^<]*)</div>', body)
    assert match, body
    return match.group(1), match.group(2)


def test_api_status_shows_a_removed_container_as_not_started_in_grey():
    body = _status_body(ContainerInfo("missing", None, None, None))

    assert _stat_value(body) == ("status-stopped", "NOT STARTED")
    assert "MISSING" not in body


def test_api_status_shows_a_clean_stop_as_stopped_in_grey():
    body = _status_body(ContainerInfo("exited", "2026-11-11T11:46:45Z", 0, None))

    assert _stat_value(body) == ("status-stopped", "STOPPED")
    assert "Last exit: 0" in body


def test_api_status_keeps_a_non_zero_exit_red():
    body = _status_body(ContainerInfo("exited", "2026-11-11T11:46:45Z", 137, None))

    assert _stat_value(body) == ("status-failed", "STOPPED")
    assert "Last exit: 137" in body


def test_api_status_running_is_unchanged():
    body = _status_body(ContainerInfo("running", "2026-11-11T23:18:28Z", 0, None))

    assert _stat_value(body) == ("status-running", "RUNNING")


def test_stopped_and_failed_tones_are_styled_on_the_card_and_the_nav_pill():
    css = (Path(__file__).resolve().parents[1] / "app" / "static" / "app.css").read_text()

    for selector in (
        ".stat-value.status-stopped",
        ".stat-value.status-failed",
        ".status-pill.status-stopped",
        ".status-pill.status-failed",
    ):
        assert selector in css


def _touch(path, when):
    path.write_text("line\n")
    os.utime(path, (when, when))


def test_last_written_is_the_newest_mtime_of_the_existing_files(tmp_path):
    _touch(tmp_path / "Server.log", 1_000)
    _touch(tmp_path / "Playerbots.log", 3_000)
    _touch(tmp_path / "Errors.log", 2_000)

    paths = [tmp_path / n for n in ("Server.log", "Playerbots.log", "Errors.log", "Gone.log")]

    assert logs_svc.last_written(paths) == 3_000


def test_last_written_is_none_when_no_file_exists(tmp_path):
    assert logs_svc.last_written([tmp_path / "Server.log"]) is None


STOP_TIME = dt.datetime(2026, 11, 11, 22, 4, 25, tzinfo=dt.timezone.utc).timestamp()
NOTICE = "World server is not running"


def _logs_body(monkeypatch, tmp_path, *, inspect, write_logs=True):
    logs = tmp_path / "logs"
    logs.mkdir()
    if write_logs:
        _touch(logs / "Server.log", STOP_TIME - 60)
        _touch(logs / "Playerbots.log", STOP_TIME)
        _touch(logs / "Errors.log", STOP_TIME - 120)
    monkeypatch.setenv("AC_STACK_DIR", str(tmp_path))
    with patch("app.main.docker_client.inspect_worldserver", **inspect), \
         patch("app.main.app_events.events.snapshot", return_value=()):
        resp = TestClient(app).get("/api/logs")
    assert resp.status_code == 200
    return resp.text


@pytest.mark.parametrize("status", ["missing", "exited", "created", "dead"])
def test_api_logs_says_the_logs_are_from_the_last_session_when_not_running(
    monkeypatch, tmp_path, status
):
    body = _logs_body(
        monkeypatch, tmp_path,
        inspect={"return_value": ContainerInfo(status, None, 0, None)},
    )

    notice_at = body.index(NOTICE)
    assert "These logs are from its last session, last written 2026-11-11 22:04 UTC." in body
    assert "They clear on the next Start." in body
    assert notice_at < body.index('class="log-tabs"')


def test_api_logs_has_no_notice_while_the_world_server_runs(monkeypatch, tmp_path):
    body = _logs_body(
        monkeypatch, tmp_path,
        inspect={"return_value": ContainerInfo("running", "2026-11-11T23:18:28Z", 0, None)},
    )

    assert NOTICE not in body


def test_api_logs_has_no_notice_when_docker_cannot_be_asked(monkeypatch, tmp_path):
    body = _logs_body(
        monkeypatch, tmp_path,
        inspect={"return_value": ContainerInfo("unknown", None, None, None)},
    )

    assert NOTICE not in body


def test_api_logs_has_no_notice_when_inspecting_raises(monkeypatch, tmp_path):
    body = _logs_body(
        monkeypatch, tmp_path,
        inspect={"side_effect": RuntimeError("no docker")},
    )

    assert NOTICE not in body


def test_api_logs_has_no_notice_when_there_are_no_log_files(monkeypatch, tmp_path):
    body = _logs_body(
        monkeypatch, tmp_path,
        inspect={"return_value": ContainerInfo("missing", None, None, None)},
        write_logs=False,
    )

    assert NOTICE not in body
