"""Tail AC log files with the known-benign noise patterns from CLAUDE.md filtered out."""

from __future__ import annotations

import re
from pathlib import Path

BENIGN_PATTERNS = [
    re.compile(r"mysql: \[Warning\] Using a password on the command line"),
    re.compile(r"Can't set process priority class, error: Permission denied"),
    re.compile(
        r"MoveSplineInitArgs::Validate: expression 'velocity > 0\.01f' failed for GUID "
    ),
    re.compile(
        r">> The file '\d{4}_\d{2}_\d{2}_\d{2}\.sql' was applied to the database, "
        r"but is missing in your update directory now!"
    ),
    re.compile(r"A:[^ ]+ - FAILED"),
    re.compile(r"Can cast spell failed\. No spellid\. - spellid: 0, bot name:"),
    re.compile(r"Random teleporting bot .* \(level \d+\) to Map:"),
]


def _is_benign(line: str) -> bool:
    return any(p.search(line) for p in BENIGN_PATTERNS)


def _decode_tail(chunks: list[bytes], offset: int) -> list[str]:
    data = b"".join(reversed(chunks))
    lines = data.decode("utf-8", errors="replace").splitlines()
    if offset > 0 and lines:
        return lines[1:]
    return lines


def tail_filtered(
    path: Path,
    n: int = 20,
    *,
    chunk_size: int = 8192,
    max_bytes: int = 1024 * 1024,
) -> list[str]:
    """Return up to n trailing non-benign lines without scanning huge logs."""
    if not path.exists():
        return []

    chunks: list[bytes] = []
    bytes_read = 0
    with path.open("rb") as f:
        f.seek(0, 2)
        offset = f.tell()

        while offset > 0 and bytes_read < max_bytes:
            read_size = min(chunk_size, offset, max_bytes - bytes_read)
            offset -= read_size
            f.seek(offset)
            chunks.append(f.read(read_size))
            bytes_read += read_size

            lines = _decode_tail(chunks, offset)
            keep = [line for line in lines if not _is_benign(line)]
            if len(keep) >= n:
                return keep[-n:]

    lines = _decode_tail(chunks, 0)
    keep = [line for line in lines if not _is_benign(line)]
    return keep[-n:]


def file_size(path: Path) -> int:
    if not path.exists():
        return 0
    return path.stat().st_size
