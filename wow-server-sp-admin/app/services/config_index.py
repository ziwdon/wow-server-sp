"""Parse AzerothCore `.conf.dist` files into structured key entries."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from app.services.env_var import config_key_to_ac_env_var

_KV_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_.]*)\s*=\s*(.*?)\s*$")


@dataclass(frozen=True)
class KeyEntry:
    key: str
    default: str
    inferred_type: str  # 'int' | 'float' | 'bool' | 'string'
    comment: str
    source_file: str
    line_number: int
    env_var: str


def _infer_type(default: str) -> str:
    try:
        int(default)
        return "int"
    except ValueError:
        pass
    try:
        float(default)
        return "float"
    except ValueError:
        pass
    return "string"


def parse_dist_file(path: Path) -> list[KeyEntry]:
    """Read a `.conf.dist`, return a list of KeyEntry in file order."""
    entries: list[KeyEntry] = []
    pending_comment: list[str] = []

    for lineno, raw in enumerate(path.read_text().splitlines(), start=1):
        line = raw.rstrip()
        stripped = line.lstrip()

        if not stripped:
            continue

        if stripped.startswith("#"):
            # Skip horizontal-rule dividers (### or more).
            if stripped.lstrip("#").strip() == "":
                continue
            # Capture the comment content (strip leading '#' + one space).
            content = stripped.lstrip("#")
            if content.startswith(" "):
                content = content[1:]
            pending_comment.append(content)
            continue

        match = _KV_RE.match(stripped)
        if not match:
            pending_comment = []
            continue

        key, raw_value = match.groups()
        # Strip surrounding double quotes if present.
        value = raw_value
        if value.startswith('"') and value.endswith('"') and len(value) >= 2:
            value = value[1:-1]

        comment_text = "\n".join(pending_comment).strip()
        entries.append(
            KeyEntry(
                key=key,
                default=value,
                inferred_type=_infer_type(value),
                comment=comment_text,
                source_file=path.name,
                line_number=lineno,
                env_var=config_key_to_ac_env_var(key),
            )
        )
        pending_comment = []

    return entries


DIST_FILE_NAMES = (
    "worldserver.conf.dist",
    "playerbots.conf.dist",
    "mod_ahbot.conf.dist",
    "individualProgression.conf.dist",
)


def build_key_index(dist_dir: Path) -> dict[str, KeyEntry]:
    """Return {key: KeyEntry} merged across all four dist files.

    If the same key appears in multiple files (rare), the first occurrence
    wins; emit a warning via the standard logging module.
    """
    import logging

    log = logging.getLogger(__name__)
    index: dict[str, KeyEntry] = {}
    for name in DIST_FILE_NAMES:
        path = dist_dir / name
        if not path.exists():
            log.warning("dist file missing: %s", path)
            continue
        for entry in parse_dist_file(path):
            if entry.key in index:
                log.warning(
                    "duplicate key %r in %s; first defined in %s",
                    entry.key,
                    entry.source_file,
                    index[entry.key].source_file,
                )
                continue
            index[entry.key] = entry
    return index
