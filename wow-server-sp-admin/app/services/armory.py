"""Armory: one character's saved gear, talents, glyphs and stats.

This part is pure: it turns rows of the characters DB (read elsewhere) and DBC records
(``DbcStore``, or any object with the same accessors) into what the page shows. It
never raises on odd data: a missing DBC record degrades to an id, not an error.

Enum values come from the pinned core (rev 3f2969dd), never from memory.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Protocol

from app.services import wow_reference as wr
from app.services.dbc import Glyph, Spell, Talent, TalentTab


class ArmoryStore(Protocol):
    """The ``DbcStore`` accessors used here (ids are passed positionally)."""

    def talents(self) -> dict[int, Talent]: ...
    def talent_tabs(self) -> dict[int, TalentTab]: ...
    def spell(self, id_: int) -> Spell | None: ...
    def spell_icon(self, id_: int) -> str: ...
    def glyph(self, id_: int) -> Glyph | None: ...


# --- item enchantments ---

# src/server/game/Entities/Item/Item.h:167-184, enum EnchantmentSlot
ENCHANT_PERM = 0          # PERM_ENCHANTMENT_SLOT
ENCHANT_TEMP = 1          # TEMP_ENCHANTMENT_SLOT
ENCHANT_SOCKETS = (2, 3, 4)  # SOCK_ENCHANTMENT_SLOT .. SOCK_ENCHANTMENT_SLOT_3
ENCHANT_BONUS = 5         # BONUS_ENCHANTMENT_SLOT (socket bonus)
ENCHANT_PRISMATIC = 6     # PRISMATIC_ENCHANTMENT_SLOT
ENCHANT_SLOTS = 12        # MAX_ENCHANTMENT_SLOT (7-11 = random property/suffix slots)


def parse_enchantments(raw: str | None) -> dict[int, int]:
    """``item_instance.enchantments`` -> ``{slot: enchant_id}`` for the non-zero ids.

    The column holds ``MAX_ENCHANTMENT_SLOT`` x ``id duration charges``, space separated.
    Token ``3 * slot`` is the id. Short input keeps what is there; a token that is not a
    number is skipped without shifting the later slots; zero, negative and out-of-range
    ids are dropped.
    """
    out: dict[int, int] = {}
    for i, token in enumerate((raw or "").split()[: 3 * ENCHANT_SLOTS : 3]):
        try:
            enchant_id = int(token)
        except ValueError:
            continue
        if enchant_id > 0:
            out[i] = enchant_id
    return out


# --- talents ---


@dataclass(frozen=True, slots=True)
class TalentCell:
    talent_id: int
    tier: int
    column: int
    rank: int  # 0 = not learned
    max_rank: int
    spell: Spell | None  # the learned rank's spell, rank 1's when not learned
    icon: str


@dataclass(frozen=True, slots=True)
class TalentTree:
    tab: TalentTab
    points: int
    cells: list[TalentCell]


def split_specs(rows: Iterable[tuple[int, int]]) -> tuple[set[int], set[int]]:
    """``character_talent`` ``(spell, specMask)`` rows -> (spec 1 spells, spec 2 spells).

    Bit 0 of ``specMask`` is the first talent group, bit 1 the second (both = 3).
    """
    spec1: set[int] = set()
    spec2: set[int] = set()
    for spell_id, mask in rows:
        if mask & 1:
            spec1.add(spell_id)
        if mask & 2:
            spec2.add(spell_id)
    return spec1, spec2


def talent_trees(class_id: int, learned: set[int], store: ArmoryStore) -> list[TalentTree]:
    """The class's three trees (``TalentTab.order``), each cell with its learned rank.

    A cell's rank is 1 + the index of the highest of its rank spells in ``learned``; a
    learned spell that is no talent of these trees is ignored. Without Talent/TalentTab
    data (DBC missing) or for an unknown class the result is ``[]``.
    """
    if class_id < 1:
        return []
    class_bit = 1 << (class_id - 1)
    tabs = sorted(
        (tab for tab in store.talent_tabs().values() if tab.class_mask & class_bit),
        key=lambda tab: (tab.order, tab.id),
    )
    by_tab: dict[int, list[Talent]] = {}
    for talent in store.talents().values():
        if talent.ranks:
            by_tab.setdefault(talent.tab, []).append(talent)

    trees: list[TalentTree] = []
    for tab in tabs:
        cells: list[TalentCell] = []
        for talent in sorted(by_tab.get(tab.id, ()), key=lambda t: (t.tier, t.column, t.id)):
            rank = 0
            for index, spell_id in enumerate(talent.ranks):
                if spell_id in learned:
                    rank = index + 1
            spell = store.spell(talent.ranks[max(rank, 1) - 1])
            cells.append(TalentCell(
                talent_id=talent.id,
                tier=talent.tier,
                column=talent.column,
                rank=rank,
                max_rank=len(talent.ranks),
                spell=spell,
                icon=store.spell_icon(spell.icon_id) if spell is not None else "",
            ))
        trees.append(TalentTree(tab=tab, points=sum(c.rank for c in cells), cells=cells))
    return trees


# --- glyphs ---

GLYPH_SLOTS = 6  # MAX_GLYPH_SLOT_INDEX: character_glyphs.glyph1 .. glyph6

# Socket kind per character_glyphs column, used only for an EMPTY socket (a filled one
# takes GlyphProperties' own type). Majors are glyph1, glyph4 and glyph6: the pinned
# mod-playerbots places "<major 1>,<minor 1>,<major 2>,<minor 2>,<minor 3>,<major 3>"
# (conf/playerbots.conf.dist:1801) into slots glyphOrder = {0, 1, 3, 2, 4, 5}
# (src/Bot/Factory/PlayerbotFactory.cpp:4627).
GLYPH_SLOT_MAJOR = (True, False, False, True, False, True)


@dataclass(frozen=True, slots=True)
class GlyphRow:
    index: int  # 0-5 = character_glyphs.glyph1 .. glyph6
    major: bool
    name: str | None  # None = empty socket
    icon: str


def glyph_rows(glyphs: tuple[int | None, ...], store: ArmoryStore) -> list[GlyphRow]:
    """One talent group's six glyph sockets (missing columns = empty sockets).

    Major/minor of a filled socket comes from ``GlyphProperties`` of its glyph id; the
    name and icon from that glyph's spell. A glyph the store does not know (DBC missing)
    is shown as ``Glyph #<id>`` with no icon.
    """
    padded = (tuple(glyphs) + (0,) * GLYPH_SLOTS)[:GLYPH_SLOTS]
    rows: list[GlyphRow] = []
    for index, glyph_id in enumerate(padded):
        column_major = GLYPH_SLOT_MAJOR[index]
        if not glyph_id:
            rows.append(GlyphRow(index=index, major=column_major, name=None, icon=""))
            continue
        glyph = store.glyph(glyph_id)
        spell = store.spell(glyph.spell_id) if glyph is not None else None
        rows.append(GlyphRow(
            index=index,
            major=glyph.type_major if glyph is not None else column_major,
            name=spell.name if spell is not None and spell.name else f"Glyph #{glyph_id}",
            icon=store.spell_icon(spell.icon_id) if spell is not None else "",
        ))
    return rows


# --- saved stats ---

# (label, character_stats column, percentage?) in display order; the power row is
# inserted after Health. Columns: data/sql/base/db_characters/character_stats.sql,
# filled by Player::_SaveStats (PlayerStorage.cpp).
_STATS_LAYOUT = (
    ("Strength", "strength", False),
    ("Agility", "agility", False),
    ("Stamina", "stamina", False),
    ("Intellect", "intellect", False),
    ("Spirit", "spirit", False),
    ("Armor", "armor", False),
    ("Holy resistance", "resHoly", False),
    ("Fire resistance", "resFire", False),
    ("Nature resistance", "resNature", False),
    ("Frost resistance", "resFrost", False),
    ("Shadow resistance", "resShadow", False),
    ("Arcane resistance", "resArcane", False),
    ("Attack power", "attackPower", False),
    ("Ranged attack power", "rangedAttackPower", False),
    ("Spell power", "spellPower", False),
    ("Melee crit", "critPct", True),
    ("Ranged crit", "rangedCritPct", True),
    ("Spell crit", "spellCritPct", True),
    ("Dodge", "dodgePct", True),
    ("Parry", "parryPct", True),
    ("Block", "blockPct", True),
    ("Resilience", "resilience", False),  # the rating (CR_CRIT_TAKEN_SPELL)
)

# Rage and runic power are stored in tenths (max 1000 = 100 on the client's bar).
_POWER_IN_TENTHS = frozenset({1, 6})


def stats_rows(
    row: tuple | None, columns: tuple[str, ...], *, class_id: int | None = None
) -> list[tuple[str, str]] | None:
    """A ``character_stats`` row -> ``[(label, value)]``; ``None`` when there is no row.

    Values are mapped by column name (``columns`` = the cursor's column names), so the
    query's column order does not matter; a column the row lacks is left out. The power
    row is the class's power type (``CLASS_POWER_TYPE``); with no class it is the saved
    max mana. Percentages get two decimals and a ``%``; everything else is an integer.
    """
    if row is None:
        return None
    values: dict[str, Any] = dict(zip(columns, row))
    out: list[tuple[str, str]] = []

    def add(label: str, column: str, pct: bool = False, divisor: int = 1) -> None:
        value = values.get(column)
        if value is None:
            return
        out.append((label, f"{float(value):.2f}%" if pct else str(int(value) // divisor)))

    add("Health", "maxhealth")
    power = wr.CLASS_POWER_TYPE.get(class_id, 0)  # unknown/None: mana
    add(wr.POWER_NAMES[power], f"maxpower{power + 1}",
        divisor=10 if power in _POWER_IN_TENTHS else 1)
    for label, column, pct in _STATS_LAYOUT:
        add(label, column, pct)
    return out


# --- search ---


def _upper(char: str) -> str:
    up = char.upper()
    return up if len(up) == 1 else char


def _lower(char: str) -> str:
    low = char.lower()
    return low if len(low) == 1 else char


def normalize_search(q: str | None) -> str | None:
    """A typed name prefix -> the escaped ``LIKE`` prefix, or ``None`` if under 2 chars.

    AC stores names first letter upper, the rest lower (``normalizePlayerName``) in a
    ``utf8mb4_bin`` column, so the prefix is brought to that form instead of wrapping the
    column in ``LOWER()`` (which would stop the ``idx_name`` index being used). A letter
    whose case change would change its length is kept as typed. ``\\``, ``%`` and ``_``
    are escaped for ``LIKE ... ESCAPE '\\'``; the caller appends the ``%``.
    """
    text = (q or "").strip()
    if len(text) < 2:
        return None
    text = _upper(text[0]) + "".join(_lower(c) for c in text[1:])
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
