"""WDBC reader and DbcStore (Armory reference data) against synthetic DBC files.

The fixtures are written into ``tmp_path`` at test time (the sandbox mounts the source
read-only, and no Blizzard file is ever checked in). Every synthetic table uses the real
3.3.5 field count of its DBC, and the tests place values at field indices written out
here as literals, taken independently from the pinned core's ``DBCStructure.h`` /
``DBCfmt.h`` (and the matching ``data/sql/base/db_world/*_dbc.sql`` column lists): a wrong
index constant in ``dbc.py`` makes a test fail instead of agreeing with itself.

``write_dbc`` and ``write_synthetic_dbc_dir`` are importable helpers (the Armory browser
check builds its client-data fixture with them).
"""
from __future__ import annotations

import os
import struct
import threading
from pathlib import Path

import pytest

from app.services import dbc
from app.services.dbc import DBC_FILES, DbcError, DbcStore, read_dbc


# --- fixture writers ---


def write_dbc(path: Path, fields: int, rows: list[tuple], strings: list[str] = ()) -> dict[str, int]:
    """Write a valid WDBC file and return ``{string: offset}``.

    The string block starts with ``\\0`` (offset 0 is the empty string), then holds
    ``strings`` in order, then any other ``str`` found in ``rows``. In a row, a ``str`` is
    written as its offset, an ``int`` as uint32 (a negative one as two's complement) and a
    ``float`` as float32; a row shorter than ``fields`` is zero-padded.
    """
    block = bytearray(b"\0")
    offsets: dict[str, int] = {"": 0}

    def offset_of(text: str) -> int:
        if text not in offsets:
            offsets[text] = len(block)
            block.extend(text.encode("utf-8") + b"\0")
        return offsets[text]

    for text in strings:
        offset_of(text)
    body = bytearray()
    for row in rows:
        if len(row) > fields:
            raise ValueError(f"row has {len(row)} values for {fields} fields")
        for value in tuple(row) + (0,) * (fields - len(row)):
            if isinstance(value, str):
                body += struct.pack("<I", offset_of(value))
            elif isinstance(value, float):
                body += struct.pack("<f", value)
            else:
                body += struct.pack("<I", value & 0xFFFFFFFF)
    header = struct.pack("<4s4I", b"WDBC", len(rows), fields, fields * 4, len(block))
    Path(path).write_bytes(header + bytes(body) + bytes(block))
    return offsets


def record(fields: int, values: dict[int, object]) -> tuple:
    """A row of ``fields`` zeros with ``values`` placed at their field indices."""
    row: list[object] = [0] * fields
    for index, value in values.items():
        row[index] = value
    return tuple(row)


# Real 3.3.5 field counts (len of the DBCfmt.h format string; SpellIcon.dbc is not loaded
# by the core, its 3.3.5 layout is ID + TextureFilename).
FIELDS = {
    "Talent.dbc": 23,
    "TalentTab.dbc": 24,
    "Spell.dbc": 234,
    "SpellIcon.dbc": 2,
    "GlyphProperties.dbc": 4,
    "SpellItemEnchantment.dbc": 38,
    "GemProperties.dbc": 5,
    "ItemRandomProperties.dbc": 24,
    "ItemRandomSuffix.dbc": 29,
    "ItemSet.dbc": 53,
    "ItemDisplayInfo.dbc": 25,
}


def synthetic_rows() -> dict[str, list[tuple]]:
    """A tiny consistent client-data set: one mage talent, a few spells, glyphs, an
    enchant + gem, a random property and suffix, an item set and two item icons."""
    f = FIELDS
    return {
        # TalentEntry: 0 TalentID, 1 TalentTab, 2 Row, 3 Col, 4-8 RankID, 9-12 spellRank
        "Talent.dbc": [
            record(f["Talent.dbc"], {0: 23, 1: 61, 2: 2, 3: 3, 4: 100, 5: 101}),
        ],
        # TalentTabEntry: 0 ID, 1-16 name, 18 spellicon, 20 ClassMask, 22 tabpage
        "TalentTab.dbc": [
            record(f["TalentTab.dbc"], {0: 61, 1: "Frost", 2: "Givre", 18: 7, 20: 128, 22: 2}),
        ],
        # SpellEntry: 0 Id, 133 SpellIconID, 136-151 SpellName, 153-168 Rank,
        # 170-185 Description (enUS = first slot of each)
        "Spell.dbc": [
            record(f["Spell.dbc"], {0: 100, 133: 7, 136: "Frostbite", 137: "Morsure",
                                    153: "Rank 1", 170: "Chills the target."}),
            record(f["Spell.dbc"], {0: 101, 133: 7, 136: "Frostbite", 153: "Rank 2",
                                    170: "Freezes the target."}),
            record(f["Spell.dbc"], {0: 56368, 133: 8, 136: "Glyph of Fireball"}),
        ],
        # SpellIcon: 0 ID, 1 TextureFilename
        "SpellIcon.dbc": [
            record(f["SpellIcon.dbc"], {0: 7, 1: "Interface\\Icons\\Spell_Nature_X"}),
            record(f["SpellIcon.dbc"], {0: 8, 1: "Interface/Icons/Spell_Fire_FlameBolt.blp"}),
        ],
        # GlyphPropertiesEntry: 0 Id, 1 SpellId, 2 TypeFlags (bit 0 set = minor)
        "GlyphProperties.dbc": [
            record(f["GlyphProperties.dbc"], {0: 2, 1: 56368, 2: 0, 3: 99}),
            record(f["GlyphProperties.dbc"], {0: 3, 1: 101, 2: 1}),
        ],
        # SpellItemEnchantmentEntry: 0 ID, 14-29 description, 31 aura_id, 33 GemID
        "SpellItemEnchantment.dbc": [
            record(f["SpellItemEnchantment.dbc"], {0: 3368, 14: "Rune of the Fallen Crusader",
                                                   31: 151}),
            record(f["SpellItemEnchantment.dbc"], {0: 3520, 14: "+20 Strength", 33: 40111}),
            record(f["SpellItemEnchantment.dbc"], {0: 2930, 14: "+12 Spell Power"}),
            record(f["SpellItemEnchantment.dbc"], {0: 2931, 14: "+8 Stamina"}),
        ],
        # GemPropertiesEntry: 0 ID, 1 spellitemenchantement, 4 color
        "GemProperties.dbc": [
            record(f["GemProperties.dbc"], {0: 1361, 1: 3520, 2: 1, 4: 2}),
        ],
        # ItemRandomPropertiesEntry: 0 ID, 2-6 Enchantment, 7-22 Name
        "ItemRandomProperties.dbc": [
            record(f["ItemRandomProperties.dbc"], {0: 14, 1: "internal", 2: 2930, 3: 2931,
                                                   7: "of the Owl"}),
        ],
        # ItemRandomSuffixEntry: 0 ID, 1-16 Name, 18 InternalName, 19-23 Enchantment,
        # 24-28 AllocationPct
        "ItemRandomSuffix.dbc": [
            record(f["ItemRandomSuffix.dbc"], {0: 5, 1: "of the Bear", 18: "bear",
                                               19: 2931, 20: 2930, 24: 5259, 25: 3506}),
        ],
        # ItemSetEntry: 0 ID, 1-16 name, 18-27 itemId (+28-34 unused), 35-42 spells,
        # 43-50 items_to_triggerspell
        "ItemSet.dbc": [
            record(f["ItemSet.dbc"], {0: 843, 1: "Frostfire Garb", 18: 40415, 19: 40416,
                                      20: 40417, 35: 61062, 36: 61063, 43: 2, 44: 4}),
        ],
        # ItemDisplayInfoEntry: 0 ID, 1-2 modelName, 3-4 modelTexture, 5-6 inventoryIcon
        "ItemDisplayInfo.dbc": [
            record(f["ItemDisplayInfo.dbc"], {0: 5, 1: "model.mdx", 5: "INV_Chest_Plate_12",
                                              6: "INV_Other"}),
            record(f["ItemDisplayInfo.dbc"], {0: 9, 5: "Inv_Sword_04"}),
        ],
    }


def write_synthetic_dbc_dir(dbc_dir: Path, skip: tuple[str, ...] = ()) -> Path:
    """Write the synthetic set into ``dbc_dir`` (created), leaving out ``skip``."""
    dbc_dir = Path(dbc_dir)
    dbc_dir.mkdir(parents=True, exist_ok=True)
    for name, rows in synthetic_rows().items():
        if name not in skip:
            write_dbc(dbc_dir / name, FIELDS[name], rows)
    return dbc_dir


@pytest.fixture(autouse=True)
def _fresh_store():
    dbc.reset_store()
    yield
    dbc.reset_store()


@pytest.fixture
def client_data(tmp_path: Path) -> Path:
    write_synthetic_dbc_dir(tmp_path / "dbc")
    return tmp_path


# --- read_dbc ---


def test_read_dbc_parses_header_rows_and_strings(tmp_path):
    path = tmp_path / "Test.dbc"
    offsets = write_dbc(path, 3, [(1, "Alpha", 1.5), (2, "Ñandú", -2)], ["Zero"])
    f = read_dbc(path)

    assert (f.records, f.fields, f.record_size) == (2, 3, 12)
    assert offsets["Zero"] == 1
    assert f.string(0) == ""
    assert f.string(1) == "Zero"
    assert f.string(offsets["Alpha"]) == "Alpha"
    assert f.string(offsets["Alpha"] + 2) == "pha"
    assert f.string(10_000) == ""
    assert f.row(0) == (1, offsets["Alpha"], struct.unpack("<I", struct.pack("<f", 1.5))[0])
    assert f.row(1)[2] == 0xFFFFFFFE  # fields are uint32; callers reinterpret
    assert f.int(1, 0) == 2
    assert f.float(0, 2) == 1.5
    assert f.str(0, 1) == "Alpha"
    assert f.str(1, 1) == "Ñandú"
    with pytest.raises(IndexError):
        f.row(2)
    with pytest.raises(IndexError):
        f.int(0, 3)


def test_read_dbc_decodes_invalid_utf8_with_replacement(tmp_path):
    path = tmp_path / "Bad.dbc"
    header = struct.pack("<4s4I", b"WDBC", 1, 1, 4, 5)
    path.write_bytes(header + struct.pack("<I", 1) + b"\0a\xffb\0")
    assert read_dbc(path).str(0, 0) == "a�b"


def test_read_dbc_rejects_bad_magic_and_truncated_file(tmp_path):
    good = tmp_path / "Good.dbc"
    write_dbc(good, 2, [(1, "x"), (2, "y")])
    data = good.read_bytes()

    cases = {
        "magic": b"WDBX" + data[4:],
        "truncated_body": data[:-3],
        "short_header": data[:12],
        "empty": b"",
        # record_size disagrees with 4-byte fields
        "inconsistent": struct.pack("<4s4I", b"WDBC", 2, 2, 12, 5) + data[20:],
    }
    for name, payload in cases.items():
        path = tmp_path / f"{name}.dbc"
        path.write_bytes(payload)
        with pytest.raises(DbcError):
            read_dbc(path)


# --- DbcStore ---


def test_dbc_files_are_the_eleven_spec_tables_in_order():
    assert DBC_FILES == (
        "Talent.dbc",
        "TalentTab.dbc",
        "Spell.dbc",
        "SpellIcon.dbc",
        "GlyphProperties.dbc",
        "SpellItemEnchantment.dbc",
        "GemProperties.dbc",
        "ItemRandomProperties.dbc",
        "ItemRandomSuffix.dbc",
        "ItemSet.dbc",
        "ItemDisplayInfo.dbc",
    )


def test_store_talents_tabs_and_spell_lookup_from_synthetic_files(client_data):
    store = DbcStore(client_data)

    assert store.missing() == []
    assert store.ok() is True
    assert store.talents() == {23: dbc.Talent(id=23, tab=61, tier=2, column=3, ranks=(100, 101))}
    assert store.talent_tabs() == {
        61: dbc.TalentTab(id=61, name="Frost", icon_id=7, class_mask=128, order=2)
    }
    frostbite = store.spell(101)
    assert frostbite == dbc.Spell(
        id=101, name="Frostbite", rank="Rank 2", icon_id=7, description="Freezes the target."
    )
    assert store.spell(101).name == "Frostbite"
    assert store.spell(100).rank == "Rank 1"
    assert store.spell(100).description == "Chills the target."
    assert store.spell(101) is frostbite  # memoised
    assert store.spell(999) is None
    assert store.spell(0) is None


def test_store_glyphs_enchants_gems_random_enchants_and_item_sets(client_data):
    store = DbcStore(client_data)

    assert store.glyph(2) == dbc.Glyph(id=2, spell_id=56368, type_major=True)
    assert store.glyph(3) == dbc.Glyph(id=3, spell_id=101, type_major=False)
    assert store.glyph(4) is None

    assert store.enchant(3368) == dbc.Enchant(
        id=3368, text="Rune of the Fallen Crusader", gem_item_id=0, visual_id=151
    )
    assert store.enchant(3520).gem_item_id == 40111
    assert store.enchant(1) is None

    # GemProperties is keyed by its own id; gem() is keyed by the gem *item*, found through
    # the gem enchant's GemID (m_src_itemID), as the core links a socket enchant to its item.
    assert store.gem(40111) == dbc.Gem(item_id=40111, enchant_id=3520, color_mask=2)
    assert store.gem(1361) is None

    assert store.random_property(14) == dbc.RandomEnchant(
        id=14, suffix="of the Owl", enchant_ids=(2930, 2931), alloc=()
    )
    assert store.random_suffix(5) == dbc.RandomEnchant(
        id=5, suffix="of the Bear", enchant_ids=(2931, 2930), alloc=(5259, 3506)
    )
    assert store.random_property(5) is None
    assert store.random_suffix(14) is None

    assert store.item_set(843) == dbc.ItemSet(
        id=843,
        name="Frostfire Garb",
        items=(40415, 40416, 40417),
        bonuses=((61062, 2), (61063, 4)),
    )
    assert store.item_set(1) is None


def test_spell_icon_and_item_icon_are_lowercase_basenames(client_data):
    store = DbcStore(client_data)

    assert store.spell_icon(7) == "spell_nature_x"
    assert store.spell_icon(8) == "spell_fire_flamebolt"
    assert store.spell_icon(12345) == ""
    assert store.item_icon(5) == "inv_chest_plate_12"
    assert store.item_icon(9) == "inv_sword_04"
    assert store.item_icon(77) == ""


def test_store_missing_file_is_reported_not_raised(tmp_path):
    write_synthetic_dbc_dir(tmp_path / "dbc", skip=("Spell.dbc",))
    store = DbcStore(tmp_path)

    assert store.missing() == ["Spell.dbc"]
    assert store.ok() is False
    assert store.spell(100) is None
    assert store.talents()[23].ranks == (100, 101)
    assert store.glyph(2).spell_id == 56368
    assert store.item_icon(5) == "inv_chest_plate_12"


def test_store_corrupt_or_wrong_layout_files_count_as_missing(tmp_path):
    dbc_dir = write_synthetic_dbc_dir(tmp_path / "dbc")
    (dbc_dir / "GemProperties.dbc").write_bytes(b"WDBX" + b"\0" * 40)
    # a Talent.dbc from another client build (22 fields): its indices cannot be trusted
    write_dbc(dbc_dir / "Talent.dbc", 22, [record(22, {0: 23, 1: 61})])
    (dbc_dir / "ItemSet.dbc").write_bytes(b"")
    store = DbcStore(tmp_path)

    assert store.missing() == ["Talent.dbc", "GemProperties.dbc", "ItemSet.dbc"]
    assert store.talents() == {}
    assert store.gem(40111) is None
    assert store.item_set(843) is None
    assert store.talent_tabs()[61].name == "Frost"


def test_store_with_no_client_data_reports_every_file(tmp_path):
    store = DbcStore(tmp_path / "absent")

    assert store.missing() == list(DBC_FILES)
    assert store.ok() is False
    assert store.talents() == {}
    assert store.talent_tabs() == {}
    assert store.spell(1) is None
    assert store.spell_icon(1) == ""
    assert store.glyph(1) is None
    assert store.enchant(1) is None
    assert store.gem(1) is None
    assert store.random_property(1) is None
    assert store.random_suffix(1) is None
    assert store.item_set(1) is None
    assert store.item_icon(1) == ""


def test_store_loads_lazily_and_only_once_across_threads(client_data, monkeypatch):
    calls: list[str] = []
    real = dbc.read_dbc

    def counting(path):
        calls.append(Path(path).name)
        return real(path)

    monkeypatch.setattr(dbc, "read_dbc", counting)
    store = DbcStore(client_data)
    assert calls == []

    barrier = threading.Barrier(8)
    results: list[int] = []

    def worker():
        barrier.wait()
        results.append(len(store.talents()))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results == [1] * 8
    assert sorted(calls) == sorted(DBC_FILES)
    store.spell(100)
    store.missing()
    assert len(calls) == len(DBC_FILES)


def test_spell_index_handles_unsorted_ids(tmp_path):
    dbc_dir = write_synthetic_dbc_dir(tmp_path / "dbc")
    n = FIELDS["Spell.dbc"]
    write_dbc(
        dbc_dir / "Spell.dbc",
        n,
        [
            record(n, {0: 300, 136: "Third"}),
            record(n, {0: 100, 136: "First"}),
            record(n, {0: 200, 136: "Second"}),
        ],
    )
    store = DbcStore(tmp_path)

    assert [store.spell(i).name for i in (100, 200, 300)] == ["First", "Second", "Third"]
    assert store.spell(150) is None


def test_store_from_env_reads_client_data_dir(client_data, monkeypatch):
    monkeypatch.setenv("CLIENT_DATA_DIR", str(client_data))
    store = dbc.store_from_env()

    assert store.dbc_dir == client_data / "dbc"
    assert store.spell(101).name == "Frostbite"
    assert dbc.store_from_env() is store  # cached per process
    dbc.reset_store()
    assert dbc.store_from_env() is not store


def test_store_from_env_defaults_to_client_data_mount(monkeypatch):
    monkeypatch.delenv("CLIENT_DATA_DIR", raising=False)
    assert dbc.store_from_env().dbc_dir == Path("/client-data/dbc")


# --- the real client data (only where CLIENT_DATA_DIR is set; skipped in the sandbox) ---


@pytest.mark.skipif(not os.environ.get("CLIENT_DATA_DIR"), reason="CLIENT_DATA_DIR not set")
def test_real_client_data_smoke():
    store = dbc.store_from_env()

    assert store.missing() == []
    assert len(store.talents()) > 300
    assert len(store.talent_tabs()) >= 30
    fireball = store.spell(133)
    assert (fireball.name, fireball.rank) == ("Fireball", "Rank 1")
    assert store.spell_icon(fireball.icon_id) == "spell_fire_flamebolt"
    assert "Fallen Crusader" in store.enchant(3368).text
