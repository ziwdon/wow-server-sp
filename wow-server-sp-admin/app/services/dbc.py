"""WoW 3.3.5 client DBC files: a small WDBC reader and the Armory's reference-data store.

The Armory needs names and icons the AC database does not hold: talent trees, spell
names, glyphs, enchant/gem texts, random suffixes (and the points that size their stats),
item sets and item icons. They come from the read-only client-data volume
(``$CLIENT_DATA_DIR/dbc/``, default ``/client-data``).

WDBC layout: a 20-byte header (magic ``WDBC``, then little-endian uint32 record count,
field count, record size, string-block size), the fixed-size records (4-byte fields), then
the string block; a string field holds a byte offset into that block.

Field indices come from the pinned core (rev 3f2969dd) ``src/server/shared/DataStores/
DBCStructure.h`` (struct comments number every field, including the ones ``DBCfmt.h``
skips with ``x``) and ``DBCfmt.h`` (whose format-string length is the field count),
cross-checked against ``data/sql/base/db_world/*_dbc.sql``. A localised string is 16
``_lang`` slots plus a flags field; enUS is the first slot. A file whose field count is
not the 3.3.5 one is treated as missing: its indices cannot be trusted.

``DbcStore`` loads every table once, on first access (or from a warm-up thread), under a
lock. A missing or unreadable file is recorded in ``missing()``; accessors never raise
and answer ``None``/``""``/``{}`` instead. Memory stays small: ``Spell.dbc`` (~49 MB) and
``ItemDisplayInfo.dbc`` stay memory-mapped behind a compact id index built in one pass
over the id column, and rows are decoded on demand and memoised; the other tables are
small and decoded once.
"""
from __future__ import annotations

import logging
import mmap
import os
import struct
import threading
from array import array
from bisect import bisect_left
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Generic, NamedTuple, TypeVar

log = logging.getLogger(__name__)

MAGIC = b"WDBC"
_HEADER = struct.Struct("<4s4I")
HEADER_SIZE = _HEADER.size  # 20
_U32 = struct.Struct("<I")
_F32 = struct.Struct("<f")

DEFAULT_CLIENT_DATA_DIR = "/client-data"


class DbcError(ValueError):
    """A file that is not a well-formed WDBC: bad magic, truncated, or an inconsistent header."""


class DbcFile:
    """One WDBC file, memory-mapped read-only. Every field reads as uint32; callers reinterpret."""

    def __init__(self, path: Path, mm: mmap.mmap, records: int, fields: int,
                 record_size: int, string_block_size: int) -> None:
        self.path = path
        self.records = records
        self.fields = fields
        self.record_size = record_size
        self.string_block_size = string_block_size
        self._mm = mm
        self._strings_start = HEADER_SIZE + records * record_size
        self._strings_end = self._strings_start + string_block_size
        self._row = struct.Struct(f"<{fields}I")

    def _offset(self, i: int, field: int) -> int:
        if not 0 <= i < self.records:
            raise IndexError(f"{self.path.name}: record {i} out of range")
        if not 0 <= field < self.fields:
            raise IndexError(f"{self.path.name}: field {field} out of range")
        return HEADER_SIZE + i * self.record_size + field * 4

    def string(self, offset: int) -> str:
        """The NUL-terminated string at ``offset`` in the string block (``""`` if out of range)."""
        if not 0 <= offset < self.string_block_size:
            return ""
        start = self._strings_start + offset
        end = self._mm.find(b"\0", start, self._strings_end)
        if end < 0:
            end = self._strings_end
        return self._mm[start:end].decode("utf-8", errors="replace")

    def row(self, i: int) -> tuple[int, ...]:
        """Every field of record ``i`` as uint32."""
        return self._row.unpack_from(self._mm, self._offset(i, 0))

    def int(self, i: int, field: int) -> int:
        """Field ``field`` of record ``i`` as uint32."""
        return _U32.unpack_from(self._mm, self._offset(i, field))[0]

    def float(self, i: int, field: int) -> float:
        """Field ``field`` of record ``i`` as float32."""
        return _F32.unpack_from(self._mm, self._offset(i, field))[0]

    def str(self, i: int, field: int) -> str:
        """The string field ``field`` of record ``i``."""
        return self.string(self.int(i, field))

    def ints(self, i: int, first: int, count: int) -> tuple[int, ...]:
        """``count`` consecutive uint32 fields of record ``i`` starting at ``first``."""
        self._offset(i, first + count - 1)
        return struct.unpack_from(f"<{count}I", self._mm, self._offset(i, first))

    def drop_resident_pages(self) -> None:
        """Let the kernel drop this mapping's resident pages (they re-fault from the page cache)."""
        advice = getattr(mmap, "MADV_DONTNEED", None)
        if advice is not None:
            try:
                self._mm.madvise(advice)
            except (OSError, ValueError):
                pass

    def close(self) -> None:
        self._mm.close()


def read_dbc(path: Path) -> DbcFile:
    """Open and validate a WDBC file. Raises ``DbcError`` for a malformed file, ``OSError`` if unreadable."""
    path = Path(path)
    with open(path, "rb") as fh:
        size = os.fstat(fh.fileno()).st_size
        if size < HEADER_SIZE:
            raise DbcError(f"{path.name}: truncated header ({size} bytes)")
        mm = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)
    try:
        magic, records, fields, record_size, string_block_size = _HEADER.unpack_from(mm, 0)
        if magic != MAGIC:
            raise DbcError(f"{path.name}: bad magic {magic!r}")
        if fields == 0 or record_size != fields * 4:
            raise DbcError(
                f"{path.name}: inconsistent header (fields={fields}, record_size={record_size})"
            )
        expected = HEADER_SIZE + records * record_size + string_block_size
        if size < expected:
            raise DbcError(f"{path.name}: truncated ({size} of {expected} bytes)")
    except BaseException:
        mm.close()
        raise
    return DbcFile(path, mm, records, fields, record_size, string_block_size)


# --- field layouts (index = field number in the record) ---

# Talent.dbc — TalentEntry, DBCfmt TalentEntryfmt (23 fields)
TALENT_FIELDS = 23
TALENT_ID = 0             # TalentID
TALENT_TAB = 1            # TalentTab (TalentTab.dbc id)
TALENT_ROW = 2            # Row (tier)
TALENT_COL = 3            # Col
TALENT_RANK_FIRST = 4     # RankID[5] 4-8, then spellRank[4] 9-12 ("not used, always 0")
TALENT_RANK_COUNT = 9

# TalentTab.dbc — TalentTabEntry, TalentTabEntryfmt (24 fields)
TALENTTAB_FIELDS = 24
TALENTTAB_ID = 0          # TalentTabID
TALENTTAB_NAME = 1        # name[16] 1-16 (unused by the core); enUS slot
TALENTTAB_ICON = 18       # spellicon (SpellIcon.dbc id)
TALENTTAB_CLASS_MASK = 20  # ClassMask
TALENTTAB_ORDER = 22      # tabpage

# Spell.dbc — SpellEntry, SpellEntryfmt (234 fields)
SPELL_FIELDS = 234
SPELL_ID = 0              # Id
SPELL_ICON = 133          # SpellIconID
SPELL_NAME = 136          # SpellName[16] 136-151; enUS slot
SPELL_RANK = 153          # Rank[16] 153-168 (m_nameSubtext_lang); enUS slot
SPELL_DESCRIPTION = 170   # Description[16] 170-185 (not loaded by the core); enUS slot

# SpellIcon.dbc — not loaded by the core (no struct/fmt in the pinned source); the 3.3.5
# client layout is ID, TextureFilename (2 fields)
SPELLICON_FIELDS = 2
SPELLICON_ID = 0          # ID
SPELLICON_TEXTURE = 1     # TextureFilename

# GlyphProperties.dbc — GlyphPropertiesEntry, GlyphPropertiesfmt (4 fields)
GLYPH_FIELDS = 4
GLYPH_ID = 0              # Id
GLYPH_SPELL = 1           # SpellId
GLYPH_TYPE_FLAGS = 2      # TypeFlags (must equal GlyphSlot.TypeFlags); bit 0 set = minor glyph
GLYPH_FLAG_MINOR = 0x1

# SpellItemEnchantment.dbc — SpellItemEnchantmentEntry, SpellItemEnchantmentfmt (38 fields)
ENCHANT_FIELDS = 38
ENCHANT_ID = 0            # ID
ENCHANT_DESCRIPTION = 14  # description[16] 14-29 (m_name_lang); enUS slot
ENCHANT_AURA_ID = 31      # aura_id (m_itemVisual)
ENCHANT_GEM_ID = 33       # GemID (m_src_itemID): the gem item that applies this enchant

# GemProperties.dbc — GemPropertiesEntry, GemPropertiesEntryfmt "nixxi" (5 fields)
GEM_FIELDS = 5
# field 0 ID (= item_template.GemProperties) is not kept: gem() is keyed by the gem item
GEM_ENCHANT = 1           # spellitemenchantement
GEM_COLOR = 4             # color (socket colour mask)

# ItemRandomProperties.dbc — ItemRandomPropertiesEntry, ItemRandomPropertiesfmt (24 fields)
RANDPROP_FIELDS = 24
RANDPROP_ID = 0           # ID
RANDPROP_ENCHANT_FIRST = 2  # Enchantment[5] 2-6
RANDPROP_ENCHANT_COUNT = 5
RANDPROP_NAME = 7         # Name[16] 7-22; enUS slot

# ItemRandomSuffix.dbc — ItemRandomSuffixEntry, ItemRandomSuffixfmt (29 fields)
RANDSUFFIX_FIELDS = 29
RANDSUFFIX_ID = 0         # ID
RANDSUFFIX_NAME = 1       # Name[16] 1-16; enUS slot
RANDSUFFIX_ENCHANT_FIRST = 19  # Enchantment[5] 19-23
RANDSUFFIX_ALLOC_FIRST = 24    # AllocationPct[5] 24-28
RANDSUFFIX_ENCHANT_COUNT = 5

# ItemSet.dbc — ItemSetEntry, ItemSetEntryfmt (53 fields)
ITEMSET_FIELDS = 53
ITEMSET_ID = 0            # id (fmt 'd': index-only, not stored by the core)
ITEMSET_NAME = 1          # name[16] 1-16; enUS slot
ITEMSET_ITEM_FIRST = 18   # itemId[10] 18-27, then itemID[7] 28-34 (unused by the core)
ITEMSET_ITEM_COUNT = 17
ITEMSET_SPELL_FIRST = 35  # spells[8] 35-42 (m_setSpellID)
ITEMSET_THRESHOLD_FIRST = 43  # items_to_triggerspell[8] 43-50 (m_setThreshold)
ITEMSET_SPELL_COUNT = 8

# ItemDisplayInfo.dbc — ItemDisplayInfoEntry, ItemDisplayTemplateEntryfmt (25 fields). The
# struct comment numbers the inventoryIcon *column* 3 (array columns counted once); the fmt
# string's 's' at field 5 and the mirror table's InventoryIcon_1 give the field index.
ITEMDISPLAY_FIELDS = 25
ITEMDISPLAY_ID = 0        # ID
ITEMDISPLAY_ICON = 5      # inventoryIcon (m_inventoryIcon[0], InventoryIcon_1)

# RandPropPoints.dbc — RandomPropertiesPointsEntry, RandomPropertiesPointsfmt
# "niiiiiiiiiiiiiii" (16 fields). The struct comments number the fields from 1 after a
# commented-out "hidden key" Id, but the fmt's 'n' (field 0) is stored as the struct's
# first member, itemLevel: the fmt length and the mirror table randproppoints_dbc (ID,
# Epic_1-5, Superior_1-5, Good_1-5) give the indices below.
RANDPOINTS_FIELDS = 16
RANDPOINTS_ITEM_LEVEL = 0      # itemLevel (fmt 'n': the id LookupEntry(ItemLevel) finds)
RANDPOINTS_EPIC_FIRST = 1      # EpicPropertiesPoints[5] (Epic_1-5)
RANDPOINTS_RARE_FIRST = 6      # RarePropertiesPoints[5] (Superior_1-5)
RANDPOINTS_UNCOMMON_FIRST = 11  # UncommonPropertiesPoints[5] (Good_1-5)
RANDPOINTS_COUNT = 5

DBC_FILES: tuple[str, ...] = (
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
    "RandPropPoints.dbc",
)

EXPECTED_FIELDS: dict[str, int] = {
    "Talent.dbc": TALENT_FIELDS,
    "TalentTab.dbc": TALENTTAB_FIELDS,
    "Spell.dbc": SPELL_FIELDS,
    "SpellIcon.dbc": SPELLICON_FIELDS,
    "GlyphProperties.dbc": GLYPH_FIELDS,
    "SpellItemEnchantment.dbc": ENCHANT_FIELDS,
    "GemProperties.dbc": GEM_FIELDS,
    "ItemRandomProperties.dbc": RANDPROP_FIELDS,
    "ItemRandomSuffix.dbc": RANDSUFFIX_FIELDS,
    "ItemSet.dbc": ITEMSET_FIELDS,
    "ItemDisplayInfo.dbc": ITEMDISPLAY_FIELDS,
    "RandPropPoints.dbc": RANDPOINTS_FIELDS,
}


# --- decoded records ---


@dataclass(frozen=True, slots=True)
class Talent:
    id: int
    tab: int
    tier: int
    column: int
    ranks: tuple[int, ...]  # rank spell ids, rank 1 first, zeros dropped


@dataclass(frozen=True, slots=True)
class TalentTab:
    id: int
    name: str
    icon_id: int
    class_mask: int
    order: int


@dataclass(frozen=True, slots=True)
class Spell:
    id: int
    name: str
    rank: str
    icon_id: int
    description: str


@dataclass(frozen=True, slots=True)
class Glyph:
    id: int
    spell_id: int
    type_major: bool


@dataclass(frozen=True, slots=True)
class Enchant:
    id: int
    text: str
    gem_item_id: int
    visual_id: int


@dataclass(frozen=True, slots=True)
class Gem:
    item_id: int
    enchant_id: int
    color_mask: int


@dataclass(frozen=True, slots=True)
class RandomEnchant:
    id: int
    suffix: str
    enchant_ids: tuple[int, ...]
    alloc: tuple[int, ...]  # AllocationPct per enchant (suffixes only; empty for random properties)


@dataclass(frozen=True, slots=True)
class ItemSet:
    id: int
    name: str
    items: tuple[int, ...]
    bonuses: tuple[tuple[int, int], ...]  # (spell_id, threshold)


class RandPropPoints(NamedTuple):
    """One item level's random-property points, each indexed by the core's suffix-factor
    column (``GenerateEnchSuffixFactor``: 0 head/chest/legs/2H, ..., 4 ranged)."""

    epic: tuple[int, ...]
    rare: tuple[int, ...]
    uncommon: tuple[int, ...]


def icon_basename(path: str) -> str:
    """``Interface\\Icons\\Spell_Nature_X`` -> ``spell_nature_x`` (any extension dropped)."""
    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    if name.lower().endswith(".blp"):
        name = name[:-4]
    return name.lower()


T = TypeVar("T")


class _IdIndex:
    """id -> record number, built in one pass over a file's id column.

    Real DBCs are sorted by id, so the ids sit in a compact ``array`` searched by bisection;
    an unsorted file falls back to a dict. A repeated id resolves to its last record, as in
    the core's DBC storage.
    """

    def __init__(self, f: DbcFile, id_field: int) -> None:
        ids = array("I", (f.int(i, id_field) for i in range(f.records)))
        self._sorted = all(ids[i] < ids[i + 1] for i in range(len(ids) - 1))
        self._ids = ids
        self._map: dict[int, int] | None = None
        if not self._sorted:
            self._map = {id_: i for i, id_ in enumerate(ids)}
            self._ids = array("I")

    def find(self, id_: int) -> int | None:
        if self._map is not None:
            return self._map.get(id_)
        i = bisect_left(self._ids, id_)
        if i < len(self._ids) and self._ids[i] == id_:
            return i
        return None


class _LazyTable(Generic[T]):
    """A large table kept memory-mapped: rows are decoded on first lookup and memoised."""

    MEMO_MAX = 16384

    def __init__(self, f: DbcFile, id_field: int, decode: Callable[[DbcFile, int], T]) -> None:
        self._file = f
        self._index = _IdIndex(f, id_field)
        self._decode = decode
        self._memo: dict[int, T] = {}
        f.drop_resident_pages()

    def get(self, id_: int) -> T | None:
        hit = self._memo.get(id_)
        if hit is not None:
            return hit
        row = self._index.find(id_)
        if row is None:
            return None
        try:
            value = self._decode(self._file, row)
        except Exception:  # noqa: BLE001 - a bad row must not break the page
            log.warning("dbc: cannot decode %s record %d", self._file.path.name, row)
            return None
        finally:
            # A fault maps up to 64 KB of already-cached neighbours (fault-around), so random
            # lookups would make the whole ~49 MB Spell.dbc resident; unmap after each decode.
            self._file.drop_resident_pages()
        if len(self._memo) >= self.MEMO_MAX:
            self._memo.clear()
        self._memo[id_] = value
        return value

    def close(self) -> None:
        self._file.close()


def _nonzero(values: tuple[int, ...]) -> tuple[int, ...]:
    return tuple(v for v in values if v)


def _decode_spell(f: DbcFile, i: int) -> Spell:
    return Spell(
        id=f.int(i, SPELL_ID),
        name=f.str(i, SPELL_NAME),
        rank=f.str(i, SPELL_RANK),
        icon_id=f.int(i, SPELL_ICON),
        description=f.str(i, SPELL_DESCRIPTION),
    )


def _decode_item_icon(f: DbcFile, i: int) -> str:
    return icon_basename(f.str(i, ITEMDISPLAY_ICON))


class DbcStore:
    """The Armory's DBC reference data, read from ``client_data_dir/dbc/`` on first use."""

    def __init__(self, client_data_dir: Path) -> None:
        self.dbc_dir = Path(client_data_dir) / "dbc"
        self._lock = threading.Lock()
        self._loaded = False
        self._missing: list[str] = []
        self._talents: dict[int, Talent] = {}
        self._talent_tabs: dict[int, TalentTab] = {}
        self._spell_icons: dict[int, str] = {}
        self._glyphs: dict[int, Glyph] = {}
        self._enchants: dict[int, Enchant] = {}
        self._gems: dict[int, Gem] = {}
        self._random_properties: dict[int, RandomEnchant] = {}
        self._random_suffixes: dict[int, RandomEnchant] = {}
        self._item_sets: dict[int, ItemSet] = {}
        self._rand_prop_points: dict[int, RandPropPoints] = {}
        self._spells: _LazyTable[Spell] | None = None
        self._item_icons: _LazyTable[str] | None = None

    # --- loading ---

    def load(self) -> None:
        """Load every table once; later calls return at once. Safe to call from any thread."""
        if self._loaded:
            return
        with self._lock:
            if self._loaded:
                return
            gem_rows: list[tuple[int, int]] = []
            loaders: dict[str, Callable[[DbcFile], bool]] = {
                "Talent.dbc": self._load_talents,
                "TalentTab.dbc": self._load_talent_tabs,
                "Spell.dbc": self._load_spells,
                "SpellIcon.dbc": self._load_spell_icons,
                "GlyphProperties.dbc": self._load_glyphs,
                "SpellItemEnchantment.dbc": self._load_enchants,
                "GemProperties.dbc": lambda f: self._load_gem_rows(f, gem_rows),
                "ItemRandomProperties.dbc": self._load_random_properties,
                "ItemRandomSuffix.dbc": self._load_random_suffixes,
                "ItemSet.dbc": self._load_item_sets,
                "ItemDisplayInfo.dbc": self._load_item_icons,
                "RandPropPoints.dbc": self._load_rand_prop_points,
            }
            for name in DBC_FILES:
                self._load_one(name, loaders[name])
            # gem() is keyed by the gem item: GemProperties.spellitemenchantement is the socket
            # enchant, whose GemID (m_src_itemID) is the item (the core's own link, Item.cpp).
            for enchant_id, color in gem_rows:
                enchant = self._enchants.get(enchant_id)
                if enchant and enchant.gem_item_id and enchant.gem_item_id not in self._gems:
                    self._gems[enchant.gem_item_id] = Gem(enchant.gem_item_id, enchant_id, color)
            self._loaded = True

    def _load_one(self, name: str, loader: Callable[[DbcFile], bool]) -> None:
        path = self.dbc_dir / name
        f: DbcFile | None = None
        try:
            f = read_dbc(path)
            if f.fields != EXPECTED_FIELDS[name]:
                raise DbcError(
                    f"{name}: {f.fields} fields, expected {EXPECTED_FIELDS[name]} (not a 3.3.5 file)"
                )
            keep_open = loader(f)
        except Exception as exc:  # noqa: BLE001 - one bad file only marks that table missing
            log.warning("dbc: %s unavailable: %s", name, exc)
            self._missing.append(name)
            if f is not None:
                f.close()
            return
        if not keep_open:
            f.close()

    # Each loader builds its table locally and assigns it once, so a file that fails part-way
    # leaves its table empty rather than half filled. True = keep the file mapped.

    def _load_talents(self, f: DbcFile) -> bool:
        talents: dict[int, Talent] = {}
        for i in range(f.records):
            talent = Talent(
                id=f.int(i, TALENT_ID),
                tab=f.int(i, TALENT_TAB),
                tier=f.int(i, TALENT_ROW),
                column=f.int(i, TALENT_COL),
                ranks=_nonzero(f.ints(i, TALENT_RANK_FIRST, TALENT_RANK_COUNT)),
            )
            talents[talent.id] = talent
        self._talents = talents
        return False

    def _load_talent_tabs(self, f: DbcFile) -> bool:
        tabs: dict[int, TalentTab] = {}
        for i in range(f.records):
            tab = TalentTab(
                id=f.int(i, TALENTTAB_ID),
                name=f.str(i, TALENTTAB_NAME),
                icon_id=f.int(i, TALENTTAB_ICON),
                class_mask=f.int(i, TALENTTAB_CLASS_MASK),
                order=f.int(i, TALENTTAB_ORDER),
            )
            tabs[tab.id] = tab
        self._talent_tabs = tabs
        return False

    def _load_spells(self, f: DbcFile) -> bool:
        self._spells = _LazyTable(f, SPELL_ID, _decode_spell)
        return True

    def _load_spell_icons(self, f: DbcFile) -> bool:
        icons: dict[int, str] = {}
        for i in range(f.records):
            icons[f.int(i, SPELLICON_ID)] = icon_basename(f.str(i, SPELLICON_TEXTURE))
        self._spell_icons = icons
        return False

    def _load_glyphs(self, f: DbcFile) -> bool:
        glyphs: dict[int, Glyph] = {}
        for i in range(f.records):
            glyph = Glyph(
                id=f.int(i, GLYPH_ID),
                spell_id=f.int(i, GLYPH_SPELL),
                type_major=not f.int(i, GLYPH_TYPE_FLAGS) & GLYPH_FLAG_MINOR,
            )
            glyphs[glyph.id] = glyph
        self._glyphs = glyphs
        return False

    def _load_enchants(self, f: DbcFile) -> bool:
        enchants: dict[int, Enchant] = {}
        for i in range(f.records):
            enchant = Enchant(
                id=f.int(i, ENCHANT_ID),
                text=f.str(i, ENCHANT_DESCRIPTION),
                gem_item_id=f.int(i, ENCHANT_GEM_ID),
                visual_id=f.int(i, ENCHANT_AURA_ID),
            )
            enchants[enchant.id] = enchant
        self._enchants = enchants
        return False

    def _load_gem_rows(self, f: DbcFile, out: list[tuple[int, int]]) -> bool:
        out.extend([(f.int(i, GEM_ENCHANT), f.int(i, GEM_COLOR)) for i in range(f.records)])
        return False

    def _load_random_properties(self, f: DbcFile) -> bool:
        entries: dict[int, RandomEnchant] = {}
        for i in range(f.records):
            entry = RandomEnchant(
                id=f.int(i, RANDPROP_ID),
                suffix=f.str(i, RANDPROP_NAME),
                enchant_ids=_nonzero(f.ints(i, RANDPROP_ENCHANT_FIRST, RANDPROP_ENCHANT_COUNT)),
                alloc=(),
            )
            entries[entry.id] = entry
        self._random_properties = entries
        return False

    def _load_random_suffixes(self, f: DbcFile) -> bool:
        entries: dict[int, RandomEnchant] = {}
        for i in range(f.records):
            enchants = f.ints(i, RANDSUFFIX_ENCHANT_FIRST, RANDSUFFIX_ENCHANT_COUNT)
            allocs = f.ints(i, RANDSUFFIX_ALLOC_FIRST, RANDSUFFIX_ENCHANT_COUNT)
            pairs = [(e, a) for e, a in zip(enchants, allocs) if e]
            entry = RandomEnchant(
                id=f.int(i, RANDSUFFIX_ID),
                suffix=f.str(i, RANDSUFFIX_NAME),
                enchant_ids=tuple(e for e, _ in pairs),
                alloc=tuple(a for _, a in pairs),
            )
            entries[entry.id] = entry
        self._random_suffixes = entries
        return False

    def _load_item_sets(self, f: DbcFile) -> bool:
        entries: dict[int, ItemSet] = {}
        for i in range(f.records):
            spells = f.ints(i, ITEMSET_SPELL_FIRST, ITEMSET_SPELL_COUNT)
            thresholds = f.ints(i, ITEMSET_THRESHOLD_FIRST, ITEMSET_SPELL_COUNT)
            entry = ItemSet(
                id=f.int(i, ITEMSET_ID),
                name=f.str(i, ITEMSET_NAME),
                items=_nonzero(f.ints(i, ITEMSET_ITEM_FIRST, ITEMSET_ITEM_COUNT)),
                bonuses=tuple((s, t) for s, t in zip(spells, thresholds) if s),
            )
            entries[entry.id] = entry
        self._item_sets = entries
        return False

    def _load_item_icons(self, f: DbcFile) -> bool:
        self._item_icons = _LazyTable(f, ITEMDISPLAY_ID, _decode_item_icon)
        return True

    def _load_rand_prop_points(self, f: DbcFile) -> bool:
        entries: dict[int, RandPropPoints] = {}
        for i in range(f.records):
            entries[f.int(i, RANDPOINTS_ITEM_LEVEL)] = RandPropPoints(
                epic=f.ints(i, RANDPOINTS_EPIC_FIRST, RANDPOINTS_COUNT),
                rare=f.ints(i, RANDPOINTS_RARE_FIRST, RANDPOINTS_COUNT),
                uncommon=f.ints(i, RANDPOINTS_UNCOMMON_FIRST, RANDPOINTS_COUNT),
            )
        self._rand_prop_points = entries
        return False

    def close(self) -> None:
        """Unmap the files kept open (tests and process teardown)."""
        with self._lock:
            for table in (self._spells, self._item_icons):
                if table is not None:
                    table.close()
            self._spells = self._item_icons = None

    # --- accessors (load on first use; never raise) ---

    def missing(self) -> list[str]:
        """DBC file names that failed to load, in ``DBC_FILES`` order."""
        self.load()
        return list(self._missing)

    def ok(self) -> bool:
        self.load()
        return not self._missing

    def talents(self) -> dict[int, Talent]:
        self.load()
        return dict(self._talents)

    def talent_tabs(self) -> dict[int, TalentTab]:
        self.load()
        return dict(self._talent_tabs)

    def spell(self, id_: int) -> Spell | None:
        self.load()
        table = self._spells
        return table.get(id_) if table is not None else None

    def spell_icon(self, id_: int) -> str:
        self.load()
        return self._spell_icons.get(id_, "")

    def glyph(self, id_: int) -> Glyph | None:
        self.load()
        return self._glyphs.get(id_)

    def enchant(self, id_: int) -> Enchant | None:
        self.load()
        return self._enchants.get(id_)

    def gem(self, item_id: int) -> Gem | None:
        self.load()
        return self._gems.get(item_id)

    def random_property(self, id_: int) -> RandomEnchant | None:
        self.load()
        return self._random_properties.get(id_)

    def random_suffix(self, id_: int) -> RandomEnchant | None:
        self.load()
        return self._random_suffixes.get(id_)

    def item_set(self, id_: int) -> ItemSet | None:
        self.load()
        return self._item_sets.get(id_)

    def item_icon(self, display_id: int) -> str:
        self.load()
        table = self._item_icons
        return (table.get(display_id) or "") if table is not None else ""

    def rand_prop_points(self, item_level: int) -> RandPropPoints | None:
        """The ``RandPropPoints.dbc`` row whose id is ``item_level`` (the core's
        ``sRandomPropertiesPointsStore.LookupEntry(ItemLevel)``)."""
        self.load()
        return self._rand_prop_points.get(item_level)


# --- the process-wide instance ---

_store: DbcStore | None = None
_store_lock = threading.Lock()


def store_from_env() -> DbcStore:
    """The process's ``DbcStore`` over ``$CLIENT_DATA_DIR/dbc`` (default ``/client-data/dbc``)."""
    global _store
    with _store_lock:
        if _store is None:
            _store = DbcStore(Path(os.environ.get("CLIENT_DATA_DIR") or DEFAULT_CLIENT_DATA_DIR))
        return _store


def reset_store() -> None:
    """Forget the cached instance (tests)."""
    global _store
    with _store_lock:
        old, _store = _store, None
    if old is not None:
        old.close()
