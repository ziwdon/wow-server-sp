"""Banish: a boss-killer spell the owner can teach to a character, defined server-side.

The WoW 3.3.5a client can only show and cast spell IDs that exist in its own
``Spell.dbc``, so a brand-new ID would need a client patch. Spell 35182
("Banish", icon 96) is an internal Blizzard test spell that the client ships
and nothing on this server references; this module redefines it on the server
only. The client keeps its own text and icon, the server gets a different
behaviour: an instant, free, 100 yd instakill on the targeted hostile NPC that
cannot miss, be reflected or be resisted, and that rewards as a normal kill
(unlike ``.die``). Learning it is a plain ``character_spell`` row.

The definition is two rows in ``acore_world``, both written from the constants
below (and verified against them, so a hand-edited row shows up as "outdated"):

* one ``spell_dbc`` row for ID 35182 (a whole-row override of the client's
  record; unlisted columns take the table defaults), and
* one ``conditions`` row (source type 17, spell) that restricts the explicit
  target to creatures, so the spell can never hit a player. It must be written
  and compared with its five non-key columns: a pre-existing row with
  ``NegativeCondition = 1`` would invert the guard.

``spell_dbc`` is loaded only at worldserver boot (there is no ``.reload``), so
the worldserver has to be restarted after installing or changing the rows.
``banish.json`` remembers when the rows were last written; compared with the
container's ``StartedAt`` it tells ``active`` from ``restart_required``.

Removal order, for whoever removes the definition later: have every character
forget the spell first (``character_spell``), then delete the two rows —
otherwise a character that still knows 35182 falls back to the client's
harmless original test spell on the next boot and the owner thinks it broke.

The SQL builders return parameterised statements; running them is the caller's job.
"""

from __future__ import annotations

import json
import logging
import math
import os
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

SPELL_ID = 35182
DEFINITION_VERSION = 1
SPEC_MASK_ALL = 255  # character_spell.specMask: known in every talent spec
STATE_FILENAME = "banish.json"
_STATE_FORMAT_VERSION = 1  # layout of banish.json; not the definition version

# Every column of spell_dbc that the definition sets, in table (= SQL) order,
# ID first. Columns not listed take the table defaults (0 / ''). Values and the
# reason for each are in the design spec, section 3.1.
SPELL_DBC_ROW: dict[str, int | float | str] = {
    "ID": SPELL_ID,
    "Mechanic": 0,  # the client's copy is 18 (banish): creature mechanic immunity would block it
    "Attributes": 0x24000000,  # AURA_IS_DEBUFF (hostile spell) + NO_IMMUNITIES
    "AttributesEx": 0x80,  # NO_REFLECTION
    "AttributesEx2": 0x04000004,  # IGNORE_LINE_OF_SIGHT + NO_SCHOOL_IMMUNITIES
    "AttributesEx3": 0x40000,  # ALWAYS_HIT
    "AttributesEx4": 0,
    "AttributesEx5": 8,  # ALLOW_WHILE_STUNNED
    "AttributesEx6": 0,
    "AttributesEx7": 0,  # in particular NOT SPELL_ATTR7_DEBUG_SPELL (the client refuses cheat spells)
    "CastingTimeIndex": 1,  # instant
    "ProcChance": 101,
    "DurationIndex": 0,  # the client's copy is 21; no aura any more
    "PowerType": 0,
    "ManaCost": 0,
    "RangeIndex": 6,  # 100 yd: the client pre-checks range from its own row
    "CumulativeAura": 0,
    "EquippedItemClass": -1,  # no equipment requirement (0 would require a consumable)
    "Effect_1": 1,  # SPELL_EFFECT_INSTAKILL
    "Effect_2": 0,
    "Effect_3": 0,
    "EffectDieSides_1": 1,
    "EffectBasePoints_1": 0,
    "ImplicitTargetA_1": 6,  # TARGET_UNIT_TARGET_ENEMY
    "ImplicitTargetA_2": 0,
    "ImplicitTargetA_3": 0,
    "ImplicitTargetB_1": 0,
    "ImplicitTargetB_2": 0,
    "ImplicitTargetB_3": 0,
    "EffectAura_1": 0,  # the client's copy has auras 12 / 39 / 93
    "EffectAura_2": 0,
    "EffectAura_3": 0,
    "EffectMultipleValue_1": 1.0,
    "EffectMiscValue_2": 0,  # the client's copy is 127
    "SpellVisualID_1": 0,
    "SpellVisualID_2": 0,
    "SpellIconID": 96,
    "Name_Lang_enUS": "Banish",
    "Name_Lang_Mask": 16712190,
    "NameSubtext_Lang_Mask": 16712172,
    # Server-side only (.lookup spell, DB readers); the client shows its own text.
    "Description_Lang_enUS": (
        "wow-server-sp admin boss killer: instantly kills the targeted hostile NPC. "
        "Rewards as a normal kill. Cannot target players."
    ),
    "Description_Lang_Mask": 16712190,
    "AuraDescription_Lang_Mask": 16712190,
    "StartRecoveryCategory": 133,  # standard GCD, enforced server-side
    "StartRecoveryTime": 1500,
    "DefenseType": 0,  # DmgClass NONE: cannot miss, cannot be reflected or redirected
    "PreventionType": 0,
    "EffectChainAmplitude_1": 1.0,
    "EffectChainAmplitude_2": 1.0,
    "EffectChainAmplitude_3": 1.0,
    "SchoolMask": 2,  # holy (irrelevant to an instakill)
}

# The conditions row: the ten primary-key columns, then the five others.
CONDITIONS_ROW: dict[str, int | str] = {
    "SourceTypeOrReferenceId": 17,  # CONDITION_SOURCE_TYPE_SPELL
    "SourceGroup": 0,
    "SourceEntry": SPELL_ID,
    "SourceId": 0,
    "ElseGroup": 0,
    "ConditionTypeOrReference": 31,  # CONDITION_OBJECT_ENTRY_GUID
    "ConditionTarget": 1,  # the explicit target
    "ConditionValue1": 3,  # TYPEID_UNIT: true only for creatures
    "ConditionValue2": 0,
    "ConditionValue3": 0,
    "NegativeCondition": 0,
    "ErrorType": 12,  # SPELL_FAILED_BAD_TARGETS ("Invalid target")
    "ErrorTextId": 0,
    "ScriptName": "",
    "Comment": "wow-server-sp admin: Banish boss killer may only target creatures",
}

CONDITIONS_KEY_COLUMNS: tuple[str, ...] = tuple(CONDITIONS_ROW)[:10]
CONDITIONS_VALUE_COLUMNS: tuple[str, ...] = tuple(CONDITIONS_ROW)[10:]


# --- SQL ---


def _quoted(columns: Sequence[str]) -> str:
    return ", ".join(f"`{c}`" for c in columns)


def _placeholders(count: int) -> str:
    return ", ".join(["%s"] * count)


def spell_dbc_insert_sql() -> tuple[str, tuple]:
    """``REPLACE`` for the spell_dbc row (ID is the primary key); parameterised."""
    sql = (
        f"REPLACE INTO acore_world.spell_dbc ({_quoted(list(SPELL_DBC_ROW))}) "
        f"VALUES ({_placeholders(len(SPELL_DBC_ROW))})"
    )
    return sql, tuple(SPELL_DBC_ROW.values())


def conditions_insert_sql() -> tuple[str, tuple]:
    """Upsert for the conditions row: the ten key columns identify it, the five others are rewritten."""
    update = ", ".join(f"`{c}` = VALUES(`{c}`)" for c in CONDITIONS_VALUE_COLUMNS)
    sql = (
        f"INSERT INTO acore_world.conditions ({_quoted(list(CONDITIONS_ROW))}) "
        f"VALUES ({_placeholders(len(CONDITIONS_ROW))}) "
        f"ON DUPLICATE KEY UPDATE {update}"
    )
    return sql, tuple(CONDITIONS_ROW.values())


def spell_dbc_select_sql() -> str:
    """Read back every column the definition sets, in ``SPELL_DBC_ROW`` order; one parameter: the ID."""
    return f"SELECT {_quoted(list(SPELL_DBC_ROW))} FROM acore_world.spell_dbc WHERE `ID` = %s"


def conditions_select_sql() -> tuple[str, tuple]:
    """Read back the five non-key columns of the row the ten key columns identify."""
    where = " AND ".join(f"`{c}` = %s" for c in CONDITIONS_KEY_COLUMNS)
    sql = f"SELECT {_quoted(CONDITIONS_VALUE_COLUMNS)} FROM acore_world.conditions WHERE {where}"
    return sql, tuple(CONDITIONS_ROW[c] for c in CONDITIONS_KEY_COLUMNS)


# --- definition status ---


def _value_matches(expected: object, actual: object) -> bool:
    if isinstance(expected, str) and isinstance(actual, (bytes, bytearray)):
        try:
            actual = bytes(actual).decode("utf-8")
        except UnicodeDecodeError:
            return False
    if isinstance(expected, float) or isinstance(actual, float):
        try:
            return math.isclose(float(expected), float(actual), rel_tol=1e-6, abs_tol=1e-6)
        except (TypeError, ValueError):
            return False
    return expected == actual


def row_matches(expected: Mapping[str, object], actual: Sequence[object] | None) -> bool:
    """Whether a fetched row equals the expected values, column by column in ``expected`` order.

    ``actual`` is one DB row selected in that same column order (``None`` = no row).
    Strings may arrive as ``bytes``/``bytearray`` (a binary collation); floats are
    compared with a tolerance because MySQL stores them as single precision.
    """
    if actual is None or len(actual) != len(expected):
        return False
    return all(_value_matches(want, got) for want, got in zip(expected.values(), actual))


def definition_state(spell_row: Sequence[object] | None, cond_row: Sequence[object] | None) -> str:
    """``missing`` (no spell_dbc row), ``installed`` (both rows equal the definition) or ``outdated``.

    ``spell_row`` is selected with ``spell_dbc_select_sql``, ``cond_row`` with
    ``conditions_select_sql``.
    """
    if spell_row is None:
        return "missing"
    cond_expected = {c: CONDITIONS_ROW[c] for c in CONDITIONS_VALUE_COLUMNS}
    if row_matches(SPELL_DBC_ROW, spell_row) and row_matches(cond_expected, cond_row):
        return "installed"
    return "outdated"


# --- worldserver state ---


def parse_docker_time(value: str | None) -> float | None:
    """Docker's RFC 3339 timestamp (nanoseconds, ``Z``) as epoch seconds; ``None`` when absent, zero or unparsable."""
    if not value or value.startswith("0001-"):  # Docker's zero time: the container never started
        return None
    text = value.removesuffix("Z")
    if "." in text:
        head, _, fraction = text.partition(".")
        text = f"{head}.{fraction[:6]}"
    try:
        return datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp()
    except (ValueError, OverflowError):
        return None


def worldserver_state(container_status: str, started_at: str | None, installed_at: int | None) -> str:
    """``active`` | ``restart_required`` | ``unknown`` | ``not_running``.

    ``container_status`` is Docker's status string (``unknown`` = the daemon could
    not be asked). The spell is loaded at boot, so the worldserver is ``active``
    only when it started after the rows were written (``installed_at``).
    """
    if container_status == "unknown":
        return "unknown"
    if container_status != "running":
        return "not_running"
    started = parse_docker_time(started_at)
    if installed_at is None or started is None:
        return "unknown"
    return "active" if started > installed_at else "restart_required"


def teach_warning(worldserver: str) -> str | None:
    """The caution shown with Teach unless the running worldserver has loaded the definition."""
    if worldserver == "active":
        return None
    return (
        "The running worldserver has not loaded the definition yet — "
        "restart it (Dashboard → Restart) before using the spell."
    )


# --- persisted install stamp ---


class BanishStateStore:
    """``banish.json`` in ADMIN_DATA_DIR: when the definition was last written.

    A regular file (not a bind-mount source), so tmp+rename is safe. Building the
    store touches nothing; routes construct one per request.
    """

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.path = data_dir / STATE_FILENAME

    def load(self) -> int | None:
        """Epoch seconds of the last install, or ``None`` when missing or unreadable."""
        try:
            raw = json.loads(self.path.read_text())
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            raw = None
        installed_at = raw.get("installed_at") if isinstance(raw, dict) else None
        if isinstance(installed_at, bool) or not isinstance(installed_at, int):
            log.warning("banish state %s unreadable; treating the install time as unknown", self.path)
            return None
        return installed_at

    def save(self, installed_at: int) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        payload = {"version": _STATE_FORMAT_VERSION, "installed_at": int(installed_at)}
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        os.replace(tmp, self.path)


def store_from_env() -> BanishStateStore:
    return BanishStateStore(Path(os.environ.get("ADMIN_DATA_DIR", "/admin-data")))
