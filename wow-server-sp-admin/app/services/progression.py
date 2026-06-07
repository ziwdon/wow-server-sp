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


@dataclass(frozen=True)
class CharacterProgressionRow:
    guid: int
    account: str
    name: str
    class_id: int
    race_id: int
    level: int
    online: bool
    progression: int
    expansion: str


def _connect(*, host: str, port: int, user: str, password: str):
    return mysql.connector.connect(
        host=host,
        port=port,
        user=user,
        password=password,
        connection_timeout=2,
        autocommit=False,
    )


def _row_to_character(row) -> CharacterProgressionRow:
    guid, account, name, class_id, race_id, level, online, progression_state = row
    state = int(progression_state or 0)
    return CharacterProgressionRow(
        guid=int(guid),
        account=str(account),
        name=str(name),
        class_id=int(class_id),
        race_id=int(race_id),
        level=int(level),
        online=bool(online),
        progression=state,
        expansion=expansion_from_state(state),
    )


def collect_characters(*, host: str, port: int, user: str, password: str) -> tuple[CharacterProgressionRow, ...]:
    conn = _connect(host=host, port=port, user=user, password=password)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT c.guid, a.username, c.name, c.class, c.race, c.level, c.online, "
                "COALESCE(MAX(CASE WHEN q.quest BETWEEN 66001 AND 66013 AND q.active=1 "
                "THEN q.quest - 66000 END), 0) AS progression_state "
                "FROM acore_characters.characters c "
                "JOIN acore_auth.account a ON a.id = c.account "
                "LEFT JOIN acore_characters.character_queststatus_rewarded q ON q.guid = c.guid "
                f"WHERE {REAL_ACCOUNT_SQL} "
                "GROUP BY c.guid, a.username, c.name, c.class, c.race, c.level, c.online "
                "ORDER BY a.username ASC, c.name ASC"
            )
            return tuple(_row_to_character(r) for r in cur.fetchall())
    finally:
        try:
            conn.close()
        except Exception:
            pass


@dataclass(frozen=True)
class ApplyValidation:
    ok: bool
    target_state: int
    noop: bool = False
    reason: str | None = None
    message: str = ""


def validate_apply(
    row: CharacterProgressionRow,
    target_expansion: str,
    *,
    progression_limit: int,
    login_floor: int,
) -> ApplyValidation:
    target = target_state_for_expansion(target_expansion)
    if row.online:
        return ApplyValidation(False, target, reason="online", message="Character must log out before progression can be changed.")
    # Targets are always expansion boundaries (0/8/13), so "already in this
    # expansion" fully captures the no-op / no-downgrade-needed case. This MUST
    # be checked BEFORE the downgrade guard: a mid-tier character (e.g. state 9
    # in TBC) targeting its own expansion (8) must be a no-op, not a downgrade.
    if expansion_from_state(row.progression) == target_expansion:
        return ApplyValidation(True, target, noop=True, message="Character is already in that expansion.")
    if target < row.progression:
        return ApplyValidation(False, target, reason="downgrade", message="Moving characters backward is not supported.")
    if progression_limit and target > progression_limit:
        return ApplyValidation(False, target, reason="progression_limit", message="Target is above IndividualProgression.ProgressionLimit.")
    if login_floor and target < login_floor:
        return ApplyValidation(False, target, reason="login_floor", message="Module login rules would promote this character above the selected target.")
    return ApplyValidation(True, target, message="Progression can be applied.")
