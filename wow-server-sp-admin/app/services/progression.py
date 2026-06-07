"""Individual progression character controls.

mod-individual-progression stores completed progression as hidden rewarded
quests in acore_characters.character_queststatus_rewarded. Expansion starts:
Vanilla=0, TBC=8, WotLK=13.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

import mysql.connector

PROGRESSION_MIN = 1
PROGRESSION_MAX = 18
QUEST_BASE = 66000
TARGET_STATES = {
    "vanilla": 0,
    "tbc": 8,
    "wotlk": 13,
}
EXPANSION_LABELS = {
    "vanilla": "Vanilla",
    "tbc": "TBC",
    "wotlk": "WotLK",
}
REAL_ACCOUNT_SQL = "a.username NOT LIKE 'RNDBOT%%' AND a.username <> 'ahbot'"


def expansion_from_state(state: int) -> str:
    if state >= TARGET_STATES["wotlk"]:
        return "wotlk"
    if state >= TARGET_STATES["tbc"]:
        return "tbc"
    return "vanilla"


def target_state_for_expansion(expansion: str) -> int:
    try:
        return TARGET_STATES[expansion]
    except KeyError as e:
        raise ValueError(f"unknown expansion: {expansion}") from e
