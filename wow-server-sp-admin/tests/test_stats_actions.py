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
