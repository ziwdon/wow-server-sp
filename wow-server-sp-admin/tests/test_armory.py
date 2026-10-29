"""Armory: reference tables and the pure character-data helpers (no DB, no network).

The DBC-backed helpers take any object with the ``DbcStore`` accessors; these tests use a
small in-memory fake built from the real ``dbc`` record types, never real DBC files.
"""
from __future__ import annotations

from app.services import wow_reference as wr
from app.services.armory import (
    ENCHANT_BONUS,
    ENCHANT_PERM,
    ENCHANT_PRISMATIC,
    ENCHANT_SOCKETS,
    GlyphRow,
    glyph_rows,
    normalize_search,
    parse_enchantments,
    split_specs,
    stats_rows,
    talent_trees,
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
