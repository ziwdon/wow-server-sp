"""Armory: reference tables and the pure character-data helpers (no DB, no network).

The DBC-backed helpers take any object with the ``DbcStore`` accessors; these tests use a
small in-memory fake built from the real ``dbc`` record types, never real DBC files.
The viewer-mapping tests use the three real characters of the 2026-10-29 spike
(``tests/data/armory/spike_characters.json``).
"""
from __future__ import annotations

import json
from pathlib import Path

from app.services import wow_reference as wr
from app.services.armory import (
    ENCHANT_BONUS,
    ENCHANT_PERM,
    ENCHANT_PRISMATIC,
    ENCHANT_SOCKETS,
    OPTION_NAME_MAP,
    Appearance,
    EquippedItem,
    GlyphRow,
    ViewerItem,
    glyph_rows,
    map_customization,
    model_id,
    normalize_search,
    parse_enchantments,
    split_specs,
    stats_rows,
    talent_trees,
    viewer_items,
)
from app.services.dbc import Glyph, Spell, Talent, TalentTab


class FakeStore:
    """Just the ``DbcStore`` accessors the helpers use (ids are positional, like ``id_``)."""

    def __init__(self, *, talents=(), tabs=(), spells=(), icons=None, glyphs=()):
        self._talents = {t.id: t for t in talents}
        self._tabs = {t.id: t for t in tabs}
        self._spells = {s.id: s for s in spells}
        self._icons = dict(icons or {})
        self._glyphs = {g.id: g for g in glyphs}

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
