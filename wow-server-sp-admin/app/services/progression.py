"""Individual progression character controls.

mod-individual-progression stores completed progression as hidden rewarded
quests in acore_characters.character_queststatus_rewarded. Expansion starts:
Vanilla=0, TBC=8, WotLK=13.
"""

from __future__ import annotations

import json
import logging
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
    "vanilla": "Classic",
    "tbc": "TBC",
    "wotlk": "WotLK",
}
EXPANSION_ICONS = {
    "vanilla": "classic",
    "tbc": "tbc",
    "wotlk": "wotlk",
}
REAL_ACCOUNT_SQL = "a.username NOT LIKE 'RNDBOT%%' AND a.username <> 'ahbot'"
PROGRESSION_AUDIT_DIRNAME = "progression-audit"
PROGRESSION_AUDIT_MAX_RECORDS = 100
PROGRESSION_AUDIT_MAX_AGE_SECONDS = 30 * 24 * 60 * 60
log = logging.getLogger(__name__)


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


CLASS_DEATH_KNIGHT = 6
RACE_DRAENEI = 11
RACE_BLOODELF = 10


@dataclass(frozen=True)
class ProgressionConfig:
    progression_limit: int = 0
    starting_progression: int = 0
    tbc_races_starting: int = 0
    death_knight_starting: int = 13


def login_floor_for_character(row: CharacterProgressionRow, cfg: ProgressionConfig) -> int:
    floor = int(cfg.starting_progression or 0)
    if row.race_id in (RACE_DRAENEI, RACE_BLOODELF):
        floor = max(floor, int(cfg.tbc_races_starting or 0))
    if row.class_id == CLASS_DEATH_KNIGHT:
        floor = max(floor, int(cfg.death_knight_starting or 0))
    return floor


def config_from_resolved_keys(keys: list[dict]) -> ProgressionConfig:
    values = {str(k.get("key")): str(k.get("effective_value", "")) for k in keys}

    def int_key(name: str, default: int) -> int:
        raw = values.get(name, "")
        try:
            return int(raw)
        except (TypeError, ValueError):
            return default

    return ProgressionConfig(
        progression_limit=int_key("IndividualProgression.ProgressionLimit", 0),
        starting_progression=int_key("IndividualProgression.StartingProgression", 0),
        tbc_races_starting=int_key("IndividualProgression.tbcRacesStartingProgression", 0),
        death_knight_starting=int_key("IndividualProgression.DeathKnightStartingProgression", 13),
    )


@dataclass(frozen=True)
class ApplyProgressionResult:
    status: str
    target_state: int
    effective_state: int
    reason: str | None = None
    message: str = ""


def _fetch_character(cur, guid: int, *, lock: bool = True) -> CharacterProgressionRow | None:
    cur.execute(
        "SELECT c.guid, a.username, c.name, c.class, c.race, c.level, c.online, "
        "COALESCE(("
        "SELECT MAX(q.quest - 66000) "
        "FROM acore_characters.character_queststatus_rewarded q "
        "WHERE q.guid = c.guid AND q.quest BETWEEN 66001 AND 66013 AND q.active=1"
        "), 0) AS progression_state "
        "FROM acore_characters.characters c "
        "JOIN acore_auth.account a ON a.id = c.account "
        f"WHERE {REAL_ACCOUNT_SQL} AND c.guid = %s " + ("FOR UPDATE" if lock else ""),
        (guid,),
    )
    row = cur.fetchone()
    return None if row is None else _row_to_character(row)


def _existing_progression_quests(cur, guid: int) -> set[int]:
    cur.execute(
        "SELECT quest FROM acore_characters.character_queststatus_rewarded "
        "WHERE guid = %s AND quest BETWEEN 66001 AND 66013 AND active = 1",
        (guid,),
    )
    return {int(r[0]) for r in cur.fetchall()}


def _write_audit_snapshot(
    *,
    snapshots_dir: Path,
    row: CharacterProgressionRow,
    target_expansion: str,
    target_state: int,
    existing_quests: set[int],
) -> Path:
    snapshots_dir = snapshots_dir / PROGRESSION_AUDIT_DIRNAME
    snapshots_dir.mkdir(parents=True, exist_ok=True)
    created_unix = int(time.time())
    path = snapshots_dir / f"progression-{row.guid}-{time.time_ns()}.json"
    payload = {
        "format_version": 1,
        "record_type": "progression_audit",
        "outcome": "pending",
        "guid": row.guid,
        "account": row.account,
        "character": row.name,
        "level": row.level,
        "online": row.online,
        "previous_progression": row.progression,
        "previous_expansion": row.expansion,
        "target_expansion": target_expansion,
        "target_state": target_state,
        "existing_progression_quests": sorted(existing_quests),
        "created_unix": created_unix,
    }
    with path.open("x", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return path


def _finalize_audit_snapshot(
    path: Path,
    *,
    outcome: str,
    effective_state: int | None = None,
    exception: Exception | None = None,
    reason: str | None = None,
) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["outcome"] = outcome
    payload["completed_unix"] = int(time.time())
    if effective_state is not None:
        payload["effective_state"] = effective_state
    if exception is not None:
        payload["exception_type"] = type(exception).__name__
        payload["exception_message"] = str(exception)
    if reason is not None:
        payload["reason"] = reason
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _prune_progression_audit_records(audit_dir: Path, *, now: int | None = None) -> int:
    """Retain only current, valid progression audit records in their own directory."""
    if not audit_dir.exists():
        return 0
    cutoff = (int(time.time()) if now is None else now) - PROGRESSION_AUDIT_MAX_AGE_SECONDS
    records: list[tuple[int, Path]] = []
    for path in audit_dir.glob("progression-*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            created_unix = payload["created_unix"]
            if (
                not isinstance(payload, dict)
                or payload.get("record_type") != "progression_audit"
                or isinstance(created_unix, bool)
                or not isinstance(created_unix, int)
            ):
                continue
        except (OSError, ValueError, TypeError, KeyError):
            continue
        records.append((created_unix, path))

    expired = [(created, path) for created, path in records if created < cutoff]
    current = [(created, path) for created, path in records if created >= cutoff]
    excess = sorted(current, key=lambda record: (record[0], record[1].name))[:-PROGRESSION_AUDIT_MAX_RECORDS]
    removed = 0
    for _, path in expired + excess:
        try:
            path.unlink()
            removed += 1
        except FileNotFoundError:
            pass
    return removed


def _complete_audit_snapshot(
    path: Path,
    *,
    outcome: str,
    effective_state: int | None = None,
    exception: Exception | None = None,
    reason: str | None = None,
) -> None:
    try:
        _finalize_audit_snapshot(
            path,
            outcome=outcome,
            effective_state=effective_state,
            exception=exception,
            reason=reason,
        )
    except Exception:
        log.warning("could not finalize progression audit record %s", path, exc_info=True)
    try:
        _prune_progression_audit_records(path.parent)
    except Exception:
        log.warning("could not prune progression audit records in %s", path.parent, exc_info=True)


def _write_audit_snapshot_best_effort(**kwargs) -> Path | None:
    try:
        return _write_audit_snapshot(**kwargs)
    except Exception:
        log.warning("could not write progression audit record", exc_info=True)
        return None


def _verify_effective_state(cur, guid: int) -> int:
    cur.execute(
        "SELECT COALESCE(MAX(quest - 66000), 0) "
        "FROM acore_characters.character_queststatus_rewarded "
        "WHERE guid = %s AND quest BETWEEN 66001 AND 66013 AND active = 1",
        (guid,),
    )
    row = cur.fetchone() or (0,)
    return int(row[0] or 0)


def apply_progression(
    *,
    guid: int,
    target_expansion: str,
    config: ProgressionConfig,
    snapshots_dir: Path,
    host: str,
    port: int,
    user: str,
    password: str,
) -> ApplyProgressionResult:
    target_state = target_state_for_expansion(target_expansion)
    conn = _connect(host=host, port=port, user=user, password=password)
    audit_snapshot: Path | None = None
    try:
        with conn.cursor() as cur:
            row = _fetch_character(cur, guid, lock=False)
            if row is None:
                conn.rollback()
                return ApplyProgressionResult("rejected", target_state, 0, reason="not_found", message="Character not found.")

            if row.online:
                conn.rollback()
                return ApplyProgressionResult("rejected", target_state, row.progression, reason="online", message="Character must be offline before changing progression.")

            # Re-read under lock to retain the race guard after the cheap
            # online pre-check above.
            row = _fetch_character(cur, guid, lock=True)
            if row is None or row.online:
                conn.rollback()
                return ApplyProgressionResult("rejected", target_state, 0, reason="online", message="Character must be offline before changing progression.")

            login_floor = login_floor_for_character(row, config)
            validation = validate_apply(
                row,
                target_expansion,
                progression_limit=config.progression_limit,
                login_floor=login_floor,
            )
            if not validation.ok:
                existing = _existing_progression_quests(cur, guid)
                audit_snapshot = _write_audit_snapshot_best_effort(
                    snapshots_dir=snapshots_dir,
                    row=row,
                    target_expansion=target_expansion,
                    target_state=target_state,
                    existing_quests=existing,
                )
                conn.rollback()
                if audit_snapshot is not None:
                    _complete_audit_snapshot(
                        audit_snapshot,
                        outcome="validation_rejected",
                        effective_state=row.progression,
                        reason=validation.reason,
                    )
                return ApplyProgressionResult("rejected", target_state, row.progression, reason=validation.reason, message=validation.message)
            if validation.noop:
                conn.rollback()
                return ApplyProgressionResult("noop", target_state, row.progression, message=validation.message)

            existing = _existing_progression_quests(cur, guid)
            audit_snapshot = _write_audit_snapshot_best_effort(
                snapshots_dir=snapshots_dir,
                row=row,
                target_expansion=target_expansion,
                target_state=target_state,
                existing_quests=existing,
            )

            for quest in range(QUEST_BASE + PROGRESSION_MIN, QUEST_BASE + target_state + 1):
                if quest not in existing:
                    cur.execute(
                        "INSERT IGNORE INTO acore_characters.character_queststatus_rewarded (guid, quest, active) VALUES (%s, %s, 1)",
                        (guid, quest),
                    )

            effective = _verify_effective_state(cur, guid)
            if effective < target_state:
                conn.rollback()
                if audit_snapshot is not None:
                    _complete_audit_snapshot(
                        audit_snapshot,
                        outcome="verification_failed_rolled_back",
                        effective_state=effective,
                    )
                return ApplyProgressionResult("error", target_state, effective, reason="verify_failed", message="Progression verification did not reach target; no changes were committed.")

            conn.commit()
            if audit_snapshot is not None:
                _complete_audit_snapshot(audit_snapshot, outcome="applied", effective_state=effective)
            return ApplyProgressionResult("applied", target_state, effective, message="Progression updated.")
    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        if audit_snapshot is not None:
            _complete_audit_snapshot(audit_snapshot, outcome="exception", exception=exc)
        raise
    finally:
        try:
            conn.close()
        except Exception:
            pass
