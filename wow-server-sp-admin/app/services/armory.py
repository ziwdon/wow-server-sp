"""Armory: one character's saved gear, talents, glyphs and stats.

This part is pure: it turns rows of the characters DB (read elsewhere) and DBC records
(``DbcStore``, or any object with the same accessors) into what the page shows, and the
character's rows into what Wowhead's model viewer needs (model id, customisation
options, items). It never raises on odd data: a missing DBC record degrades to an id,
not an error.

Enum values come from the pinned core (rev 3f2969dd), never from memory.
"""
from __future__ import annotations

from dataclasses import dataclass, field
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


# --- equipped items ---


@dataclass(frozen=True, slots=True)
class EquippedItem:
    """One equipped item: ``character_inventory`` (bag 0), ``item_instance``, ``item_template``.

    Every ``item_template`` column of the equipment query (spec §6, query 2) is a field;
    the column name is in the comment. A numbered column group is a tuple in column order
    with zeros kept (index ``i`` = column ``i + 1``), so ``stat_types[i]`` pairs with
    ``stat_values[i]`` and ``spell_ids[i]`` with ``spell_triggers[i]``. The resistance
    columns are nullable in the DB; ``None`` is stored as 0.
    """

    slot: int  # character_inventory.slot (EQUIPMENT_SLOT_*, 0-18)
    entry: int  # entry
    name: str  # name
    display_id: int  # displayid
    inventory_type: int  # InventoryType (INVTYPE_*)
    item_class: int = 0  # class
    subclass: int = 0  # subclass
    quality: int = 0  # Quality
    item_level: int = 0  # ItemLevel
    required_level: int = 0  # RequiredLevel
    bonding: int = 0  # bonding
    armor: int = 0  # armor
    delay: int = 0  # delay (ms between swings)
    dmg_min: float = 0.0  # dmg_min1 (the first damage line only)
    dmg_max: float = 0.0  # dmg_max1
    dmg_type: int = 0  # dmg_type1
    stat_types: tuple[int, ...] = ()  # stat_type1 .. stat_type10 (ITEM_MOD_*)
    stat_values: tuple[int, ...] = ()  # stat_value1 .. stat_value10
    holy_res: int = 0  # holy_res
    fire_res: int = 0  # fire_res
    nature_res: int = 0  # nature_res
    frost_res: int = 0  # frost_res
    shadow_res: int = 0  # shadow_res
    arcane_res: int = 0  # arcane_res
    socket_colors: tuple[int, ...] = ()  # socketColor_1 .. socketColor_3 (colour masks)
    socket_bonus: int = 0  # socketBonus (a SpellItemEnchantment id)
    item_set: int = 0  # itemset (an ItemSet id)
    spell_ids: tuple[int, ...] = ()  # spellid_1 .. spellid_5
    spell_triggers: tuple[int, ...] = ()  # spelltrigger_1 .. spelltrigger_5
    max_durability: int = 0  # MaxDurability
    allowable_class: int = -1  # AllowableClass (class bit mask, -1 = every class)
    description: str = ""  # description
    # item_instance.enchantments through parse_enchantments: {EnchantmentSlot: enchant id}
    enchantments: dict[int, int] = field(default_factory=dict)
    # item_instance.randomPropertyId: < 0 = ItemRandomSuffix id, > 0 = ItemRandomProperties
    random_property_id: int = 0


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


# --- 3D model viewer (spec §2 "Mapping facts", §7.3) ---
#
# Every rule here was measured by the 2026-10-29 spike against Wowhead's live viewer
# with three real characters; the npm package's README is wrong on two of them.

RACE_TAUREN = 6


def model_id(race: int, gender: int) -> int:
    """Wowhead's character model id, ``race*2 - 1 + gender``, the DB gender used as-is.

    DB gender 0 = male, 1 = female; ``meta/character/<id>.json`` (``Character.Gender``)
    confirmed it for all 20 race/gender pairs.
    """
    return race * 2 - 1 + gender


@dataclass(frozen=True, slots=True)
class Appearance:
    """The ``characters`` appearance columns, by their DB index values."""

    skin: int  # skin
    face: int  # face
    hair_style: int  # hairStyle
    hair_color: int  # hairColor
    facial_style: int  # facialStyle


# ``meta/charactercustomization/<modelId>.json`` option name -> the ``Appearance`` field
# whose value picks the choice: the spike's ``page.html`` (``map=fixed``) table, which
# rendered every test character correctly. ``Options[]`` order differs between races
# (Tauren: Skin Color, Horn Style, Horn Color, Hair, Face), so it is matched by name.
# "Horn Style" is the Tauren's hair style; for every other race it is facial.
OPTION_NAME_MAP: dict[str, str] = {
    "Skin Color": "skin",
    "Face": "face",
    "Hair Style": "hair_style",
    "Hair Color": "hair_color",
    "Horn Color": "hair_color",
    "Horn Style": "facial_style",  # "hair_style" when race == RACE_TAUREN
    "Facial Hair": "facial_style",
    "Piercings": "facial_style",
    "Markings": "facial_style",
    "Features": "facial_style",
    "Earrings": "facial_style",
    "Tusks": "facial_style",
    "Hair": "facial_style",
}


def _choice_id(choice: Any) -> Any:
    return choice.get("Id") if isinstance(choice, dict) else None


def map_customization(
    options_json: list[dict], race: int, appearance: Appearance
) -> tuple[list[dict], list[str]]:
    """``Options[]`` of the customisation JSON -> (viewer options, unrecognised names).

    One ``{"optionId", "choiceId"}`` per option, in ``Options[]`` order. The choice is
    ``Choices[value]`` in plain array order, where ``value`` is the appearance field the
    option name maps to (``OPTION_NAME_MAP``); a value outside the list (or one whose
    entry has no id) falls back to ``Choices[0]``. An option whose name is not in the
    table gets ``Choices[0]`` and its name is reported (once; the caller logs it). An
    option with no id, no choices or a first choice without an id is skipped and not
    reported: there is nothing to fall back to.
    """
    options: list[dict] = []
    unknown: list[str] = []
    for option in options_json or ():
        if not isinstance(option, dict):
            continue
        option_id = option.get("Id")
        choices = option.get("Choices")
        if option_id is None or not isinstance(choices, list) or not choices:
            continue
        first = _choice_id(choices[0])
        if first is None:
            continue
        name = option.get("Name")
        name = name if isinstance(name, str) else ""
        attr = OPTION_NAME_MAP.get(name)
        if name == "Horn Style" and race == RACE_TAUREN:
            attr = "hair_style"
        choice_id = first
        if attr is None:
            if name not in unknown:
                unknown.append(name)
        else:
            index = getattr(appearance, attr)
            if 0 <= index < len(choices):
                picked = _choice_id(choices[index])
                if picked is not None:
                    choice_id = picked
        options.append({"optionId": option_id, "choiceId": choice_id})
    return options, unknown


# InventoryType values (src/server/game/Entities/Item/ItemTemplate.h, enum InventoryType)
INVTYPE_SHIELD = 14
INVTYPE_RANGED = 15  # bow
INVTYPE_WEAPONOFFHAND = 22
INVTYPE_HOLDABLE = 23  # held in off hand
INVTYPE_THROWN = 25
INVTYPE_RANGEDRIGHT = 26  # gun, crossbow, wand
INVTYPE_RELIC = 28

# DB slots (Player.h, EQUIPMENT_SLOT_*)
SLOT_MAIN_HAND = 15
SLOT_OFF_HAND = 16
SLOT_RANGED = 17
# neck, two rings, two trinkets: no model on the character
VIEWER_SKIPPED_SLOTS = frozenset({1, 10, 11, 12, 13})


@dataclass(frozen=True, slots=True)
class ViewerItem:
    slot: int  # DB slot, for the paper-doll
    inventory_type: int  # the viewer's slot value: InventoryType, the off hand folded
    display_id: int


def _viewer_slot_type(item: EquippedItem, taken: set[int]) -> int | None:
    """The type to send for ``item``, ``None`` to leave it out (``taken`` = DB slots)."""
    if item.slot in VIEWER_SKIPPED_SLOTS:
        return None
    if item.slot == SLOT_OFF_HAND:
        if item.inventory_type in (INVTYPE_SHIELD, INVTYPE_HOLDABLE):
            return item.inventory_type
        return INVTYPE_WEAPONOFFHAND
    if item.slot == SLOT_RANGED:
        # The viewer folds a bow onto its off-hand key and thrown/gun/crossbow/wand onto
        # its main-hand key, so a ranged item would replace what that hand holds.
        if item.inventory_type == INVTYPE_RANGED:
            return item.inventory_type if SLOT_OFF_HAND not in taken else None
        if item.inventory_type in (INVTYPE_THROWN, INVTYPE_RANGEDRIGHT):
            return item.inventory_type if SLOT_MAIN_HAND not in taken else None
        return None  # relics never; nothing else belongs in this slot
    return item.inventory_type


def viewer_items(items: list[EquippedItem]) -> tuple[list[ViewerItem], list[int]]:
    """Equipped items -> (what the viewer gets, the DB slots deliberately left out).

    Both lists are sorted by DB slot; ``display_id`` is still the DB one (the resolver
    swaps it later). Rules (spec §7.3): neck, rings and trinkets are never sent; the main
    hand (15) is sent with its own type; the off hand (16) as shield 14 / held 23 / else
    off-hand weapon 22; the ranged slot (17) only when its hand key is free (a bow only
    with no off-hand item, thrown/gun/crossbow/wand only with no main-hand item), a
    relic never; every other slot with its ``InventoryType`` (a robe stays 20).
    """
    taken = {item.slot for item in items}
    sent: list[ViewerItem] = []
    omitted: list[int] = []
    for item in sorted(items, key=lambda i: i.slot):
        viewer_type = _viewer_slot_type(item, taken)
        if viewer_type is None:
            omitted.append(item.slot)
        else:
            sent.append(ViewerItem(item.slot, viewer_type, item.display_id))
    return sent, omitted


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
