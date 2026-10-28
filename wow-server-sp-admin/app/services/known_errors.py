"""Known-harmless Errors.log lines, so the dashboard can mark them instead of alarming.

Each pattern is shared verbatim with `KNOWN_ERRORS_LOG_PATTERNS` in
`scripts/verify-azerothcore.sh` (Check 16), which applies it with `grep -E`, so
it must mean the same thing as a POSIX ERE and as a Python regex: literals,
`[...]` classes, `+`/`*`, escaped `\\.`/`\\(`/`\\)` and `^`/`$` only.
`scripts/tests/test_known_errors_parity.py` keeps the two lists identical.
Patterns are exact wording with numeric ids, so a near-miss stays actionable.
Standard library only: the parity test imports this file by path.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class KnownError:
    key: str
    pattern: str
    title: str
    reason: str
    regex: re.Pattern[str] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "regex", re.compile(self.pattern))


KNOWN_ERRORS: tuple[KnownError, ...] = (
    KnownError(
        key="graveyard-zone",
        pattern=(
            r"Table .*graveyard_zone.* incomplete: Zone [0-9]+ Team [01] "
            r"does not have a linked graveyard"
        ),
        title="Zone without a linked graveyard",
        reason=(
            "Upstream data gap: AzerothCore links no graveyard for a few zones bots "
            "die in. The server falls back to the default graveyard."
        ),
    ),
    KnownError(
        key="empty-pool-1072",
        pattern=(
            r"^Pool Id 1072 is empty \(has no creatures, no gameobjects, no quests "
            r"and no valid child pools\)\. The pool will not be spawned\.$"
        ),
        title="Empty spawn pool 1072 (Mirelow)",
        reason=(
            "mod-individual-progression replaced Mirelow's spawn points with one "
            "patrolling Mirelow but left the empty pool behind. Mirelow still "
            "spawns; logged once per boot."
        ),
    ),
    KnownError(
        key="smart-talk-16878",
        pattern=(
            r"^SmartScript::ProcessAction: SMART_ACTION_TALK: EntryOrGuid 16878 "
            r"SourceType 0 EventType 61 TargetType 7 using non-existent Text id 0 "
            r"for talker [0-9]+, ignored\.$"
        ),
        title="Shattered Hand Berserker emote skipped",
        reason=(
            "Upstream SmartAI quirk: its enrage emote targets whoever triggered it. "
            "When that was a bot's summon (e.g. a Spirit Wolf), only the emote is "
            "skipped."
        ),
    ),
)


def match_known(line: str) -> KnownError | None:
    for entry in KNOWN_ERRORS:
        if entry.regex.search(line):
            return entry
    return None


@dataclass
class ErrorsLogSummary:
    size: int = 0
    too_large: bool = False
    actionable: int = 0
    known: list[tuple[KnownError, int]] = field(default_factory=list)

    @property
    def known_total(self) -> int:
        return sum(count for _, count in self.known)

    @property
    def state(self) -> str:
        if self.too_large or self.actionable:
            return "dirty"
        return "known" if self.known else "clean"


def summarize_errors_log(path: Path, max_bytes: int = 1024 * 1024) -> ErrorsLogSummary:
    """Classify every line of Errors.log; a file over max_bytes is dirty unread."""
    if not path.exists():
        return ErrorsLogSummary()
    with path.open("rb") as f:
        data = f.read(max_bytes + 1)
    size = max(path.stat().st_size, len(data))
    if len(data) > max_bytes:
        return ErrorsLogSummary(size=size, too_large=True)

    counts = {entry.key: 0 for entry in KNOWN_ERRORS}
    actionable = 0
    for line in data.decode("utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        entry = match_known(line)
        if entry is None:
            actionable += 1
        else:
            counts[entry.key] += 1
    known = [(entry, counts[entry.key]) for entry in KNOWN_ERRORS if counts[entry.key]]
    return ErrorsLogSummary(size=size, actionable=actionable, known=known)
