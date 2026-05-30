from unittest.mock import MagicMock, patch

from app.services import actions
from app.services.actions import ActionResult
from app.services.docker_client import ContainerInfo


@patch("app.services.actions.WorldserverConsole")
@patch("app.services.actions.inspect_worldserver")
def test_reset_bots_sends_rndbot_init(mock_inspect, mock_console_cls):
    mock_inspect.return_value = ContainerInfo(
        status="running", started_at=None, exit_code=None, image=None
    )
    console = MagicMock()
    mock_console_cls.return_value.__enter__.return_value = console

    msgs = []
    result = actions.run_reset_bots(on_progress=lambda s, m: msgs.append((s, m)))

    assert result == ActionResult.OK
    mock_console_cls.assert_called_once_with(actions.WORLDSERVER)
    sent = [c.args[0] for c in console.send.call_args_list]
    assert sent == ["playerbot rndbot init"]
    # Completion-free copy (must not imply the re-roll itself finished).
    done = " ".join(m for _, m in msgs).lower()
    assert "command sent" in done
    assert "completed" not in done and "finished" not in done


@patch("app.services.actions.WorldserverConsole")
@patch("app.services.actions.inspect_worldserver")
def test_reset_bots_errors_when_not_running(mock_inspect, mock_console_cls):
    mock_inspect.return_value = ContainerInfo(
        status="exited", started_at=None, exit_code=0, image=None
    )
    msgs = []
    result = actions.run_reset_bots(on_progress=lambda s, m: msgs.append((s, m)))

    assert result == ActionResult.ERROR
    assert any("server must be running" in msg for _, msg in msgs)
    mock_console_cls.assert_not_called()


@patch("app.services.actions.WorldserverConsole")
@patch("app.services.actions.inspect_worldserver")
def test_reset_bots_errors_on_console_failure(mock_inspect, mock_console_cls):
    mock_inspect.return_value = ContainerInfo(
        status="running", started_at=None, exit_code=None, image=None
    )
    console = MagicMock()
    console.send.side_effect = RuntimeError("attach failed")
    mock_console_cls.return_value.__enter__.return_value = console

    msgs = []
    result = actions.run_reset_bots(on_progress=lambda s, m: msgs.append((s, m)))

    assert result == ActionResult.ERROR
    assert any("console error: attach failed" in msg for _, msg in msgs)


@patch("app.services.actions.run_start", return_value=ActionResult.OK)
@patch("app.services.actions.run_stop", return_value=ActionResult.OK)
@patch("app.services.backup.run_backup")
@patch("app.services.actions.subprocess.run")
@patch("app.services.actions.db_credentials", return_value={"password": "pw"})
def test_clear_bots_happy_path_order(
    mock_creds, mock_run, mock_backup, mock_stop, mock_start
):
    events = []
    mock_stop.side_effect = lambda **_: events.append("stop") or ActionResult.OK
    mock_backup.side_effect = (
        lambda *_, **__: events.append("backup")
        or type("R", (), {"ok": True, "archive": "s", "output": ""})()
    )
    mock_start.side_effect = lambda **_: events.append("start") or ActionResult.OK
    # subprocess.run is used for the SQL import + the 4 verify queries.
    def fake_run(cmd, **kwargs):
        events.append("import" if "-i" in cmd else "count")
        return MagicMock(returncode=0, stdout="0\n", stderr="")

    mock_run.side_effect = fake_run
    sql_file = MagicMock()

    with patch.object(actions, "CLEAR_SQL") as mock_sql:
        mock_sql.open.return_value.__enter__.return_value = sql_file
        result = actions.run_clear_bots(on_progress=lambda *_: None)

    assert result == ActionResult.OK
    mock_stop.assert_called_once()
    mock_start.assert_called_once()
    mock_sql.open.assert_called_once_with("rb")
    # Safety backup uses the preclear label.
    assert mock_backup.call_args.args[0] == "preclear"
    # The SQL import targets ac-database mysql.
    import_calls = [
        c for c in mock_run.call_args_list
        if c.args[0][:5] == ["docker", "exec", "-i", "ac-database", "mysql"]
    ]
    assert import_calls, "expected a docker exec ... mysql call"
    assert import_calls[0].kwargs["stdin"] is sql_file
    assert events == ["stop", "backup", "import", "count", "count", "count", "count", "start"]


@patch("app.services.actions.run_start", return_value=ActionResult.OK)
@patch("app.services.actions.run_stop", return_value=ActionResult.OK)
@patch("app.services.backup.run_backup")
@patch("app.services.actions.db_credentials", return_value={"password": "pw"})
def test_clear_bots_aborts_if_safety_backup_fails(
    mock_creds, mock_backup, mock_stop, mock_start
):
    mock_backup.return_value = type("R", (), {"ok": False, "archive": None, "output": ""})()
    result = actions.run_clear_bots(on_progress=lambda *_: None)
    assert result == ActionResult.ERROR
    mock_stop.assert_called_once()
    mock_start.assert_called_once()   # server brought back up after abort


@patch("app.services.actions.run_start", return_value=ActionResult.OK)
@patch("app.services.actions.run_stop", return_value=ActionResult.OK)
@patch("app.services.backup.run_backup")
@patch("app.services.actions.subprocess.run")
@patch("app.services.actions.db_credentials", return_value={"password": "pw"})
def test_clear_bots_aborts_if_sql_import_fails(
    mock_creds, mock_run, mock_backup, mock_stop, mock_start
):
    mock_backup.return_value = type("R", (), {"ok": True, "archive": "s", "output": ""})()
    mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="boom")
    sql_file = MagicMock()

    with patch.object(actions, "CLEAR_SQL") as mock_sql:
        mock_sql.open.return_value.__enter__.return_value = sql_file
        result = actions.run_clear_bots(on_progress=lambda *_: None)

    assert result == ActionResult.ERROR
    mock_start.assert_called_once()   # restarted after failed import


@patch("app.services.actions.run_start", return_value=ActionResult.OK)
@patch("app.services.actions.run_stop", return_value=ActionResult.OK)
@patch("app.services.backup.run_backup", side_effect=RuntimeError("backup exploded"))
def test_clear_bots_restarts_if_safety_backup_raises(
    mock_backup, mock_stop, mock_start
):
    msgs = []
    result = actions.run_clear_bots(on_progress=lambda s, m: msgs.append((s, m)))

    assert result == ActionResult.ERROR
    mock_stop.assert_called_once()
    mock_start.assert_called_once()
    assert any("clear failed: backup exploded" in msg for _, msg in msgs)


@patch("app.services.actions.subprocess.run", side_effect=OSError("docker missing"))
def test_count_query_returns_minus_one_on_subprocess_error(mock_run):
    assert actions._count_query("SELECT COUNT(*)", "pw") == -1


@patch("app.services.actions.subprocess.run")
def test_count_query_returns_minus_one_on_empty_output(mock_run):
    mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

    assert actions._count_query("SELECT COUNT(*)", "pw") == -1
