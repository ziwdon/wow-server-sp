from app.services.console import format_command, DETACH_BYTES


def test_format_appends_newline():
    assert format_command("saveall") == b"saveall\n"


def test_format_strips_internal_newlines():
    # A command must be a single line; sanitize the user-supplied text.
    assert format_command("announce hello\nworld") == b"announce hello world\n"


def test_detach_bytes_are_ctrl_p_ctrl_q():
    assert DETACH_BYTES == b"\x10\x11"
