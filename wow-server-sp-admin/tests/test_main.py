from app.main import _format_started_at


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
