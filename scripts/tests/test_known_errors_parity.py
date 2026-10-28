"""The admin dashboard and verify-azerothcore.sh Check 16 must agree on known-harmless lines."""

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VERIFY = ROOT / "scripts" / "verify-azerothcore.sh"
KNOWN_ERRORS_PY = ROOT / "wow-server-sp-admin" / "app" / "services" / "known_errors.py"

SAMPLES = [
    "Table `graveyard_zone` incomplete: Zone 2037 Team 0 does not have a linked graveyard",
    "Table `graveyard_zone` incomplete: Zone 3455 Team 1 does not have a linked graveyard",
    "Table `graveyard_zone` incomplete: Zone 2037 Team 2 does not have a linked graveyard",
    "Pool Id 1072 is empty (has no creatures, no gameobjects, no quests and no valid child pools). The pool will not be spawned.",
    "Pool Id 1073 is empty (has no creatures, no gameobjects, no quests and no valid child pools). The pool will not be spawned.",
    "Pool Id abc is empty (has no creatures, no gameobjects, no quests and no valid child pools). The pool will not be spawned.",
    "Pool Id 1072 is empty (has no creatures, no gameobjects, no quests and no valid child pools). The pool will not be spawned. x",
    "SmartScript::ProcessAction: SMART_ACTION_TALK: EntryOrGuid 16878 SourceType 0 EventType 61 TargetType 7 using non-existent Text id 0 for talker 29264, ignored.",
    "SmartScript::ProcessAction: SMART_ACTION_TALK: EntryOrGuid 16878 SourceType 0 EventType 61 TargetType 7 using non-existent Text id 0 for talker 19668, ignored.",
    "SmartScript::ProcessAction: SMART_ACTION_TALK: EntryOrGuid 16879 SourceType 0 EventType 61 TargetType 7 using non-existent Text id 0 for talker 29264, ignored.",
    "SmartScript::ProcessAction: SMART_ACTION_TALK: EntryOrGuid 16878 SourceType 0 EventType 61 TargetType 7 using non-existent Text id 0 for talker , ignored.",
    "[ERROR] database connection lost",
]


def _python_entries():
    spec = importlib.util.spec_from_file_location("known_errors_under_test", KNOWN_ERRORS_PY)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(module)
    return [(entry.key, entry.pattern) for entry in module.KNOWN_ERRORS]


def _bash_entries():
    text = VERIFY.read_text()
    block = re.search(r"^KNOWN_ERRORS_LOG_PATTERNS=\(\n(.*?)^\)$", text, re.M | re.S)
    assert block, "KNOWN_ERRORS_LOG_PATTERNS array not found in verify-azerothcore.sh"
    entries = []
    for raw in block.group(1).splitlines():
        raw = raw.strip()
        if not raw or raw.startswith("#"):
            continue
        assert raw.startswith("'") and raw.endswith("'") and raw.count("'") == 2, raw
        key, _, pattern = raw[1:-1].partition("|")
        entries.append((key, pattern))
    return entries


def test_dashboard_and_verifier_share_the_same_known_lines():
    assert _bash_entries() == _python_entries()


def test_each_pattern_matches_the_same_lines_in_grep_and_python():
    for key, pattern in _python_entries():
        result = subprocess.run(
            ["grep", "-E", "-e", pattern],
            input="\n".join(SAMPLES) + "\n",
            capture_output=True,
            text=True,
        )
        assert result.returncode in (0, 1), result.stderr
        grep_hits = result.stdout.splitlines()
        python_hits = [line for line in SAMPLES if re.search(pattern, line)]
        assert grep_hits == python_hits, key
        assert python_hits, f"{key} matches none of the samples"
