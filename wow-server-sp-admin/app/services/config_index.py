"""Parse AzerothCore `.conf.dist` files into structured key entries."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from app.services.env_var import config_key_to_ac_env_var

_KV_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_.]*)\s*=\s*(.*?)\s*$")
_COMMENT_KEY_RE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9_.]*)\s*$")
_BOOL_KEY_RE = re.compile(
    r"(^|[._])(enable|enabled|disable|disabled|logininfo)([._]|$)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class KeyEntry:
    key: str
    default: str
    inferred_type: str  # 'int' | 'float' | 'bool' | 'string'
    comment: str
    source_file: str
    line_number: int
    env_var: str


def _infer_type(key: str, default: str, comment: str = "") -> str:
    normalized = default.strip().lower()
    comment_lower = comment.lower()
    key_lower = key.lower()

    if normalized in {"true", "false"}:
        return "bool"

    if normalized in {"0", "1"}:
        if _BOOL_KEY_RE.search(key) or (
            "enabled" in comment_lower and "disabled" in comment_lower
        ):
            return "bool"
        if (
            "comma" in comment_lower
            or "separated" in comment_lower
            or key_lower.endswith("guids")
        ):
            return "string"

    if key_lower.startswith("rate.") or ".rate" in key_lower:
        try:
            float(default)
            return "float"
        except ValueError:
            pass

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


def _is_comment_divider(stripped: str) -> bool:
    return (
        stripped.startswith("#")
        and stripped.count("#") >= 3
        and stripped.lstrip("#").strip() == ""
    )


def _comment_content(stripped: str) -> str:
    content = stripped.lstrip("#")
    if content.startswith(" "):
        content = content[1:]
    return content


def _comments_by_key(lines: list[str]) -> dict[str, str]:
    """Split a grouped documentation block into key-specific comments."""
    comments: dict[str, str] = {}
    current_keys: list[str] = []
    section_start: int | None = None
    saw_body = False

    def flush(end: int) -> None:
        if section_start is None or not current_keys:
            return
        text = "\n".join(lines[section_start:end]).strip()
        if not text:
            return
        for current_key in current_keys:
            comments[current_key] = text

    for index, line in enumerate(lines):
        match = _COMMENT_KEY_RE.match(line)
        if match:
            if current_keys and saw_body:
                flush(index)
                current_keys = [match.group(1)]
                section_start = index
                saw_body = False
            else:
                if section_start is None:
                    section_start = index
                current_keys.append(match.group(1))
            continue
        if current_keys and line.strip():
            saw_body = True

    flush(len(lines))
    return comments


def parse_dist_file(path: Path) -> list[KeyEntry]:
    """Read a `.conf.dist`, return a list of KeyEntry in file order."""
    entries: list[KeyEntry] = []
    pending_comment: list[str] = []
    last_comment_block: list[str] = []
    active_comment: list[str] = []
    active_comment_by_key: dict[str, str] = {}

    for lineno, raw in enumerate(path.read_text().splitlines(), start=1):
        line = raw.rstrip()
        stripped = line.lstrip()

        if not stripped:
            continue

        if stripped.startswith("#"):
            if _is_comment_divider(stripped):
                if pending_comment:
                    last_comment_block = pending_comment
                    pending_comment = []
                continue
            pending_comment.append(_comment_content(stripped))
            continue

        match = _KV_RE.match(stripped)
        if not match:
            pending_comment = []
            last_comment_block = []
            active_comment = []
            active_comment_by_key = {}
            continue

        key, raw_value = match.groups()
        # Strip surrounding double quotes if present.
        value = raw_value
        if value.startswith('"') and value.endswith('"') and len(value) >= 2:
            value = value[1:-1]

        if pending_comment or last_comment_block:
            active_comment = pending_comment or last_comment_block
            active_comment_by_key = _comments_by_key(active_comment)
            pending_comment = []
            last_comment_block = []

        if active_comment_by_key:
            comment_text = active_comment_by_key.get(key, "")
            if not comment_text and "." in key:
                # Comment block used the short name (e.g. "BotActiveAlone") but the
                # config key has a dotted prefix ("AiPlayerbot.BotActiveAlone").
                comment_text = active_comment_by_key.get(key.rsplit(".", 1)[1], "")
        else:
            comment_text = "\n".join(active_comment).strip()
            # Do NOT clear active_comment here. Consecutive KV lines that share
            # the same flat comment block (no per-key split) should all inherit it.
            # active_comment is reset naturally when pending_comment or
            # last_comment_block fires on the next distinct comment section.

        entries.append(
            KeyEntry(
                key=key,
                default=value,
                inferred_type=_infer_type(key, value, comment_text),
                comment=comment_text,
                source_file=path.name,
                line_number=lineno,
                env_var=config_key_to_ac_env_var(key),
            )
        )

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
    missing = [name for name in DIST_FILE_NAMES if not (dist_dir / name).exists()]
    if missing:
        raise FileNotFoundError(
            "missing required dist files in "
            f"{dist_dir}: {', '.join(missing)}"
        )

    index: dict[str, KeyEntry] = {}
    for name in DIST_FILE_NAMES:
        path = dist_dir / name
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
