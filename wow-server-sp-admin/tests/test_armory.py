"""Armory: reference tables, the pure character-data helpers and the read-only DB layer.

The DBC-backed helpers take any object with the ``DbcStore`` accessors; these tests use a
small in-memory fake built from the real ``dbc`` record types, never real DBC files.
The viewer-mapping tests use the three real characters of the 2026-10-29 spike
(``tests/data/armory/spike_characters.json``). The DB-layer tests replace
``mysql.connector.connect`` with ``FakeConn`` (no socket is ever opened).
"""
from __future__ import annotations

import dataclasses
import json
from decimal import Decimal
from pathlib import Path

import mysql.connector
import pytest

from app.services import armory, players
from app.services import wow_reference as wr
from app.services.armory import (
    ENCHANT_BONUS,
    ENCHANT_PERM,
    ENCHANT_PRISMATIC,
    ENCHANT_SOCKETS,
    EQUIPMENT_COLUMNS,
    OPTION_NAME_MAP,
    Appearance,
    CharacterView,
    EquippedItem,
    GlyphRow,
    Header,
    SearchHit,
    SetBlock,
    Socket,
    Tooltip,
    ViewerItem,
    collect_character,
    dps,
    equipped_item,
    format_stat,
    glyph_rows,
    map_customization,
    model_id,
    normalize_search,
    parse_enchantments,
    search_characters,
    socket_color_name,
    split_specs,
    stats_rows,
    suffix_factor,
    talent_trees,
    tooltip,
    viewer_items,
)
from app.services import dbc
from app.services.dbc import (
    Enchant,
    Gem,
    Glyph,
    ItemSet,
    RandomEnchant,
    RandPropPoints,
    Spell,
    Talent,
    TalentTab,
)


class FakeStore:
    """Just the ``DbcStore`` accessors the helpers use (ids are positional, like ``id_``)."""

    def __init__(self, *, talents=(), tabs=(), spells=(), icons=None, glyphs=(),
                 enchants=(), gems=(), random_properties=(), random_suffixes=(),
                 item_sets=(), icons_by_display=None, points=None, missing=()):
        self._talents = {t.id: t for t in talents}
        self._tabs = {t.id: t for t in tabs}
        self._spells = {s.id: s for s in spells}
        self._icons = dict(icons or {})
        self._glyphs = {g.id: g for g in glyphs}
        self._enchants = {e.id: e for e in enchants}
        self._gems = {g.item_id: g for g in gems}
        self._random_properties = {r.id: r for r in random_properties}
        self._random_suffixes = {r.id: r for r in random_suffixes}
        self._item_sets = {s.id: s for s in item_sets}
        self._item_icons = dict(icons_by_display or {})
        self._points = dict(points or {})
        self._missing = list(missing)

    def missing(self):
        return list(self._missing)

    def ok(self):
        return not self._missing

    def enchant(self, id_):
        return self._enchants.get(id_)

    def gem(self, item_id):
        return self._gems.get(item_id)

    def random_property(self, id_):
        return self._random_properties.get(id_)

    def random_suffix(self, id_):
        return self._random_suffixes.get(id_)

    def item_set(self, id_):
        return self._item_sets.get(id_)

    def item_icon(self, display_id):
        return self._item_icons.get(display_id, "")

    def rand_prop_points(self, item_level):
        return self._points.get(item_level)

    def talents(self):
        return dict(self._talents)

    def talent_tabs(self):
        return dict(self._tabs)

    def spell(self, id_):
        return self._spells.get(id_)

    def spell_icon(self, id_):
        return self._icons.get(id_, "")

    def glyph(self, id_):
        return self._glyphs.get(id_)


# --- enchantments ---


def test_parse_enchantments_reads_slots_and_ignores_zero_and_garbage():
    # AC writes 12 x "id duration charges" with a trailing space.
    raw = "2564 0 0 0 0 0 3375 0 0 " + "0 0 0 " * 9
    assert parse_enchantments(raw) == {0: 2564, 2: 3375}
    assert parse_enchantments("abc") == {}
    assert parse_enchantments("") == {}
    assert parse_enchantments(None) == {}
    # short input: what parses is kept
    assert parse_enchantments("2564 0") == {0: 2564}
    # a garbage token does not shift the later slots
    assert parse_enchantments("2564 0 0 x 0 0 3375 0 0") == {0: 2564, 2: 3375}
    # durations/charges are never read as ids; negatives and slots >= 12 are dropped
    assert parse_enchantments("0 2564 7 -5 0 0") == {}
    assert parse_enchantments("0 0 0 " * 12 + "99 0 0") == {}
    # the last real slot (PROP_ENCHANTMENT_SLOT_4 = 11) is kept
    assert parse_enchantments("0 0 0 " * 11 + "123 0 0") == {11: 123}


def test_enchantment_slot_constants_match_item_h():
    # src/server/game/Entities/Item/Item.h, enum EnchantmentSlot (pinned core 3f2969dd)
    assert ENCHANT_PERM == 0
    assert ENCHANT_SOCKETS == (2, 3, 4)
    assert ENCHANT_BONUS == 5
    assert ENCHANT_PRISMATIC == 6


# --- talents ---


def test_split_specs_by_mask_bits():
    rows = [(100, 1), (200, 2), (300, 3), (400, 0)]
    spec1, spec2 = split_specs(rows)
    assert spec1 == {100, 300}
    assert spec2 == {200, 300}
    assert split_specs([]) == (set(), set())


def _warrior_store(spell_store=True):
    tabs = [
        # dict order differs from the in-game order on purpose
        TalentTab(id=163, name="Protection", icon_id=3, class_mask=1, order=2),
        TalentTab(id=161, name="Arms", icon_id=1, class_mask=1, order=0),
        TalentTab(id=164, name="Fury", icon_id=2, class_mask=1, order=1),
        TalentTab(id=41, name="Fire", icon_id=4, class_mask=128, order=1),  # mage
        TalentTab(id=409, name="Tenacity", icon_id=5, class_mask=0, order=0),  # pet
    ]
    talents = [
        Talent(id=3, tab=161, tier=1, column=0, ranks=(30,)),
        Talent(id=1, tab=161, tier=0, column=0, ranks=(10, 11, 12)),
        Talent(id=2, tab=161, tier=0, column=1, ranks=(20, 21)),
        Talent(id=4, tab=164, tier=0, column=2, ranks=(40, 41, 42, 43, 44)),
        Talent(id=5, tab=41, tier=0, column=0, ranks=(50,)),
    ]
    spells = [
        Spell(id=12, name="Improved Heroic Strike", rank="Rank 3", icon_id=7, description="d12"),
        Spell(id=20, name="Deflection", rank="Rank 1", icon_id=8, description="d20"),
        Spell(id=30, name="Improved Rend", rank="Rank 1", icon_id=9, description="d30"),
        Spell(id=43, name="Cruelty", rank="Rank 4", icon_id=10, description="d43"),
    ] if spell_store else []
    icons = {7: "ability_rogue_ambush", 8: "ability_parry", 9: "ability_gouge", 10: "ability_rogue_eviscerate"}
    return FakeStore(talents=talents, tabs=tabs, spells=spells, icons=icons)


def test_talent_trees_filters_tabs_by_class_and_counts_points():
    store = _warrior_store()
    learned = {12, 20, 43, 999, 50}  # 999 is no talent; 50 is a mage talent
    trees = talent_trees(1, learned, store)

    assert [t.tab.name for t in trees] == ["Arms", "Fury", "Protection"]
    assert [t.points for t in trees] == [4, 4, 0]

    arms = trees[0].cells
    assert [(c.talent_id, c.tier, c.column) for c in arms] == [(1, 0, 0), (2, 0, 1), (3, 1, 0)]
    imp_hs, deflection, rend = arms
    assert (imp_hs.rank, imp_hs.max_rank) == (3, 3)
    assert imp_hs.spell.id == 12 and imp_hs.icon == "ability_rogue_ambush"
    assert (deflection.rank, deflection.max_rank) == (1, 2)
    assert deflection.spell.id == 20
    # an unlearned talent shows its rank-1 spell, dimmed by rank 0
    assert (rend.rank, rend.max_rank) == (0, 1)
    assert rend.spell.id == 30 and rend.icon == "ability_gouge"

    fury = trees[1].cells
    assert len(fury) == 1 and (fury[0].rank, fury[0].max_rank) == (4, 5)
    assert trees[2].cells == []


def test_talent_trees_degrade_without_dbc_data():
    # Spell.dbc missing: cells stay, with no spell and no icon
    trees = talent_trees(1, {12}, _warrior_store(spell_store=False))
    cell = trees[0].cells[0]
    assert cell.spell is None and cell.icon == "" and cell.rank == 3
    # Talent/TalentTab.dbc missing, or a class with no trees
    assert talent_trees(1, {12}, FakeStore()) == []
    assert talent_trees(6, {12}, _warrior_store()) == []
    assert talent_trees(0, {12}, _warrior_store()) == []


# --- glyphs ---


def test_glyph_rows_use_glyph_type_not_column():
    store = FakeStore(
        glyphs=[
            Glyph(id=181, spell_id=58097, type_major=False),  # minor glyph in column 0
            Glyph(id=263, spell_id=58388, type_major=True),   # major glyph in column 2
        ],
        spells=[
            Spell(id=58097, name="Glyph of Battle", rank="", icon_id=11, description=""),
            Spell(id=58388, name="Glyph of Revenge", rank="", icon_id=12, description=""),
        ],
        icons={11: "inv_glyph_minorwarrior", 12: "inv_glyph_majorwarrior"},
    )
    rows = glyph_rows((181, 0, 263, 0, None, 999), store)

    assert [r.index for r in rows] == [0, 1, 2, 3, 4, 5]
    assert rows[0] == GlyphRow(index=0, major=False, name="Glyph of Battle", icon="inv_glyph_minorwarrior")
    assert rows[2] == GlyphRow(index=2, major=True, name="Glyph of Revenge", icon="inv_glyph_majorwarrior")
    # empty sockets take the column's kind (glyph1/4/6 major, glyph2/3/5 minor)
    assert (rows[1].major, rows[1].name, rows[1].icon) == (False, None, "")
    assert (rows[3].major, rows[3].name) == (True, None)
    assert (rows[4].major, rows[4].name) == (False, None)
    # a glyph the store does not know (DBC missing): show its id
    assert (rows[5].name, rows[5].icon, rows[5].major) == ("Glyph #999", "", True)


def test_glyph_rows_pad_to_six_sockets():
    rows = glyph_rows((), FakeStore())
    assert [(r.index, r.major, r.name) for r in rows] == [
        (0, True, None), (1, False, None), (2, False, None),
        (3, True, None), (4, False, None), (5, True, None),
    ]
    assert len(glyph_rows((1, 2, 3, 4, 5, 6, 7), FakeStore())) == 6


# --- stats ---

# data/sql/base/db_characters/character_stats.sql (pinned core), in table order
STATS_COLUMNS = (
    "guid", "maxhealth", "maxpower1", "maxpower2", "maxpower3", "maxpower4", "maxpower5",
    "maxpower6", "maxpower7", "strength", "agility", "stamina", "intellect", "spirit",
    "armor", "resHoly", "resFire", "resNature", "resFrost", "resShadow", "resArcane",
    "blockPct", "dodgePct", "parryPct", "critPct", "rangedCritPct", "spellCritPct",
    "attackPower", "rangedAttackPower", "spellPower", "resilience",
)


def _stats_row(**overrides):
    values = dict(
        guid=2506, maxhealth=12345, maxpower1=6789, maxpower2=1000, maxpower3=0,
        maxpower4=100, maxpower5=0, maxpower6=8, maxpower7=1000, strength=101, agility=102,
        stamina=103, intellect=104, spirit=105, armor=4321, resHoly=1, resFire=2,
        resNature=3, resFrost=4, resShadow=5, resArcane=6, blockPct=5.0, dodgePct=10.5,
        parryPct=0.0, critPct=12.3456, rangedCritPct=7.001, spellCritPct=3.999,
        attackPower=1500, rangedAttackPower=900, spellPower=250, resilience=42,
    )
    values.update(overrides)
    return values


def test_stats_rows_none_when_missing():
    assert stats_rows(None, STATS_COLUMNS) is None
    assert stats_rows(None, STATS_COLUMNS, class_id=1) is None


def test_stats_rows_labels_and_formats_by_column_name():
    values = _stats_row()
    # the cursor's column order is not the table order: values are mapped by name
    columns = tuple(reversed(STATS_COLUMNS))
    row = tuple(values[c] for c in columns)

    rows = stats_rows(row, columns, class_id=8)  # mage: mana
    assert rows == [
        ("Health", "12345"),
        ("Mana", "6789"),
        ("Strength", "101"),
        ("Agility", "102"),
        ("Stamina", "103"),
        ("Intellect", "104"),
        ("Spirit", "105"),
        ("Armor", "4321"),
        ("Holy resistance", "1"),
        ("Fire resistance", "2"),
        ("Nature resistance", "3"),
        ("Frost resistance", "4"),
        ("Shadow resistance", "5"),
        ("Arcane resistance", "6"),
        ("Attack power", "1500"),
        ("Ranged attack power", "900"),
        ("Spell power", "250"),
        ("Melee crit", "12.35%"),
        ("Ranged crit", "7.00%"),
        ("Spell crit", "4.00%"),
        ("Dodge", "10.50%"),
        ("Parry", "0.00%"),
        ("Block", "5.00%"),
        ("Resilience", "42"),
    ]


def test_stats_rows_power_follows_the_class_power_type():
    row = tuple(_stats_row()[c] for c in STATS_COLUMNS)
    power = lambda cid: stats_rows(row, STATS_COLUMNS, class_id=cid)[1]  # noqa: E731
    # every player saves max rage 1000 and max energy 100, so the class decides the row;
    # rage and runic power are stored in tenths (Unit::GetCreatePowers)
    assert power(1) == ("Rage", "100")
    assert power(4) == ("Energy", "100")
    assert power(6) == ("Runic power", "100")
    assert power(11) == ("Mana", "6789")
    # class unknown: the saved max mana, labelled as such
    assert stats_rows(row, STATS_COLUMNS)[1] == ("Mana", "6789")


def test_stats_rows_skips_columns_the_row_lacks():
    columns = ("guid", "maxhealth", "critPct")
    rows = stats_rows((1, 500, 1.5), columns, class_id=1)
    assert rows == [("Health", "500"), ("Melee crit", "1.50%")]


# --- search ---


def test_normalize_search_capitalises_and_escapes():
    assert normalize_search("  vEGI% ") == "Vegi\\%"
    assert normalize_search("v") is None
    assert normalize_search("  v  ") is None
    assert normalize_search("") is None
    assert normalize_search("a_b") == "A\\_b"
    assert normalize_search("x\\y") == "X\\\\y"
    assert normalize_search("PITOKAX") == "Pitokax"
    assert normalize_search("éLAN") == "Élan"


# --- reference tables ---


def test_reference_tables_cover_all_equipment_slots_and_inventory_types():
    assert set(wr.EQUIP_SLOT_NAMES) == set(range(19))
    assert set(wr.INVENTORY_TYPE_NAMES) == set(range(29))
    assert wr.EQUIP_SLOT_NAMES[0] == "Head" and wr.EQUIP_SLOT_NAMES[18] == "Tabard"
    assert wr.INVENTORY_TYPE_NAMES[1] == "Head"
    assert wr.INVENTORY_TYPE_NAMES[13] == "One-Hand"
    assert wr.INVENTORY_TYPE_NAMES[17] == "Two-Hand"
    assert wr.INVENTORY_TYPE_NAMES[23] == "Held In Off-hand"
    assert wr.INVENTORY_TYPE_NAMES[28] == "Relic"


def test_reference_quality_stat_bonding_damage_and_subclass_tables():
    assert {q: wr.QUALITY_COLORS[q] for q in range(7)} == {
        0: "#9d9d9d", 1: "#ffffff", 2: "#1eff00", 3: "#0070dd",
        4: "#a335ee", 5: "#ff8000", 6: "#e6cc80",
    }
    assert set(wr.QUALITY_NAMES) == set(wr.QUALITY_COLORS)
    assert wr.QUALITY_NAMES[4] == "Epic"

    expected_stats = {
        3: "Agility", 4: "Strength", 5: "Intellect", 6: "Spirit", 7: "Stamina",
        12: "Defense Rating", 13: "Dodge Rating", 31: "Hit Rating",
        32: "Critical Strike Rating", 35: "Resilience Rating", 36: "Haste Rating",
        37: "Expertise Rating", 38: "Attack Power", 39: "Ranged Attack Power",
        41: "Spell Healing", 42: "Spell Damage", 43: "Mana Regeneration",
        44: "Armor Penetration Rating", 45: "Spell Power", 47: "Spell Penetration",
    }
    for stat_type, name in expected_stats.items():
        assert wr.STAT_NAMES[stat_type] == name, stat_type
    # every ItemModType of ItemTemplate.h (40 is commented out there: not in 3.3)
    assert set(wr.STAT_NAMES) == {0, 1, *range(3, 8), *range(12, 40), *range(41, 49)}

    assert wr.BONDING_NAMES[1] == "Binds when picked up"
    assert wr.BONDING_NAMES[2] == "Binds when equipped"
    assert wr.BONDING_NAMES[3] == "Binds when used"
    assert wr.BONDING_NAMES[4] == "Quest Item"
    assert set(wr.DAMAGE_TYPE_NAMES) == set(range(7))
    assert wr.DAMAGE_TYPE_NAMES[2] == "Fire"

    assert {s for (c, s) in wr.ITEM_SUBCLASS_NAMES if c == 2} == set(range(21))
    assert {s for (c, s) in wr.ITEM_SUBCLASS_NAMES if c == 4} == set(range(11))
    assert wr.ITEM_SUBCLASS_NAMES[(2, 2)] == "Bow"
    assert wr.ITEM_SUBCLASS_NAMES[(4, 4)] == "Plate"
    assert wr.ITEM_SUBCLASS_NAMES[(4, 6)] == "Shield"


def test_every_class_has_a_power_type_with_a_name():
    assert set(wr.CLASS_POWER_TYPE) == set(wr.CLASSES)
    for power_type in wr.CLASS_POWER_TYPE.values():
        assert power_type in wr.POWER_NAMES


# --- 3D viewer mapping (spec §2 "Mapping facts", §7.3) ---

_SPIKE = json.loads(
    (Path(__file__).parent / "data" / "armory" / "spike_characters.json").read_text("utf-8")
)


def _spike(name):
    return next(c for c in _SPIKE["characters"] if c["name"] == name)


def _gear(name):
    return [EquippedItem(**piece) for piece in _spike(name)["gear"]]


def _appearance(name):
    c = _spike(name)
    return Appearance(skin=c["skin"], face=c["face"], hair_style=c["hairStyle"],
                      hair_color=c["hairColor"], facial_style=c["facialStyle"])


def _option(name, option_id, n_choices):
    """An ``Options[]`` entry; the choice ids are synthetic (option id * 100 + index):
    the spike recorded the names, option ids and choice counts, not the choice ids."""
    return {"Name": name, "Id": option_id,
            "Choices": [{"Id": option_id * 100 + i, "Name": ""} for i in range(n_choices)]}


def _pairs(items):
    return [[v.inventory_type, v.display_id] for v in items]


def _item(slot, inventory_type, display_id=1000, **kw):
    return EquippedItem(slot=slot, entry=50000 + slot, name=f"Item {slot}",
                        display_id=display_id, inventory_type=inventory_type, **kw)


def test_model_id_uses_db_gender_as_is():
    assert model_id(6, 1) == 12  # Tauren female
    assert model_id(4, 0) == 7   # Night Elf male
    assert model_id(7, 1) == 14  # Gnome female
    assert model_id(1, 0) == 1 and model_id(11, 1) == 22  # Human male .. Draenei female
    # the three spike characters (each rendered with exactly this id)
    for name, expected in (("Vegivaca", 12), ("Sariel", 7), ("Pitokax", 14)):
        c = _spike(name)
        assert model_id(c["race"], c["gender"]) == expected, name


def test_option_name_map_is_the_spike_table():
    # spec §2 / spike page.html map=fixed; "Horn Style" is facial unless Tauren (see below)
    assert OPTION_NAME_MAP == {
        "Skin Color": "skin",
        "Face": "face",
        "Hair Style": "hair_style",
        "Hair Color": "hair_color",
        "Horn Color": "hair_color",
        "Horn Style": "facial_style",
        "Facial Hair": "facial_style",
        "Piercings": "facial_style",
        "Markings": "facial_style",
        "Features": "facial_style",
        "Earrings": "facial_style",
        "Tusks": "facial_style",
        "Hair": "facial_style",
    }


def test_map_customization_tauren_horns_by_name():
    # Tauren female (model 12) Options[] in the measured order (x1-vegivaca-mapfix: face is
    # 5th), plus an option the table does not know, inserted to check the order is kept.
    options_json = [
        _option("Skin Color", 74, 18),
        _option("Horn Style", 77, 12),
        _option("Horn Color", 78, 4),
        _option("Hair", 79, 5),
        _option("Body Size", 999, 3),
        _option("Face", 379, 8),
    ]
    # Vegivaca: skin 1, face 1, hairStyle 2, hairColor 1, facialStyle 2
    options, unknown = map_customization(options_json, 6, _appearance("Vegivaca"))
    assert options == [
        {"optionId": 74, "choiceId": 7401},    # skin 1
        {"optionId": 77, "choiceId": 7702},    # Horn Style = hairStyle 2 (Tauren, "Long")
        {"optionId": 78, "choiceId": 7801},    # Horn Color = hairColor 1
        {"optionId": 79, "choiceId": 7902},    # Hair (braids) = facialStyle 2 ("Loops")
        {"optionId": 999, "choiceId": 99900},  # unknown: first choice
        {"optionId": 379, "choiceId": 37901},  # face 1
    ]
    assert unknown == ["Body Size"]


def test_map_customization_human_names():
    looks = Appearance(skin=3, face=5, hair_style=7, hair_color=2, facial_style=4)
    human = [  # synthetic ids, the names every non-Tauren race shares
        _option("Skin Color", 1, 10),
        _option("Face", 2, 12),
        _option("Hair Style", 3, 13),
        _option("Hair Color", 4, 10),
        _option("Facial Hair", 5, 9),
    ]
    assert map_customization(human, 1, looks) == ([
        {"optionId": 1, "choiceId": 103},
        {"optionId": 2, "choiceId": 205},
        {"optionId": 3, "choiceId": 307},
        {"optionId": 4, "choiceId": 402},
        {"optionId": 5, "choiceId": 504},
    ], [])

    # the measured Night Elf male (r2-sariel) and Gnome female (x5-pitokax) option ids
    night_elf = [_option("Skin Color", 40, 13), _option("Face", 41, 18),
                 _option("Hair Style", 42, 12), _option("Hair Color", 43, 12),
                 _option("Facial Hair", 44, 6)]
    assert map_customization(night_elf, 4, _appearance("Sariel")) == ([
        {"optionId": 40, "choiceId": 4004}, {"optionId": 41, "choiceId": 4101},
        {"optionId": 42, "choiceId": 4200}, {"optionId": 43, "choiceId": 4300},
        {"optionId": 44, "choiceId": 4402},
    ], [])
    gnome = [_option("Skin Color", 85, 11), _option("Face", 86, 14),
             _option("Hair Style", 87, 12), _option("Hair Color", 88, 13),
             _option("Earrings", 89, 7)]
    assert map_customization(gnome, 7, _appearance("Pitokax")) == ([
        {"optionId": 85, "choiceId": 8503}, {"optionId": 86, "choiceId": 8600},
        {"optionId": 87, "choiceId": 8700}, {"optionId": 88, "choiceId": 8808},
        {"optionId": 89, "choiceId": 8904},  # Earrings = facialStyle 4 ("Full Earrings")
    ], [])


def test_map_customization_horn_style_is_facial_except_for_tauren():
    looks = Appearance(skin=0, face=0, hair_style=1, hair_color=0, facial_style=3)
    horns = [_option("Horn Style", 7, 6)]
    assert map_customization(horns, 6, looks)[0] == [{"optionId": 7, "choiceId": 701}]
    assert map_customization(horns, 11, looks)[0] == [{"optionId": 7, "choiceId": 703}]


def test_map_customization_out_of_range_choice_falls_back_and_odd_data_is_skipped():
    looks = Appearance(skin=20, face=-1, hair_style=0, hair_color=0, facial_style=0)
    options_json = [
        _option("Skin Color", 1, 4),          # 20 is past the end: Choices[0]
        _option("Face", 2, 4),                # negative never indexes from the end
        {"Name": "Hair Style", "Id": 3, "Choices": []},  # no choices: no entry
        {"Name": "Tattoo", "Id": 4, "Choices": []},      # unknown but empty: not reported
        "not an option",
        {"Name": "Hair Color", "Choices": [{"Id": 9}]},  # no option id
        {"Name": "Facial Hair", "Id": 5, "Choices": ["x", {"Id": 501}]},  # no usable choice
    ]
    options, unknown = map_customization(options_json, 1, looks)
    assert options == [{"optionId": 1, "choiceId": 100}, {"optionId": 2, "choiceId": 200}]
    assert unknown == []
    # an unknown name is reported once however often it appears
    twice = [_option("Body Size", 8, 2), _option("Body Size", 9, 2)]
    assert map_customization(twice, 1, looks) == (
        [{"optionId": 8, "choiceId": 800}, {"optionId": 9, "choiceId": 900}], ["Body Size"])
    assert map_customization([], 1, looks) == ([], [])


def test_equipped_item_needs_only_the_identifying_fields():
    item = EquippedItem(slot=15, entry=19364, name="Ashkandi",
                        display_id=32000, inventory_type=17)
    assert (item.item_class, item.subclass, item.quality, item.item_level) == (0, 0, 0, 0)
    assert item.stat_types == () and item.stat_values == ()
    assert item.socket_colors == () and item.spell_ids == () and item.spell_triggers == ()
    assert item.enchantments == {} and item.random_property_id == 0
    assert item.allowable_class == -1 and item.description == ""
    assert item.holy_res == item.arcane_res == 0


def test_viewer_items_tauren_warrior_matches_spike():
    # Vegivaca: two-hander + bow + tabard + shirt all sent; the bow is sent because the
    # off hand (slot 16) is empty. Exactly the items x1-vegivaca-mapfix rendered.
    items, omitted = viewer_items(_gear("Vegivaca"))
    assert _pairs(items) == [
        [1, 32493], [3, 28351], [4, 9995], [5, 33983], [6, 31924], [7, 28716],
        [8, 31914], [9, 33818], [10, 34298], [16, 34329], [17, 32000], [15, 33857],
        [19, 20621],
    ]
    assert [v.slot for v in items] == [0, 2, 3, 4, 5, 6, 7, 8, 9, 14, 15, 17, 18]
    assert omitted == [1, 10, 11, 12, 13]  # neck, rings, trinkets
    assert items[0] == ViewerItem(slot=0, inventory_type=1, display_id=32493)


def test_viewer_items_druid_matches_spike():
    # Sariel (r2-sariel): two-hander, no off hand, no ranged
    items, omitted = viewer_items(_gear("Sariel"))
    assert _pairs(items) == [
        [1, 27234], [3, 27236], [5, 31074], [6, 28177], [7, 27235], [8, 31070],
        [9, 30805], [10, 31071], [16, 27087], [17, 31817], [19, 20621],
    ]
    assert omitted == [1, 10, 11, 12, 13]


def test_viewer_items_rogue_daggers_keep_thrown_out():
    # Pitokax: dagger in each hand + a thrown axe (type 25 = the main-hand key). Sent, the
    # axe replaced the main-hand dagger (r3-pitokax); without it (x5) both daggers render.
    items, omitted = viewer_items(_gear("Pitokax"))
    assert _pairs(items) == [
        [1, 33743], [3, 27652], [4, 9906], [5, 31105], [6, 31339], [7, 31340],
        [8, 31109], [9, 31106], [10, 31503], [16, 29824], [13, 31606], [22, 3363],
    ]
    assert [v.slot for v in items][-2:] == [15, 16]
    assert omitted == [1, 10, 11, 12, 13, 17]


def test_viewer_items_bow_sent_when_offhand_empty_and_dropped_when_shield():
    gear = _gear("Vegivaca")  # two-hander in 15, bow (type 15) in 17, nothing in 16
    items, omitted = viewer_items(gear)
    assert [15, 33857] in _pairs(items) and 17 not in omitted

    shield = _item(16, 14, 40000, item_class=4, subclass=6)
    items, omitted = viewer_items(gear + [shield])
    assert [14, 40000] in _pairs(items)
    assert [15, 33857] not in _pairs(items)
    assert omitted == [1, 10, 11, 12, 13, 17]


def test_viewer_items_thrown_and_gun_only_without_a_main_hand():
    for ranged_type in (25, 26):  # thrown; gun/crossbow/wand
        ranged = _item(17, ranged_type, 7000)
        assert viewer_items([ranged]) == ([ViewerItem(17, ranged_type, 7000)], [])
        # an off-hand item does not block it (different hand key)
        items, omitted = viewer_items([ranged, _item(16, 23, 6000)])
        assert _pairs(items) == [[23, 6000], [ranged_type, 7000]] and omitted == []
        # a main-hand item does
        items, omitted = viewer_items([_item(15, 21, 5000), ranged])
        assert _pairs(items) == [[21, 5000]] and omitted == [17]


def test_viewer_items_offhand_types():
    def offhand(inventory_type):
        items, omitted = viewer_items([_item(16, inventory_type, 3363)])
        assert omitted == []
        return items[0].inventory_type

    assert offhand(14) == 14  # shield
    assert offhand(23) == 23  # held in off hand
    assert offhand(13) == 22  # one-hander
    assert offhand(22) == 22  # off-hand weapon
    assert offhand(17) == 22  # Titan's Grip two-hander
    # the main hand keeps its own type whatever it is
    for main_type in (13, 17, 21):
        assert viewer_items([_item(15, main_type)])[0][0].inventory_type == main_type


def test_viewer_items_relic_never_sent():
    relic = _item(17, 28, 4444)
    assert viewer_items([relic]) == ([], [17])
    items, omitted = viewer_items([_item(15, 17, 5000), relic])
    assert _pairs(items) == [[17, 5000]] and omitted == [17]
    # any other type in the ranged slot (should not happen) is left out too
    assert viewer_items([_item(17, 13)]) == ([], [17])


def test_viewer_items_sorted_by_slot_and_robe_sent_as_is():
    robe = _item(4, 20, 8888)
    gear = [g for g in reversed(_gear("Pitokax")) if g.slot != 4] + [robe]
    items, omitted = viewer_items(gear)
    assert [v.slot for v in items] == [0, 2, 3, 4, 5, 6, 7, 8, 9, 14, 15, 16]
    assert omitted == [1, 10, 11, 12, 13, 17]
    assert ViewerItem(4, 20, 8888) in items
    assert viewer_items([]) == ([], [])


# --- item tooltips (spec §3 G3, §6, §12) ---

# Enchant/gem fixtures: (enchant id, text, gem item id, gem colour mask or None = no
# GemProperties row). Colours are SocketColor masks: 1 meta, 2 red, 4 yellow, 8 blue.
_GEM_ROWS = (
    (3621, "+21 Critical Strike Rating and 3% Increased Critical Damage", 41398, 1),  # meta
    (3520, "+20 Strength", 40111, 2),               # red
    (3532, "+10 Strength and +15 Stamina", 40129, 10),  # purple: red | blue
    (3540, "+20 Critical Strike Rating", 40142, 4),  # yellow
    (3879, "+10 All Stats", 49110, 14),             # prismatic: red | yellow | blue
    (3600, "+12 Agility", 40200, None),             # gem enchant without gem data
)


def _tooltip_store(**kw):
    enchants = [Enchant(id=e, text=t, gem_item_id=g, visual_id=0) for e, t, g, _ in _GEM_ROWS]
    enchants += [
        Enchant(id=3307, text="+6 Stamina", gem_item_id=0, visual_id=0),
        Enchant(id=3368, text="Rune of the Fallen Crusader", gem_item_id=0, visual_id=151),
        Enchant(id=3729, text="Prismatic Socket", gem_item_id=0, visual_id=0),
        Enchant(id=2802, text="+$i Stamina", gem_item_id=0, visual_id=0),
        Enchant(id=2803, text="+$i Strength", gem_item_id=0, visual_id=0),
        Enchant(id=2930, text="+12 Spell Power", gem_item_id=0, visual_id=0),
        Enchant(id=2931, text="+8 Stamina", gem_item_id=0, visual_id=0),
    ]
    gems = [Gem(item_id=g, enchant_id=e, color_mask=c) for e, _, g, c in _GEM_ROWS if c is not None]
    defaults = dict(
        enchants=enchants,
        gems=gems,
        icons_by_display={33100: "inv_helmet_96"},
        random_properties=[RandomEnchant(id=14, suffix="of the Owl",
                                         enchant_ids=(2930, 2931), alloc=())],
        random_suffixes=[RandomEnchant(id=5, suffix="of the Bear",
                                       enchant_ids=(2802, 2803), alloc=(5259, 3506))],
        points={200: RandPropPoints(epic=(501, 372, 283, 214, 165),
                                    rare=(402, 303, 234, 175, 136),
                                    uncommon=(305, 226, 177, 138, 109))},
    )
    defaults.update(kw)
    return FakeStore(**defaults)


def test_tooltip_weapon_damage_speed_dps():
    sword = _item(15, 17, 33100, item_class=2, subclass=8, quality=4, item_level=200,
                  required_level=80, bonding=1, dmg_min=100.0, dmg_max=200.0, delay=2600,
                  description="Forged in fire.")
    t = tooltip(sword, _tooltip_store(), [sword])

    assert isinstance(t, Tooltip)
    assert (t.name, t.quality, t.quality_color) == ("Item 15", 4, "#a335ee")
    assert (t.ilvl, t.required_level, t.binding) == (200, 80, "Binds when picked up")
    assert (t.slot_name, t.type_name) == ("Two-Hand", "Sword")
    assert t.damage == "100 - 200 Damage"
    assert t.speed == "Speed 2.60"
    assert t.dps == "57.7"  # (100 + 200) / 2 / 2.6
    assert (t.description, t.icon) == ("Forged in fire.", "inv_helmet_96")

    assert dps(100, 200, 2600) == "57.7"
    assert dps(0, 0, 2600) is None
    assert dps(10, 20, 0) is None

    fire = tooltip(dataclasses.replace(sword, dmg_type=2, dmg_min=51.5, dmg_max=96.4),
                   _tooltip_store(), [])
    assert fire.damage == "52 - 96 Fire Damage"

    helm = _item(0, 1, 777, item_class=4, subclass=4, armor=1500, quality=3)
    h = tooltip(helm, _tooltip_store(), [helm])
    assert (h.damage, h.speed, h.dps) == (None, None, None)
    assert (h.armor, h.slot_name, h.type_name, h.binding) == (1500, "Head", "Plate", None)
    assert h.icon == ""  # display id unknown to the store
    # a ring (armour "Miscellaneous") shows no type line; the client shows none either
    ring = tooltip(_item(10, 11, item_class=4, subclass=0), _tooltip_store(), [])
    assert (ring.slot_name, ring.type_name) == ("Finger", None)
    # an unknown quality falls back to the common colour
    assert tooltip(_item(0, 1, quality=9), _tooltip_store(), []).quality_color == "#ffffff"


def test_tooltip_stats_and_resistances_formatting():
    item = _item(0, 1, stat_types=(4, 7, 32, 0, 99, 0, 0, 0, 0, 0),
                 stat_values=(16, -5, 20, 0, 3, 0, 0, 0, 0, 0),
                 fire_res=10, arcane_res=5, holy_res=0)
    t = tooltip(item, _tooltip_store(), [item])

    # zero-value columns are unused stat slots; an unknown stat type degrades to its id
    assert t.stats == ["+16 Strength", "-5 Stamina", "+20 Critical Strike Rating", "+3 Stat #99"]
    # resistances in spell-school order (holy, fire, nature, frost, shadow, arcane)
    assert t.resistances == ["+10 Fire Resistance", "+5 Arcane Resistance"]

    assert format_stat(4, 16) == "+16 Strength"
    assert format_stat(7, -5) == "-5 Stamina"
    assert format_stat(40, 7) == "+7 Stat #40"
    # short tuples (an EquippedItem built by hand) are fine
    assert tooltip(_item(0, 1), _tooltip_store(), []).stats == []


def test_tooltip_sockets_gems_and_bonus_active_only_when_all_filled():
    """The bonus is active only when every native socket holds a gem whose colour mask
    matches the socket (``Item::GemsFitSockets``); filled with a wrong colour is not enough."""
    store = _tooltip_store()
    helm = _item(0, 1, socket_colors=(1, 2, 8), socket_bonus=3307,
                 enchantments={2: 3621, 3: 3520, 4: 3532})
    t = tooltip(helm, store, [helm])
    assert t.sockets == [
        Socket("Meta", "+21 Critical Strike Rating and 3% Increased Critical Damage", ""),
        Socket("Red", "+20 Strength", ""),
        Socket("Blue", "+10 Strength and +15 Stamina", ""),
    ]
    assert (t.socket_bonus, t.socket_bonus_active) == ("+6 Stamina", True)

    def active(enchantments, colors=(1, 2, 8)):
        item = dataclasses.replace(helm, socket_colors=colors, enchantments=enchantments)
        return tooltip(item, store, [item]).socket_bonus_active

    # an empty socket
    empty = dataclasses.replace(helm, enchantments={2: 3621, 3: 3520})
    e = tooltip(empty, store, [empty])
    assert e.sockets[2] == Socket("Blue", None, "")
    assert e.socket_bonus_active is False
    assert active({2: 3621, 3: 3520, 4: 3540}) is False  # yellow gem in a blue socket
    assert active({2: 3520, 3: 3520, 4: 3532}) is False  # red gem in the meta socket
    assert active({2: 3879, 3: 3520, 4: 3532}) is False  # prismatic gem in the meta socket
    assert active({2: 3621, 3: 3621, 4: 3532}) is False  # meta gem in a red socket
    assert active({2: 3621, 3: 3879, 4: 3879}) is True   # prismatic gems fit any colour
    assert active({2: 3621, 3: 3600, 4: 3532}) is False  # no gem data: not a match
    assert active({2: 3621, 3: 9999, 4: 3532}) is False  # unknown enchant: not a match

    unknown = dataclasses.replace(helm, enchantments={2: 3621, 3: 3600, 4: 9999})
    assert [s.gem_text for s in tooltip(unknown, store, []).sockets] == [
        "+21 Critical Strike Rating and 3% Increased Critical Damage",
        "+12 Agility",      # the gem enchant's text, even without gem data
        "Enchant #9999",
    ]

    # a prismatic socket (belt buckle: PRISMATIC slot 6) after the native ones; only the
    # native sockets decide the bonus
    belt = _item(5, 6, socket_colors=(4, 0, 0), socket_bonus=3307,
                 enchantments={2: 3540, 3: 3879, 6: 3729})
    b = tooltip(belt, store, [belt])
    assert b.sockets == [Socket("Yellow", "+20 Critical Strike Rating", ""),
                         Socket("Prismatic", "+10 All Stats", "")]
    assert b.socket_bonus_active is True
    bare_buckle = dataclasses.replace(belt, enchantments={2: 3540, 6: 3729})
    assert tooltip(bare_buckle, store, []).sockets[1] == Socket("Prismatic", None, "")
    # a gem beyond the native sockets is listed even without the slot-6 marker
    stray = dataclasses.replace(belt, enchantments={2: 3540, 4: 3520})
    assert tooltip(stray, store, []).sockets[1] == Socket("Prismatic", "+20 Strength", "")

    plain = tooltip(_item(0, 1), store, [])
    assert (plain.sockets, plain.socket_bonus, plain.socket_bonus_active) == ([], None, False)

    assert [socket_color_name(m) for m in (1, 2, 4, 8, 0, 14)] == [
        "Meta", "Red", "Yellow", "Blue", "Prismatic", "Prismatic"]
    assert socket_color_name(3) == "Colour #3"


def test_tooltip_permanent_enchant_text():
    store = _tooltip_store()
    weapon = _item(15, 17, enchantments={0: 3368, 1: 2930})
    assert tooltip(weapon, store, [weapon]).enchant == "Rune of the Fallen Crusader"
    # a temporary enchant (slot 1: poisons, oils) is not the permanent enchant line
    temp_only = dataclasses.replace(weapon, enchantments={1: 2930})
    assert tooltip(temp_only, store, []).enchant is None
    assert tooltip(_item(0, 1), store, []).enchant is None


def test_tooltip_random_suffix_appends_name_and_stats():
    store = _tooltip_store()
    # rare legs, item level 200: suffix factor = RarePropertiesPoints[0] = 402
    legs = _item(6, 7, quality=3, item_level=200, random_property_id=-5)
    legs = dataclasses.replace(legs, name="Frostsaber Leggings")
    t = tooltip(legs, store, [legs])
    assert t.name == "Frostsaber Leggings of the Bear"  # the DBC name carries "of the"
    # amount = AllocationPct * factor / 10000, integer division as in the core
    assert t.suffix_stats == ["+211 Stamina", "+140 Strength"]  # 5259*402, 3506*402

    # the RandPropPoints row is missing: the amounts are unknown, the name is not
    no_points = tooltip(dataclasses.replace(legs, item_level=199), store, [])
    assert no_points.name == "Frostsaber Leggings of the Bear"
    assert no_points.suffix_stats == ["+? Stamina", "+? Strength"]

    # a random property (> 0) has fixed amounts in its enchant texts
    owl = dataclasses.replace(legs, name="Robe", random_property_id=14)
    o = tooltip(owl, store, [owl])
    assert (o.name, o.suffix_stats) == ("Robe of the Owl", ["+12 Spell Power", "+8 Stamina"])

    # unknown ids: the name stays, the id is shown
    lost = tooltip(dataclasses.replace(legs, random_property_id=-77), store, [])
    assert (lost.name, lost.suffix_stats) == ("Frostsaber Leggings", ["Random suffix #77"])
    lost = tooltip(dataclasses.replace(legs, random_property_id=88), store, [])
    assert (lost.name, lost.suffix_stats) == ("Frostsaber Leggings", ["Random property #88"])
    assert tooltip(dataclasses.replace(legs, random_property_id=0), store, []).suffix_stats == []


def test_suffix_factor_ports_generate_ench_suffix_factor():
    points = RandPropPoints(epic=(501, 372, 283, 214, 165),
                            rare=(402, 303, 234, 175, 136),
                            uncommon=(305, 226, 177, 138, 109))

    def factor(inventory_type, quality):
        return suffix_factor(_item(0, inventory_type, quality=quality), points)

    # column 0: head, body, chest, legs, 2H, robe
    assert [factor(t, 4) for t in (1, 4, 5, 7, 17, 20)] == [501] * 6
    # column 1: shoulders, waist, feet, hands, trinket
    assert [factor(t, 3) for t in (3, 6, 8, 10, 12)] == [303] * 5
    # column 2: neck, wrists, finger, shield, cloak, holdable
    assert [factor(t, 2) for t in (2, 9, 11, 14, 16, 23)] == [177] * 6
    # column 3: one-hand, main hand, off hand
    assert [factor(t, 4) for t in (13, 21, 22)] == [214] * 3
    # column 4: ranged, thrown, ranged right
    assert [factor(t, 3) for t in (15, 25, 26)] == [136] * 3
    # no points: non-equip, bag, tabard, ammo, quiver, relic, unknown types
    assert [factor(t, 4) for t in (0, 18, 19, 24, 27, 28, 29)] == [0] * 7
    # only uncommon, rare and epic have points
    assert [factor(1, q) for q in (0, 1, 5, 6, 7)] == [0] * 5
    assert suffix_factor(_item(0, 1, quality=4), None) is None


def test_tooltip_set_block_counts_equipped_pieces():
    store = _tooltip_store(
        item_sets=[ItemSet(id=843, name="Frostfire Garb",
                           items=(40415, 40416, 40417, 40418, 40419),
                           bonuses=((61063, 4), (61062, 2)))],
        spells=[Spell(id=61062, name="Increased Mana Regen", rank="", icon_id=0,
                      description="")],
    )
    head = _item(0, 1, item_set=843)
    chest = _item(4, 20, item_set=843)
    legs = _item(6, 7, item_set=843)
    other = _item(9, 10, item_set=900)
    gear = [head, chest, legs, other, _item(14, 16)]

    t = tooltip(head, store, gear)
    # counted like the core: every equipped item whose itemset is this set
    assert t.set_block == SetBlock(
        name="Frostfire Garb", have=3, total=5,
        bonuses=[(2, "Increased Mana Regen"), (4, "Spell #61063")],
    )
    # the whole tooltip is plain data (the page embeds it as JSON)
    json.dumps(dataclasses.asdict(t))

    # unknown set: its id, the count, no total (0) and no bonuses
    assert tooltip(other, store, gear).set_block == SetBlock("Set #900", 1, 0, [])
    assert tooltip(_item(14, 16), store, gear).set_block is None
    # the item itself always counts, even when it is not in the list
    assert tooltip(head, store, []).set_block.have == 1


def test_tooltip_equip_use_chance_prefixes():
    store = _tooltip_store(spells=[
        Spell(id=1234, name="Increase Spell Dam 23", rank="", icon_id=0, description=""),
        Spell(id=5678, name="Berserking", rank="Rank 2", icon_id=0, description=""),
        Spell(id=9012, name="Fiery Weapon", rank="", icon_id=0, description=""),
        Spell(id=3333, name="Learning", rank="", icon_id=0, description=""),
        Spell(id=4444, name="Quick Use", rank="", icon_id=0, description=""),
    ])
    trinket = _item(12, 12, spell_ids=(1234, 5678, 9012, 3333, 4444),
                    spell_triggers=(1, 0, 2, 6, 5))
    t = tooltip(trinket, store, [trinket])
    # ITEM_SPELLTRIGGER_ON_EQUIP 1, ON_USE 0, CHANCE_ON_HIT 2; other triggers are skipped
    assert t.equips == [
        "Equip: Increase Spell Dam 23",
        "Use: Berserking (Rank 2)",
        "Chance on hit: Fiery Weapon",
    ]
    # an empty spell column (id 0, trigger 0) is no "Use:" line
    blank = _item(12, 12, spell_ids=(0, 0, 0, 0, 0), spell_triggers=(0, 0, 0, 0, 0))
    assert tooltip(blank, store, []).equips == []


def test_tooltip_degrades_to_ids_when_store_missing():
    store = FakeStore(missing=list(dbc.DBC_FILES))
    assert store.ok() is False
    item = _item(0, 1, 33100, enchantments={0: 2564, 2: 3520}, socket_colors=(2, 0, 0),
                 socket_bonus=3307, spell_ids=(1234, 0, 0, 0, 0),
                 spell_triggers=(1, 0, 0, 0, 0), item_set=843, random_property_id=-5)
    t = tooltip(item, store, [item])

    assert t.enchant == "Enchant #2564"
    assert t.sockets == [Socket("Red", "Enchant #3520", "")]
    assert (t.socket_bonus, t.socket_bonus_active) == ("Enchant #3307", False)
    assert t.equips == ["Equip: Spell #1234"]
    assert t.set_block == SetBlock("Set #843", 1, 0, [])
    assert (t.name, t.suffix_stats) == ("Item 0", ["Random suffix #5"])
    assert t.icon == ""
    # the item's own data still shows
    assert (t.slot_name, t.ilvl) == ("Head", 0)
    # an enchant whose text is empty also degrades to its id
    blank = _tooltip_store(enchants=[Enchant(id=2564, text="", gem_item_id=0, visual_id=0)])
    assert tooltip(item, blank, []).enchant == "Enchant #2564"


# --- DB layer: search and character collection (spec §6) ---


class FakeCursor:
    def __init__(self, conn):
        self._conn = conn
        self._rows: list = []
        self.description = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self._conn.executed.append((sql, params))
        if self._conn.fail_at == len(self._conn.executed):
            raise mysql.connector.Error("boom")
        assert self._conn.results, f"unexpected extra query: {sql}"
        rows, columns = self._conn.results.pop(0)
        self._rows = list(rows)
        self.description = [(name, 3, None, None, None, None, 1, 0, 45) for name in columns]

    def fetchall(self):
        rows, self._rows = self._rows, []
        return rows

    def fetchone(self):
        return self._rows.pop(0) if self._rows else None


class FakeConn:
    """Answers each ``execute`` with the next queued ``(rows, column names)``."""

    def __init__(self, results, *, fail_at=None):
        self.results = [r if isinstance(r, tuple) else (r, ()) for r in results]
        self.executed: list[tuple[str, tuple | None]] = []
        self.fail_at = fail_at  # 1-based execute call that raises
        self.closed = False

    def cursor(self, *args, **kwargs):
        return FakeCursor(self)

    def close(self):
        self.closed = True


DB = dict(host="db", port=3306, user="u", password="p")


def _install_db(monkeypatch, results, **kw):
    conn = FakeConn(results, **kw)
    calls = []

    def connect(**kwargs):
        calls.append(kwargs)
        return conn

    monkeypatch.setattr(armory.mysql.connector, "connect", connect)
    return conn, calls


def _expected_connect():
    return [dict(DB, connection_timeout=2, autocommit=True)]


def test_search_normalises_and_escapes_prefix_and_orders_humans_first(monkeypatch):
    conn, calls = _install_db(monkeypatch, [[
        (2506, "Vegivaca", 60, 6, 1, 0),
        (9001, "Vegibot", 70, 2, 3, 1),
    ]])

    hits = search_characters("  vEGI% ", **DB)

    assert calls == _expected_connect()
    [(sql, params)] = conn.executed
    assert params == ("Vegi\\%%",)
    assert sql.startswith("SELECT ")
    # MySQL text ESCAPE '\\' = one backslash ('\' alone would swallow the quote)
    assert "c.name LIKE %s ESCAPE '\\\\'" in sql
    assert "a.username <> 'ahbot'" in sql
    assert "ORDER BY (a.username LIKE 'RNDBOT%%'), c.level DESC, c.name LIMIT 20" in sql
    assert "LOWER(" not in sql.upper()
    assert conn.closed
    # humans first is the SQL's order; the rows keep it
    assert hits == [
        SearchHit(guid=2506, name="Vegivaca", level=60, race=6, class_id=1,
                  class_name="Warrior", class_color=wr.CLASS_COLORS["Warrior"],
                  race_name="Tauren", is_bot=False),
        SearchHit(guid=9001, name="Vegibot", level=70, race=2, class_id=3,
                  class_name="Hunter", class_color=wr.CLASS_COLORS["Hunter"],
                  race_name="Orc", is_bot=True),
    ]


def test_search_short_query_runs_no_sql(monkeypatch):
    def connect(**kwargs):
        raise AssertionError("no connection for a short query")

    monkeypatch.setattr(armory.mysql.connector, "connect", connect)
    assert search_characters(" v ", **DB) == []
    assert search_characters("", **DB) == []


def test_search_db_error_propagates_and_closes(monkeypatch):
    conn, _ = _install_db(monkeypatch, [], fail_at=1)
    with pytest.raises(mysql.connector.Error):
        search_characters("Vegi", **DB)
    assert conn.closed


# One complete query-2 row: every column non-default, keyed by column name.
_ITEM_VALUES = {
    "slot": 4, "entry": 19374, "name": "Bracers of Arcane Accuracy", "displayid": 33100,
    "InventoryType": 9, "class": 4, "subclass": 1, "Quality": 4, "ItemLevel": 75,
    "RequiredLevel": 60, "bonding": 1, "armor": 61, "delay": 2600, "dmg_min1": 101.5,
    "dmg_max1": 188.25, "dmg_type1": 2,
    **{f"stat_type{i}": 2 + i for i in range(1, 11)},
    **{f"stat_value{i}": 10 * i for i in range(1, 11)},
    "holy_res": 1, "fire_res": 2, "nature_res": 3, "frost_res": 4, "shadow_res": 5,
    "arcane_res": 6, "socketColor_1": 1, "socketColor_2": 2, "socketColor_3": 8,
    "socketBonus": 3312, "itemset": 213,
    **{f"spellid_{i}": 18000 + i for i in range(1, 6)},
    **{f"spelltrigger_{i}": i % 3 for i in range(1, 6)},
    "MaxDurability": 35, "AllowableClass": 400, "description": "Crackles with power.",
    "enchantments": "2564 0 0 0 0 0 3375 0 0 " + "0 0 0 " * 9, "randomPropertyId": -14,
}


def _item_row(**overrides):
    values = {**_ITEM_VALUES, **overrides}
    return tuple(values[column.split(".", 1)[1]] for column in EQUIPMENT_COLUMNS)


def test_equipment_columns_are_the_spec_query_2_columns():
    names = [column.split(".", 1)[1] for column in EQUIPMENT_COLUMNS]
    assert len(names) == len(set(names))
    assert set(names) == set(_ITEM_VALUES)


def test_equipped_item_maps_every_column_of_a_full_row():
    item = equipped_item(_item_row())
    assert item == EquippedItem(
        slot=4, entry=19374, name="Bracers of Arcane Accuracy", display_id=33100,
        inventory_type=9, item_class=4, subclass=1, quality=4, item_level=75,
        required_level=60, bonding=1, armor=61, delay=2600, dmg_min=101.5,
        dmg_max=188.25, dmg_type=2,
        stat_types=(3, 4, 5, 6, 7, 8, 9, 10, 11, 12),
        stat_values=(10, 20, 30, 40, 50, 60, 70, 80, 90, 100),
        holy_res=1, fire_res=2, nature_res=3, frost_res=4, shadow_res=5, arcane_res=6,
        socket_colors=(1, 2, 8), socket_bonus=3312, item_set=213,
        spell_ids=(18001, 18002, 18003, 18004, 18005), spell_triggers=(1, 2, 0, 1, 2),
        max_durability=35, allowable_class=400, description="Crackles with power.",
        enchantments={0: 2564, 2: 3375}, random_property_id=-14,
    )
    # every field came from the row: none is still at its default
    bare = EquippedItem(slot=0, entry=0, name="", display_id=0, inventory_type=0)
    for f in dataclasses.fields(EquippedItem):
        assert getattr(item, f.name) != getattr(bare, f.name), f.name


def test_equipped_item_null_resistances_are_zero():
    nulls = {f"{school}_res": None for school in
             ("holy", "fire", "nature", "frost", "shadow", "arcane")}
    item = equipped_item(_item_row(**nulls))
    assert (item.holy_res, item.fire_res, item.nature_res,
            item.frost_res, item.shadow_res, item.arcane_res) == (0, 0, 0, 0, 0, 0)


def test_equipped_item_is_hashable_and_compares_enchantments():
    a = EquippedItem(slot=0, entry=1, name="x", display_id=2, inventory_type=1,
                     enchantments={0: 2564})
    assert hash(a) == hash(dataclasses.replace(a, enchantments={}))
    assert a != dataclasses.replace(a, enchantments={})


# Query 1 (header) in SELECT order: guid, name, username, is_bot, race, gender, class,
# level, online, logout_time, skin, face, hairStyle, hairColor, facialStyle,
# talentGroupsCount, activeTalentGroup. Vegivaca = the spike's Tauren warrior.
_VEGIVACA = (2506, "Vegivaca", "EDUARDO", 0, 6, 1, 1, 60, 0, 1716000000,
             1, 1, 2, 1, 2, 2, 1)
_STATS_SELECT = STATS_COLUMNS[1:]  # every character_stats column but guid


def _vegivaca_results(*, stats=(), glyphs=None, items=None, avg=Decimal("37")):
    return [
        [_VEGIVACA],
        items if items is not None else [
            _item_row(slot=0, entry=19372, name="Helm of Endless Rage", displayid=32493,
                      InventoryType=1, enchantments="2583 0 0 0 0 0 " + "0 0 0 " * 10),
            _item_row(slot=15, entry=19364, name="Ashkandi", displayid=32000,
                      InventoryType=17, enchantments=""),
        ],
        [(avg,)],
        [(12294, 1), (12296, 3), (12345, 2)],
        glyphs if glyphs is not None else [(0, 762, 0, None, 0, 0, 0)],
        (list(stats), _STATS_SELECT),
    ]


def test_collect_character_returns_none_for_unknown_guid(monkeypatch):
    conn, calls = _install_db(monkeypatch, [[]])
    assert collect_character(999, **DB, store=None) is None
    assert calls == _expected_connect()
    assert len(conn.executed) == 1 and conn.executed[0][1] == (999,)
    assert conn.closed


def test_collect_character_assembles_view(monkeypatch):
    conn, calls = _install_db(monkeypatch, _vegivaca_results())

    view = collect_character(2506, **DB, store=None)

    assert isinstance(view, CharacterView)
    assert calls == _expected_connect() and conn.closed
    assert all(params == (2506,) for _, params in conn.executed)
    assert view.header == Header(
        guid=2506, name="Vegivaca", account="EDUARDO", is_bot=False, race=6,
        race_name="Tauren", gender=1, class_id=1, class_name="Warrior",
        class_color=wr.CLASS_COLORS["Warrior"], faction="Horde",
        faction_color=wr.FACTION_COLORS["Horde"], level=60, online=False,
        logout_time=1716000000,
        appearance=Appearance(skin=1, face=1, hair_style=2, hair_color=1, facial_style=2),
    )
    assert [(i.slot, i.entry, i.name, i.display_id, i.inventory_type) for i in view.items] == [
        (0, 19372, "Helm of Endless Rage", 32493, 1),
        (15, 19364, "Ashkandi", 32000, 17),
    ]
    assert view.items[0] == equipped_item(_vegivaca_results()[1][0])
    assert view.items[0].enchantments == {0: 2583}
    assert view.items[1].enchantments == {}
    assert view.avg_ilvl == 37 and type(view.avg_ilvl) is int
    assert view.specs == ({12294, 12296}, {12296, 12345})
    assert (view.spec_count, view.active_spec) == (2, 1)
    # talent group 0 from its row (NULL column -> 0); group 1 has no row
    assert view.glyphs == ((762, 0, 0, 0, 0, 0), (0, 0, 0, 0, 0, 0))
    assert view.stats_row is None
    assert view.stats_columns == _STATS_SELECT
    hash(view)  # frozen view: hashable despite its list/set fields


def test_collect_character_stats_row_and_bot_header(monkeypatch):
    stats = tuple(v for k, v in _stats_row().items() if k != "guid")
    bot = (7001, "Rndhero", "RNDBOT42", 1, 10, 0, 8, 80, 1, 0, 0, 0, 0, 0, 0, 1, 0)
    results = _vegivaca_results(stats=[stats], items=[], avg=None,
                                glyphs=[(1, 1, 2, 3, 4, 5, 6), (0, 7, 8, 9, 10, 11, 12)])
    results[0] = [bot]
    _install_db(monkeypatch, results)

    view = collect_character(7001, **DB, store=None)

    h = view.header
    assert (h.account, h.is_bot, h.online, h.faction, h.class_name) == (
        "RNDBOT42", True, True, "Horde", "Mage")
    assert view.items == [] and view.avg_ilvl is None
    assert view.glyphs == ((7, 8, 9, 10, 11, 12), (1, 2, 3, 4, 5, 6))
    assert (view.spec_count, view.active_spec) == (1, 0)
    assert view.stats_row == stats and view.stats_columns == _STATS_SELECT
    # what Task 10 does with it: the saved stats by column name, mana for a mage
    shown = dict(stats_rows(view.stats_row, view.stats_columns, class_id=h.class_id))
    assert shown["Health"] == "12345" and shown["Mana"] == "6789"


def test_collect_character_runs_only_selects(monkeypatch):
    conn, calls = _install_db(monkeypatch, _vegivaca_results())
    collect_character(2506, **DB, store=None)
    assert len(calls) == 1  # one connection
    assert 1 <= len(conn.executed) <= 8
    for sql, _ in conn.executed:
        assert sql.lstrip().upper().startswith("SELECT "), sql
        assert ";" not in sql
    sqls = [sql for sql, _ in conn.executed]
    equipment = next(s for s in sqls if "it.InventoryType" in s)
    assert "ci.bag = 0 AND ci.slot < 19" in equipment
    stats_sql = next(s for s in sqls if "character_stats" in s)
    assert all(column in stats_sql for column in _STATS_SELECT)


def test_collect_character_db_error_propagates_and_closes(monkeypatch):
    conn, _ = _install_db(monkeypatch, _vegivaca_results(), fail_at=2)
    with pytest.raises(mysql.connector.Error):
        collect_character(2506, **DB, store=None)
    assert conn.closed


def test_avg_ilvl_uses_the_players_ranking_expression(monkeypatch):
    """The paper-doll average and the Players ranking come from one SQL expression."""
    conn, _ = _install_db(monkeypatch, [[], [(0, 0, 0, 0)], [], []])
    players.collect_players(**DB)
    ranking = next(sql for sql, _ in conn.executed if "AVG(" in sql)

    conn, _ = _install_db(monkeypatch, _vegivaca_results())
    collect_character(2506, **DB, store=None)
    avg_sql = next(sql for sql, _ in conn.executed if "AVG(" in sql)

    assert "ROUND(AVG(it.ItemLevel))" in ranking and "ROUND(AVG(it.ItemLevel))" in avg_sql
    joins = avg_sql[avg_sql.index("LEFT JOIN"):avg_sql.index("WHERE")]
    assert "ci.slot < 19" in joins and joins in ranking
    assert "WHERE c.guid = %s" in avg_sql
