"""Armory display IDs: which model id Wowhead's viewer knows an equipped item by (spec 2, 8.1).

Wowhead regenerated its Wrath model data, and some 3.3.5 ``item_template.displayid``
values no longer exist there (8 of 37 pieces in the spike); its item lookup by entry
(``https://www.wowhead.com/wotlk/item=<entry>&xml``, ``<icon displayId="…">``) names the
id that does. ``DisplayIdResolver.resolve`` runs the spec 8.1 state machine:

1. ``displayid-map.json`` knows the entry: use it. A found id is permanent; ``null``
   (known unresolvable) is asked again after ``NULL_RETRY_SECONDS``.
2. Probe the meta file the viewer would ask for under the DB id, through the proxy's
   ``serve`` (so it is cached for the viewer's own request a moment later). 200: record it.
3. 404: one item-XML GET (``AssetProxy.fetch_item_xml``: the On/Off switch, the
   cool-down and the shared slots apply), parse the id with a regex (no XML parser on
   untrusted input), verify its meta the same way, record it.

``null`` is recorded only for a definitive answer: the DB id's meta is 404 and the
XML is 404, or names no usable id, or names the DB id again, or names an id whose
meta is 404 as well. Anything transient (switched off, cooling down, no free slot,
429/5xx, network errors, a follower that gave up waiting) records nothing, so a later
view asks again. Two concurrent resolves of one entry share one lookup.

``resolve_outcome`` tells the two kinds of miss apart (R23): ``Miss.UNRESOLVABLE`` is
definitive (a recorded ``null``, or no usable entry), ``Miss.PENDING`` transient.
``resolve_items`` sorts the items it cannot send into those two lists, so the model
panel can say "Wowhead unreachable" instead of blaming the item; ``resolve`` answers
``None`` for both.

The map lives next to the cache, in the proxy's armory dir (``ADMIN_DATA_DIR/armory``),
and survives a cache Clear. It is written with the armory's one private-write helper
(0600, tmp + rename); a corrupt or unreadable one starts empty and an unwritable one
keeps working in memory, each logged once. ``get_resolver()`` is the one long-lived
instance, bound to ``get_proxy()``'s.
"""

from __future__ import annotations

import dataclasses
import enum
import json
import logging
import math
import re
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from app.services import armory_assets
from app.services.armory_assets import (
    DEFAULT_TIMEOUT_SECONDS,
    MAX_PARALLEL_FETCHES,
    SLOT_WAIT_MARGIN_SECONDS,
    AssetProxy,
    _json_bytes,
    _make_private_dir,
    _write_private_file,
)

if TYPE_CHECKING:
    from app.services.armory import ViewerItem

log = logging.getLogger(__name__)

MAP_FILE = "displayid-map.json"
NULL_RETRY_SECONDS = 7 * 86_400
# The InventoryTypes whose model metadata lives under meta/armor/<type>/ (the allow-list's set);
# weapons, shields, held items and ranged weapons use meta/item/.
ARMOR_META_TYPES = frozenset({1, 3, 4, 5, 6, 7, 8, 9, 10, 16, 19, 20})
# A follower waits for at most three proxy round trips of its leader (DB meta, XML,
# Wowhead id's meta), each a slot wait plus one fetch (about two fetch timeouts at worst).
FOLLOW_TIMEOUT_SECONDS = 3 * (DEFAULT_TIMEOUT_SECONDS + 2 * DEFAULT_TIMEOUT_SECONDS + 2 * SLOT_WAIT_MARGIN_SECONDS)

_MAX_ENTRY = 0xFFFFFFFF  # item_template.entry is an unsigned 32-bit id
_MAX_DISPLAY_ID = 99_999_999  # the allow-list's [0-9]{1,8}
_MAX_DISPLAY_ID_DIGITS = 8
_MAX_EPOCH = 10**11  # a checked_at beyond this (year 5138) is not a time this code wrote
_ENTRY_KEY = re.compile(r"[1-9][0-9]{0,9}")
# The brief's pattern; ``\d`` on bytes is ASCII 0-9 only.
_ICON_DISPLAY_ID = re.compile(rb'<icon[^>]*displayId="(\d+)"')


def meta_path(inventory_type: int, display_id: int) -> str:
    """The proxy path of the meta file the viewer asks for an item of this type and display id."""
    if inventory_type in ARMOR_META_TYPES:
        return f"wrath/meta/armor/{inventory_type}/{display_id}.json"
    return f"wrath/meta/item/{display_id}.json"


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _valid_entry(entry: object) -> bool:
    return _is_int(entry) and 0 < entry <= _MAX_ENTRY


def _parse_display_id(body: bytes) -> int | None:
    """The display id of the first ``<icon … displayId="N"`` in ``body``, or None.

    Exactly what ``_ICON_DISPLAY_ID.search(body)`` finds, without its quadratic
    backtracking on a hostile body (many ``<icon`` and no ``>``): a match cannot cross
    a ``>``, and within one ``>``-free run only its first ``<icon`` can be the leftmost
    match, so the pattern is tried once per run. An id of more than 8 digits, or 0, is
    no id.
    """
    if not isinstance(body, (bytes, bytearray)):
        return None
    for run in bytes(body).split(b">"):
        start = run.find(b"<icon")
        if start < 0:
            continue
        match = _ICON_DISPLAY_ID.match(run, start)
        if match is None:
            continue
        digits = match.group(1)
        if len(digits) > _MAX_DISPLAY_ID_DIGITS:
            return None
        value = int(digits)
        return value if value > 0 else None
    return None


class Miss(enum.Enum):
    """Why an item has no display id to send the viewer (R23)."""

    UNRESOLVABLE = "unresolvable"  # definitive: a recorded null (no model on Wowhead), or no usable entry
    PENDING = "pending"  # transient: nothing recorded (switched off, cool-down, no slot, upstream failure)


# A resolve's answer: the display id to send, or why there is none.
Outcome = int | Miss


@dataclass(frozen=True)
class _Record:
    display_id: int | None  # None: known unresolvable
    checked_at: int  # epoch seconds


@dataclass
class _Flight:
    """One lookup in progress for an entry; other resolves of it wait on ``done``."""

    done: threading.Event = field(default_factory=threading.Event)
    outcome: Outcome = Miss.PENDING  # kept if the leader raises: nothing was recorded
    followers: int = 0


def _parse_record(key: object, value: object) -> tuple[int, _Record] | None:
    if not isinstance(key, str) or not _ENTRY_KEY.fullmatch(key) or not isinstance(value, dict):
        return None
    entry = int(key)
    display_id = value.get("display_id")
    checked_at = value.get("checked_at")
    if not _valid_entry(entry):
        return None
    if display_id is not None and not (_is_int(display_id) and 0 <= display_id <= _MAX_DISPLAY_ID):
        return None
    # A finite epoch in a sane range (json.loads accepts NaN, Infinity and ints of any size).
    if not (_is_int(checked_at) or (isinstance(checked_at, float) and math.isfinite(checked_at))):
        return None
    if not 0 <= checked_at <= _MAX_EPOCH:
        return None
    return entry, _Record(display_id, int(checked_at))


class DisplayIdResolver:
    """Entry -> the display id Wowhead's viewer has a model for (spec 8.1); see the module docstring.

    Every method blocks (proxy calls, the map file): call them from a worker thread.
    ``_mu`` guards the in-memory map and the flights; ``_write_lock`` serialises map
    writes (taken first, ``_mu`` only briefly inside it). Neither is held while a
    proxy call runs.
    """

    def __init__(
        self,
        proxy: AssetProxy,
        data_dir: Path | None = None,
        *,
        now: Callable[[], float] = time.time,
        follow_timeout_s: float = FOLLOW_TIMEOUT_SECONDS,
    ) -> None:
        self.proxy = proxy
        self.dir = Path(data_dir) if data_dir is not None else proxy.data_dir
        self.path = self.dir / MAP_FILE
        self._now = now
        self._follow_timeout = float(follow_timeout_s)
        self._mu = threading.Lock()
        self._write_lock = threading.Lock()
        self._map: dict[int, _Record] | None = None  # None: not read yet (building touches nothing)
        self._dirty = False
        self._write_failing = False
        self._flights: dict[int, _Flight] = {}

    # public

    def resolve(self, entry: int, db_display_id: int, inventory_type: int) -> int | None:
        """The display id to send the viewer for this item, or None (no 3D for it, for now or for good)."""
        outcome = self.resolve_outcome(entry, db_display_id, inventory_type)
        return None if isinstance(outcome, Miss) else outcome

    def resolve_outcome(self, entry: int, db_display_id: int, inventory_type: int) -> Outcome:
        """The display id, or ``Miss.UNRESOLVABLE`` (definitive) / ``Miss.PENDING`` (ask again later)."""
        try:
            return self._resolve(entry, db_display_id, inventory_type)
        finally:
            self._flush()

    def resolve_items(
        self, items: Sequence[ViewerItem], entries: Mapping[int, int], *, workers: int = 1
    ) -> tuple[list[ViewerItem], list[int], list[int]]:
        """(``viewer_items``' list with resolved display ids, unresolved slots, pending slots).

        ``entries`` maps DB slot -> ``item_template.entry``; an item whose slot has no
        entry there is unresolved. Unresolved = a definitive miss (no model for it on
        Wowhead); pending = a transient one (nothing recorded; a later view asks again).
        Up to ``workers`` items resolve at once (at most ``MAX_PARALLEL_FETCHES``, the
        proxy's upstream slots). The first list keeps the input order, the slot lists
        are sorted. The map is written once, after all of them.
        """
        workers = max(1, min(int(workers), MAX_PARALLEL_FETCHES, len(items) or 1))

        def one(item: ViewerItem) -> Outcome:
            entry = entries.get(item.slot)
            if entry is None:
                return Miss.UNRESOLVABLE
            return self._resolve(entry, item.display_id, item.inventory_type)

        try:
            if workers == 1:
                outcomes = [one(item) for item in items]
            else:
                with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="armory-resolve") as pool:
                    outcomes = list(pool.map(one, items))
        finally:
            self._flush()
        sent: list[ViewerItem] = []
        unresolved: list[int] = []
        pending: list[int] = []
        for item, outcome in zip(items, outcomes):
            if outcome is Miss.UNRESOLVABLE:
                unresolved.append(item.slot)
            elif outcome is Miss.PENDING:
                pending.append(item.slot)
            elif outcome == item.display_id:
                sent.append(item)
            else:
                sent.append(dataclasses.replace(item, display_id=outcome))
        return sent, sorted(unresolved), sorted(pending)

    # the state machine

    def _resolve(self, entry: object, db_display_id: object, inventory_type: object) -> Outcome:
        if not (_valid_entry(entry) and _is_int(db_display_id) and _is_int(inventory_type)):
            return Miss.UNRESOLVABLE  # nothing to look up: no later view can do better
        with self._mu:
            records = self._loaded()
            record = records.get(entry)
            if record is not None:
                if record.display_id is not None:
                    return record.display_id
                # Symmetric: a null dated more than that in the future (a wrong clock) is not trusted.
                if abs(self._now() - record.checked_at) < NULL_RETRY_SECONDS:
                    return Miss.UNRESOLVABLE
            flight = self._flights.get(entry)
            leading = flight is None
            if flight is None:
                flight = self._flights[entry] = _Flight()
            else:
                flight.followers += 1
        if not leading:
            if not flight.done.wait(self._follow_timeout):
                return Miss.PENDING  # the leader is still asking; this view goes without
            return flight.outcome
        try:
            flight.outcome = self._look_up(entry, db_display_id, inventory_type)
            return flight.outcome
        finally:
            # The outcome is recorded (under _mu) before the flight leaves the table, so a
            # resolve arriving now finds one or the other.
            with self._mu:
                self._flights.pop(entry, None)
            flight.done.set()

    def _look_up(self, entry: int, db_display_id: int, inventory_type: int) -> Outcome:
        """Steps 2 and 3 of spec 8.1. Records definitive answers; a transient one is ``Miss.PENDING``, unrecorded."""
        first = self._meta_status(inventory_type, db_display_id)
        if first == 200:
            return self._record(entry, db_display_id)
        if first != 404:
            return Miss.PENDING  # switched off, cooling down, busy, upstream failure
        result = self.proxy.fetch_item_xml(entry)
        if result is None:
            return Miss.PENDING
        if result.status == 404:
            return self._record(entry, None)  # Wowhead has no such item
        if result.status != 200:
            return Miss.PENDING
        found = _parse_display_id(result.body)
        if found is None or found == db_display_id:
            return self._record(entry, None)
        second = self._meta_status(inventory_type, found)
        if second == 200:
            log.info("armory: item %d is display id %d on Wowhead (DB %d)", entry, found, db_display_id)
            return self._record(entry, found)
        if second == 404:
            return self._record(entry, None)
        return Miss.PENDING

    def _meta_status(self, inventory_type: int, display_id: int) -> int:
        """200 (exists), 404 (does not: also for an id the allow-list cannot express) or 503 (unknown now)."""
        return self.proxy.serve(meta_path(inventory_type, display_id)).status

    def _record(self, entry: int, display_id: int | None) -> Outcome:
        with self._mu:
            self._loaded()[entry] = _Record(display_id, int(self._now()))
            self._dirty = True
        if display_id is None:
            log.info(
                "armory: no 3D model found for item %d; asking again in %d days", entry, NULL_RETRY_SECONDS // 86_400
            )
            return Miss.UNRESOLVABLE
        return display_id

    # the map file

    def _loaded(self) -> dict[int, _Record]:
        """Caller holds ``_mu``. The in-memory map, read from disk on first use."""
        if self._map is None:
            self._map = self._read()
        return self._map

    def _read(self) -> dict[int, _Record]:
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return {}
        except OSError as err:
            log.warning("armory: %s unreadable (%s); starting with an empty map", self.path, err.strerror or err)
            return {}
        try:
            data = json.loads(raw)
        except ValueError:  # bad JSON or bad UTF-8
            data = None
        if not isinstance(data, dict):
            log.warning("armory: %s is not a display-ID map; starting with an empty map", self.path)
            return {}
        records: dict[int, _Record] = {}
        for key, value in data.items():
            parsed = _parse_record(key, value)
            if parsed is not None:
                records[parsed[0]] = parsed[1]
        dropped = len(data) - len(records)
        if dropped:
            log.warning("armory: %s had %d invalid entries; they are asked again", self.path, dropped)
        return records

    def _flush(self) -> None:
        """Write the map if anything changed. Never raises: an unwritable map keeps working in memory."""
        if not self._dirty:  # a stale read only skips a write the recording call does itself
            return
        with self._write_lock:
            with self._mu:
                if not self._dirty or self._map is None:
                    return
                data = _json_bytes(
                    {
                        str(entry): {"display_id": record.display_id, "checked_at": record.checked_at}
                        for entry, record in self._map.items()
                    }
                )
                self._dirty = False
            try:
                _make_private_dir(self.dir)
                _write_private_file(self.path, data)
            except OSError as err:
                with self._mu:
                    self._dirty = True  # the next call tries again
                if not self._write_failing:
                    self._write_failing = True
                    log.warning(
                        "armory: could not write %s (%s); display ids are kept in memory only",
                        self.path,
                        err.strerror or type(err).__name__,
                    )
                return
            if self._write_failing:
                self._write_failing = False
                log.info("armory: %s is writable again", self.path)


# --- the process-wide instance ---

_resolver: DisplayIdResolver | None = None
_resolver_lock = threading.Lock()


def get_resolver() -> DisplayIdResolver:
    """The one resolver of this process, on ``get_proxy()``'s proxy; building it touches nothing."""
    global _resolver
    proxy = armory_assets.get_proxy()
    with _resolver_lock:
        if _resolver is None or _resolver.proxy is not proxy:
            _resolver = DisplayIdResolver(proxy)
        return _resolver


def reset_resolver() -> None:
    """Forget the instance (tests)."""
    global _resolver
    with _resolver_lock:
        _resolver = None
