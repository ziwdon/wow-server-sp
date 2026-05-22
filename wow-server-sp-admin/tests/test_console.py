import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

from app.services.console import DETACH_BYTES, WorldserverConsole, format_command
import app.services.console as console_mod


def test_format_appends_newline():
    assert format_command("saveall") == b"saveall\n"


def test_format_strips_internal_newlines():
    # A command must be a single line; sanitize the user-supplied text.
    assert format_command("announce hello\nworld") == b"announce hello world\n"


def test_detach_bytes_are_ctrl_p_ctrl_q():
    assert DETACH_BYTES == b"\x10\x11"


@patch("app.services.console.subprocess.Popen")
@patch("app.services.console.time.sleep")
def test_console_uses_pty_stdin_for_tty_enabled_worldserver(
    mock_sleep, mock_popen, monkeypatch,
):
    mock_openpty = MagicMock(return_value=(10, 11))
    mock_setraw = MagicMock()
    mock_write = MagicMock()
    mock_close = MagicMock()
    monkeypatch.setattr(
        console_mod,
        "pty",
        SimpleNamespace(openpty=mock_openpty),
        raising=False,
    )
    monkeypatch.setattr(
        console_mod,
        "os",
        SimpleNamespace(write=mock_write, close=mock_close),
        raising=False,
    )
    monkeypatch.setattr(
        console_mod,
        "tty",
        SimpleNamespace(setraw=mock_setraw),
        raising=False,
    )
    mock_openpty.return_value = (10, 11)
    proc = MagicMock()
    proc.poll.return_value = None
    mock_popen.return_value = proc

    with WorldserverConsole("ac-worldserver") as console:
        console.send("saveall")

    mock_openpty.assert_called_once_with()
    mock_setraw.assert_called_once_with(11)
    popen_kwargs = mock_popen.call_args.kwargs
    assert popen_kwargs["stdin"] == 11
    assert popen_kwargs["stdin"] != subprocess.PIPE
    mock_close.assert_any_call(11)
    mock_close.assert_any_call(10)
    mock_sleep.assert_any_call(0.25)
    mock_sleep.assert_any_call(0.1)
    mock_write.assert_has_calls([
        call(10, b"saveall\n"),
        call(10, DETACH_BYTES),
    ])
