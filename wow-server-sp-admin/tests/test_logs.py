import pytest
from pathlib import Path

from app.services.logs import tail_filtered, BENIGN_PATTERNS


def test_tail_returns_last_n_lines(tmp_path):
    p = tmp_path / "x.log"
    p.write_text("\n".join(f"line {i}" for i in range(50)) + "\n")
    out = tail_filtered(p, n=5)
    assert out == ["line 45", "line 46", "line 47", "line 48", "line 49"]


def test_tail_filters_benign_patterns(tmp_path):
    p = tmp_path / "Server.log"
    content = [
        "real error: foo",
        "Can't set process priority class, error: Permission denied",
        "another real line",
        ">> The file '2026_05_20_01.sql' was applied to the database, but is missing in your update directory now!",
        "MoveSplineInitArgs::Validate: expression 'velocity > 0.01f' failed for GUID 12345",
        "final real line",
    ]
    p.write_text("\n".join(content) + "\n")
    out = tail_filtered(p, n=20)
    # Three benign lines filtered, three real lines kept.
    assert out == ["real error: foo", "another real line", "final real line"]


def test_tail_filtered_does_not_read_entire_file(tmp_path, monkeypatch):
    p = tmp_path / "Playerbots.log"
    p.write_text(
        "old important line\n"
        + "\n".join(f"benign filler {i} A:follow - FAILED" for i in range(2000))
        + "\nrecent line 1\nrecent line 2\n"
    )

    original_read_text = Path.read_text

    def fail_read_text(self, *args, **kwargs):
        if self == p:
            raise AssertionError("tail_filtered must not read the whole log file")
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_read_text)

    assert tail_filtered(p, n=2, max_bytes=4096) == ["recent line 1", "recent line 2"]


def test_tail_filtered_drops_partial_line_when_max_bytes_reached(tmp_path):
    p = tmp_path / "Server.log"
    tail_window = "should never be returned\nrecent complete line\n"
    p.write_text("truncated older line " + tail_window)

    assert tail_filtered(p, n=5, max_bytes=len(tail_window)) == [
        "recent complete line"
    ]


def test_tail_filtered_keeps_line_when_max_bytes_starts_at_boundary(tmp_path):
    p = tmp_path / "Server.log"
    tail_window = "first complete\nsecond complete\n"
    p.write_text("older\n" + tail_window)

    assert tail_filtered(p, n=5, max_bytes=len(tail_window)) == [
        "first complete",
        "second complete",
    ]


def test_tail_filtered_n_zero_returns_empty(tmp_path):
    p = tmp_path / "Server.log"
    p.write_text("line 1\nline 2\n")
    assert tail_filtered(p, n=0) == []


def test_tail_filtered_n_negative_raises(tmp_path):
    p = tmp_path / "Server.log"
    p.write_text("line 1\n")
    with pytest.raises(ValueError, match="non-negative"):
        tail_filtered(p, n=-1)


def test_tail_filtered_chunk_size_zero_raises(tmp_path):
    p = tmp_path / "Server.log"
    p.write_text("line 1\n")
    with pytest.raises(ValueError, match="positive"):
        tail_filtered(p, chunk_size=0)
