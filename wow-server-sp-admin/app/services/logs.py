"""Tail AC log files with the known-benign noise patterns from CLAUDE.md filtered out."""

from __future__ import annotations

import re
from collections import deque
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


def tail_filtered(path: Path, n: int = 20) -> list[str]:
    """Return up to n trailing non-benign lines from path."""
    if not path.exists():
        return []
    keep: deque[str] = deque(maxlen=n)
    for raw in path.read_text(errors="replace").splitlines():
        if _is_benign(raw):
            continue
        keep.append(raw)
    return list(keep)


def file_size(path: Path) -> int:
    if not path.exists():
        return 0
    return path.stat().st_size
