"""Armory display-ID resolver: the 3.3.5 display ids that Wowhead's Wrath data renumbered (spec 2, 8.1).

Every upstream answer comes from a fake ``fetch`` handed to a real ``AssetProxy``, so the
switch, cool-down and slot rules are the proxy's own; an autouse fixture makes any real
connection attempt fail the test. Thread tests wait on events with a timeout.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import re
import socket
import stat
import threading
import time
from pathlib import Path

import pytest

from app.services import armory_assets, armory_resolver
from app.services.armory import ViewerItem
from app.services.armory_assets import AssetProxy, FetchResult, UpstreamError, resolve_upstream
from app.services.armory_resolver import (
    MAP_FILE,
    NULL_RETRY_SECONDS,
    DisplayIdResolver,
    Miss,
    get_resolver,
    meta_path,
    reset_resolver,
)

MV = "https://wow.zamimg.com/modelviewer/"
WAIT = 5.0
T0 = 1_800_000_000.0
DAY = 86_400.0

# Spec 2: entry 6125 (Brawler's Harness, a shirt) has displayid 9995 in the 3.3.5 DB;
# Wowhead's Wrath data knows it as 9182.
SHIRT_ENTRY, SHIRT_DB_ID, SHIRT_WH_ID, SHIRT_TYPE = 6125, 9995, 9182, 4
SHIRT_DB_META = f"wrath/meta/armor/4/{SHIRT_DB_ID}.json"
SHIRT_WH_META = f"wrath/meta/armor/4/{SHIRT_WH_ID}.json"
XML_6125 = "https://www.wowhead.com/wotlk/item=6125&xml"


def _item_xml(icon_tag: bytes, entry: int = 6125) -> bytes:
    """A body shaped like Wowhead's item XML, with ``icon_tag`` where the icon element sits."""
    return (
        b'<?xml version="1.0" encoding="UTF-8"?><wowhead><item id="' + str(entry).encode() + b'">'
        b"<name><![CDATA[Brawler's Harness]]></name><level>1</level>"
        b'<quality id="1">Common</quality><class id="4"><![CDATA[Armor]]></class>'
        b'<subclass id="0"><![CDATA[Miscellaneous]]></subclass>'
        + icon_tag
        + b'<inventorySlot id="4">Shirt</inventorySlot>'
        b"<htmlTooltip><![CDATA[<table><tr><td>Brawler's Harness</td></tr></table>]]></htmlTooltip>"
        b"</item></wowhead>"
    )


ITEM_6125_XML = _item_xml(b'<icon displayId="9182">inv_shirt_06</icon>')


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("a test tried to open a network connection")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)


def _ok(body: bytes = b"{}") -> FetchResult:
    return FetchResult(200, body, None, None)


def _status(code: int) -> FetchResult:
    return FetchResult(code, b"", None, None)


class Clock:
    def __init__(self, now: float):
        self.now = now

    def __call__(self) -> float:
        return self.now


class FakeFetch:
    """Records every call; answers from ``answers`` (url -> FetchResult or exception), else a 404."""

    def __init__(self, answers: dict | None = None, *, gate: str | None = None):
        self.answers = dict(answers or {})
        self.calls: list[str] = []
        self.lock = threading.Lock()
        self.gate = gate  # calls for this url block until ``release`` is set
        self.release = threading.Event()
        self.entered = threading.Semaphore(0)

    def __call__(self, url: str, *, if_none_match: str | None = None) -> FetchResult:
        assert if_none_match is None  # the resolver never sends a conditional request
        with self.lock:
            self.calls.append(url)
        if url == self.gate:
            self.entered.release()
            if not self.release.wait(WAIT):
                raise AssertionError("the test never opened the gate")
        answer = self.answers.get(url, _status(404))
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def urls(self) -> list[str]:
        with self.lock:
            return list(self.calls)


class Rig:
    def __init__(self, tmp_path: Path, answers: dict | None = None, *, gate: str | None = None, **resolver_kwargs):
        self.fetch = FakeFetch(answers, gate=gate)
        self.mono = Clock(1000.0)
        self.wall = Clock(T0)
        self.data_dir = tmp_path / "armory"
        self.proxy = AssetProxy(self.data_dir, fetch=self.fetch, clock=self.mono, now=self.wall)
        self.resolver = DisplayIdResolver(self.proxy, self.data_dir, now=self.wall, **resolver_kwargs)
        self.map_path = self.data_dir / MAP_FILE

    def answer(self, local_path_or_url: str, result) -> None:
        url = local_path_or_url if local_path_or_url.startswith("https://") else MV + local_path_or_url
        self.fetch.answers[url] = result

    def saved(self) -> dict:
        return json.loads(self.map_path.read_text())

    def fresh(self) -> DisplayIdResolver:
        """A second resolver on the same files (what an admin restart sees)."""
        return DisplayIdResolver(self.proxy, self.data_dir, now=self.wall)


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def _in_thread(fn, *args):
    box: dict = {}

    def target():
        try:
            box["result"] = fn(*args)
        except BaseException as err:  # re-raised in the test thread by _joined
            box["error"] = err

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread, box


def _joined(thread: threading.Thread, box: dict):
    thread.join(WAIT)
    assert not thread.is_alive(), "worker thread hung"
    if "error" in box:
        raise box["error"]
    return box["result"]


def _wait_until(predicate, what: str) -> None:
    deadline = time.monotonic() + WAIT
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.005)


def _warnings(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == armory_resolver.__name__ and r.levelno >= logging.WARNING]


def _followers(resolver: DisplayIdResolver, entry: int) -> int:
    with resolver._mu:
        flight = resolver._flights.get(entry)
        return flight.followers if flight is not None else 0


# --- meta paths ---


def test_meta_path_armor_vs_item():
    armor = {1, 3, 4, 5, 6, 7, 8, 9, 10, 16, 19, 20}
    for inventory_type in sorted(armor):
        assert meta_path(inventory_type, 9182) == f"wrath/meta/armor/{inventory_type}/9182.json"
    # Weapons, shields, held items, ranged, and anything else: the item meta.
    for inventory_type in sorted(set(range(0, 30)) - armor):
        assert meta_path(inventory_type, 9182) == "wrath/meta/item/9182.json"
    # Every path it builds is one the proxy serves (the allow-list's own armor types).
    for inventory_type in range(0, 30):
        for display_id in (1, 9182, 99_999_999):
            upstream = resolve_upstream(meta_path(inventory_type, display_id))
            assert upstream is not None and upstream.kind == "meta", (inventory_type, display_id)


# --- the state machine ---


def test_resolver_uses_db_display_id_when_meta_exists_and_caches_it(tmp_path):
    rig = Rig(tmp_path, {MV + "wrath/meta/armor/5/33983.json": _ok(b'{"Model": 1}')})
    assert not rig.data_dir.exists()  # building the resolver touches nothing

    assert rig.resolver.resolve(16966, 33983, 5) == 33983
    assert rig.fetch.urls() == [MV + "wrath/meta/armor/5/33983.json"]  # no item XML lookup
    assert rig.saved() == {"16966": {"display_id": 33983, "checked_at": int(T0)}}
    # The probe went through the proxy, so the viewer's own request a moment later is a hit.
    assert rig.proxy.serve("wrath/meta/armor/5/33983.json").status == 200
    assert len(rig.fetch.calls) == 1

    # A found id is permanent: no upstream call again, not after weeks, not after a cache Clear,
    # not while the viewer is switched off, not in a new process.
    rig.wall.now += 400 * DAY
    rig.proxy.clear_cache()
    assert rig.resolver.resolve(16966, 33983, 5) == 33983
    rig.proxy.settings.set_enabled(False)
    assert rig.resolver.resolve(16966, 33983, 5) == 33983
    assert rig.fresh().resolve(16966, 33983, 5) == 33983
    assert len(rig.fetch.calls) == 1


def test_resolver_falls_back_to_item_xml_and_verifies_new_id(tmp_path):
    rig = Rig(tmp_path, {XML_6125: _ok(ITEM_6125_XML), MV + SHIRT_WH_META: _ok()})

    assert rig.resolver.resolve(SHIRT_ENTRY, SHIRT_DB_ID, SHIRT_TYPE) == SHIRT_WH_ID
    assert rig.fetch.urls() == [MV + SHIRT_DB_META, XML_6125, MV + SHIRT_WH_META]
    assert [url for url in rig.fetch.urls() if not url.startswith(MV)] == [XML_6125]
    assert rig.saved() == {"6125": {"display_id": SHIRT_WH_ID, "checked_at": int(T0)}}

    # The XML is never stored as an asset: the cache holds the verified meta and the 404 marker only.
    cached = {p.relative_to(rig.proxy.cache.root).as_posix() for p in rig.proxy.cache.root.rglob("*") if p.is_file()}
    assert cached == {SHIRT_WH_META, ".neg/" + SHIRT_DB_META}
    assert sorted(p.name for p in rig.data_dir.iterdir()) == ["cache", MAP_FILE]

    assert rig.resolver.resolve(SHIRT_ENTRY, SHIRT_DB_ID, SHIRT_TYPE) == SHIRT_WH_ID
    assert rig.fresh().resolve(SHIRT_ENTRY, SHIRT_DB_ID, SHIRT_TYPE) == SHIRT_WH_ID
    assert len(rig.fetch.calls) == 3


def test_resolver_falls_back_for_weapons_through_the_item_meta(tmp_path):
    xml_url = "https://www.wowhead.com/wotlk/item=19019&xml"
    rig = Rig(
        tmp_path,
        {
            xml_url: _ok(_item_xml(b'<icon displayId="30606">inv_sword_39</icon>', 19019)),
            MV + "wrath/meta/item/30606.json": _ok(),
        },
    )
    assert rig.resolver.resolve(19019, 29581, 22) == 30606  # an off-hand weapon, folded to 22
    assert rig.fetch.urls() == [MV + "wrath/meta/item/29581.json", xml_url, MV + "wrath/meta/item/30606.json"]


def test_resolver_records_null_after_double_404_and_retries_after_seven_days(tmp_path):
    # Entry 6125: the DB id's meta is missing and so is the meta of the id Wowhead's XML names.
    # Entry 4242: the DB id's meta is missing and Wowhead has no such item at all.
    rig = Rig(tmp_path, {XML_6125: _ok(ITEM_6125_XML)})
    assert rig.resolver.resolve(SHIRT_ENTRY, SHIRT_DB_ID, SHIRT_TYPE) is None
    assert rig.resolver.resolve(4242, 777, 7) is None
    xml_4242 = "https://www.wowhead.com/wotlk/item=4242&xml"
    assert rig.fetch.urls() == [
        MV + SHIRT_DB_META,
        XML_6125,
        MV + SHIRT_WH_META,
        MV + "wrath/meta/armor/7/777.json",
        xml_4242,
    ]
    assert rig.saved() == {
        "4242": {"display_id": None, "checked_at": int(T0)},
        "6125": {"display_id": None, "checked_at": int(T0)},
    }

    # Known unresolvable: no upstream call for 7 days, in this process or the next.
    rig.wall.now = T0 + NULL_RETRY_SECONDS - 1
    assert rig.resolver.resolve(SHIRT_ENTRY, SHIRT_DB_ID, SHIRT_TYPE) is None
    assert rig.fresh().resolve(4242, 777, 7) is None
    assert len(rig.fetch.calls) == 5

    # After 7 days it is asked again; Wowhead has the DB id's meta by now.
    assert NULL_RETRY_SECONDS == 7 * DAY
    rig.wall.now = T0 + NULL_RETRY_SECONDS
    rig.answer(SHIRT_DB_META, _ok())
    assert rig.resolver.resolve(SHIRT_ENTRY, SHIRT_DB_ID, SHIRT_TYPE) == SHIRT_DB_ID
    assert rig.fetch.urls()[5:] == [MV + SHIRT_DB_META]
    assert rig.saved()["6125"] == {"display_id": SHIRT_DB_ID, "checked_at": int(T0 + NULL_RETRY_SECONDS)}
    # A null that is still unresolvable after the retry is re-dated, not dropped.
    assert rig.resolver.resolve(4242, 777, 7) is None
    assert rig.fetch.urls()[6:] == [MV + "wrath/meta/armor/7/777.json", xml_4242]
    assert rig.saved()["4242"] == {"display_id": None, "checked_at": int(T0 + NULL_RETRY_SECONDS)}

    # A null dated far in the future (a clock that was wrong when it was written) is not trusted.
    rig.map_path.write_text(json.dumps({"4242": {"display_id": None, "checked_at": T0 + 30 * DAY}}))
    rig.wall.now += 2 * 3600  # past the proxy's 1 h memory of the 404s
    assert rig.fresh().resolve(4242, 777, 7) is None
    assert len(rig.fetch.calls) == 10


@pytest.mark.parametrize(
    "icon_tag",
    [
        b"<icon>inv_shirt_06</icon>",  # no displayId at all
        b'<icon displayId="">inv_shirt_06</icon>',
        b'<icon displayId="abc">inv_shirt_06</icon>',
        b'<icon displayId="-9182">inv_shirt_06</icon>',
        b"<icon displayId='9182'>inv_shirt_06</icon>",  # not the attribute form the pattern takes
        b'<icon displayId="0">inv_shirt_06</icon>',  # ids are positive
        b'<icon displayId="000">inv_shirt_06</icon>',
        b'<icon displayId="123456789">inv_shirt_06</icon>',  # more than 8 digits
        b'<icon displayId="\xef\xbc\x99\xef\xbc\x91\xef\xbc\x98\xef\xbc\x92">x</icon>',  # full-width digits
        b'<icon> displayId="9182"</icon>',  # outside the tag
        b'<item displayId="9182"/>',  # not the icon element
        b"\xff\xfe\x00garbage",
    ],
    ids=[
        "no-attr",
        "empty",
        "letters",
        "negative",
        "single-quotes",
        "zero",
        "zeros",
        "nine-digits",
        "fullwidth",
        "outside-tag",
        "other-tag",
        "garbage",
    ],
)
def test_resolver_ignores_malformed_xml(tmp_path, icon_tag):
    rig = Rig(tmp_path, {XML_6125: _ok(_item_xml(icon_tag)), MV + SHIRT_WH_META: _ok()})
    assert rig.resolver.resolve(SHIRT_ENTRY, SHIRT_DB_ID, SHIRT_TYPE) is None
    assert rig.fetch.urls() == [MV + SHIRT_DB_META, XML_6125]  # no id to verify
    assert rig.saved() == {"6125": {"display_id": None, "checked_at": int(T0)}}  # a definitive answer


@pytest.mark.parametrize("body", [b"", b"not xml at all", b"<wowhead><error>Item not found!</error></wowhead>"])
def test_resolver_records_null_for_an_xml_without_an_icon(tmp_path, body):
    rig = Rig(tmp_path, {XML_6125: _ok(body)})
    assert rig.resolver.resolve(SHIRT_ENTRY, SHIRT_DB_ID, SHIRT_TYPE) is None
    assert rig.fetch.urls() == [MV + SHIRT_DB_META, XML_6125]
    assert rig.saved()["6125"]["display_id"] is None


def test_resolver_records_null_when_the_xml_names_the_db_id_again(tmp_path):
    body = _item_xml(b'<icon displayId="9995">inv_shirt_06</icon>')
    rig = Rig(tmp_path, {XML_6125: _ok(body)})
    assert rig.resolver.resolve(SHIRT_ENTRY, SHIRT_DB_ID, SHIRT_TYPE) is None
    assert rig.fetch.urls() == [MV + SHIRT_DB_META, XML_6125]  # its meta is already known missing
    assert rig.saved()["6125"]["display_id"] is None


def test_resolver_parses_the_first_icon_and_never_backtracks_quadratically(tmp_path):
    body = _item_xml(b'<icon size="large" displayId="9182">a</icon><icon displayId="1">b</icon>')
    rig = Rig(tmp_path, {XML_6125: _ok(body), MV + SHIRT_WH_META: _ok()})
    assert rig.resolver.resolve(SHIRT_ENTRY, SHIRT_DB_ID, SHIRT_TYPE) == SHIRT_WH_ID

    # A hostile 12 MB body (the fetcher allows 16 MB) that a plain re.search would take hours on.
    hostile = b"<icon " * 2_000_000
    rig.answer("https://www.wowhead.com/wotlk/item=5&xml", _ok(hostile))
    started = time.monotonic()
    assert rig.resolver.resolve(5, 5, 4) is None
    assert time.monotonic() - started < 3.0
    assert rig.saved()["5"]["display_id"] is None


def test_parse_matches_the_plain_pattern_on_ordinary_bodies():
    pattern = re.compile(rb'<icon[^>]*displayId="(\d+)"')
    bodies = [
        ITEM_6125_XML,
        _item_xml(b'<icon a="1" displayId="12" b="2" displayId="34">x</icon>'),
        _item_xml(b'<icon displayId="5"><icon displayId="6">'),
        b'<iconic displayId="77">',
        b'x<icon displayId="1"',
        b'<icon><icon displayId="8">',
        b"",
    ]
    for body in bodies:
        match = pattern.search(body)
        expected = int(match.group(1)) if match else None
        assert armory_resolver._parse_display_id(body) == expected, body


@pytest.mark.parametrize(
    "where, failure",
    [
        ("db-meta", _status(502)),
        ("db-meta", _status(429)),
        ("db-meta", UpstreamError("timeout")),
        ("xml", _status(503)),
        ("xml", _status(429)),
        ("xml", _status(403)),
        ("xml", UpstreamError("connection failed")),
        ("wh-meta", _status(500)),
        ("wh-meta", UpstreamError("timeout")),
    ],
    ids=[
        "meta-502",
        "meta-429",
        "meta-error",
        "xml-503",
        "xml-429",
        "xml-403",
        "xml-error",
        "wh-meta-500",
        "wh-meta-error",
    ],
)
def test_resolver_transient_failures_record_nothing_and_retry_later(tmp_path, where, failure):
    rig = Rig(tmp_path, {XML_6125: _ok(ITEM_6125_XML), MV + SHIRT_WH_META: _ok()})
    target = {"db-meta": MV + SHIRT_DB_META, "xml": XML_6125, "wh-meta": MV + SHIRT_WH_META}[where]
    healthy = rig.fetch.answers.get(target, _status(404))
    rig.fetch.answers[target] = failure

    assert rig.resolver.resolve(SHIRT_ENTRY, SHIRT_DB_ID, SHIRT_TYPE) is None
    assert rig.fetch.urls()[-1] == target  # it stopped at the failure, without a retry
    assert rig.fetch.urls().count(target) == 1
    assert not rig.map_path.exists()  # nothing recorded

    # The failing host cools down: a resolve now asks nothing of it and still records nothing.
    calls = len(rig.fetch.calls)
    assert rig.resolver.resolve(SHIRT_ENTRY, SHIRT_DB_ID, SHIRT_TYPE) is None
    assert target not in rig.fetch.urls()[calls:]
    assert not rig.map_path.exists()

    # Once it is over and upstream answers again, the next view resolves and records.
    rig.mono.now += 61
    rig.fetch.answers[target] = healthy
    assert rig.resolver.resolve(SHIRT_ENTRY, SHIRT_DB_ID, SHIRT_TYPE) == SHIRT_WH_ID
    assert rig.saved()["6125"]["display_id"] == SHIRT_WH_ID


def test_resolver_records_nothing_while_the_viewer_is_switched_off(tmp_path):
    rig = Rig(tmp_path, {XML_6125: _ok(ITEM_6125_XML), MV + SHIRT_WH_META: _ok()})
    rig.proxy.settings.set_enabled(False)
    assert rig.resolver.resolve(SHIRT_ENTRY, SHIRT_DB_ID, SHIRT_TYPE) is None
    assert rig.fetch.calls == []
    assert not rig.map_path.exists()

    # Switched off after the DB id's 404 was seen: the item lookup is not attempted either.
    rig.proxy.settings.set_enabled(True)
    rig.proxy.cache.mark_negative(SHIRT_DB_META)
    rig.proxy.settings.set_enabled(False)
    assert rig.resolver.resolve(SHIRT_ENTRY, SHIRT_DB_ID, SHIRT_TYPE) is None
    assert rig.fetch.calls == []
    assert not rig.map_path.exists()

    rig.proxy.settings.set_enabled(True)
    assert rig.resolver.resolve(SHIRT_ENTRY, SHIRT_DB_ID, SHIRT_TYPE) == SHIRT_WH_ID
    assert rig.fetch.urls() == [XML_6125, MV + SHIRT_WH_META]


@pytest.mark.parametrize(
    "args",
    [
        (0, 9995, 4),
        (-1, 9995, 4),
        (True, 9995, 4),
        ("6125", 9995, 4),
        (None, 9995, 4),
        (2**32, 9995, 4),
        (6125, None, 4),
        (6125, "9995", 4),
        (6125, 9995, None),
        (6125, 9995, True),
    ],
)
def test_resolver_refuses_bad_arguments_without_asking_anyone(tmp_path, args):
    rig = Rig(tmp_path)
    assert rig.resolver.resolve(*args) is None
    assert rig.fetch.calls == []
    assert not rig.map_path.exists()


def test_resolver_single_flight_per_entry(tmp_path):
    rig = Rig(tmp_path, {XML_6125: _ok(ITEM_6125_XML), MV + SHIRT_WH_META: _ok()}, gate=XML_6125)
    leader = _in_thread(rig.resolver.resolve, SHIRT_ENTRY, SHIRT_DB_ID, SHIRT_TYPE)
    assert rig.fetch.entered.acquire(timeout=WAIT)  # the leader is inside the item lookup
    follower = _in_thread(rig.resolver.resolve, SHIRT_ENTRY, SHIRT_DB_ID, SHIRT_TYPE)
    _wait_until(lambda: _followers(rig.resolver, SHIRT_ENTRY) == 1, "the second resolve to join the first")
    rig.fetch.release.set()
    assert _joined(*leader) == _joined(*follower) == SHIRT_WH_ID
    assert rig.fetch.urls() == [MV + SHIRT_DB_META, XML_6125, MV + SHIRT_WH_META]
    assert rig.resolver._flights == {}


def test_resolver_single_flight_shares_a_transient_failure(tmp_path):
    rig = Rig(tmp_path, {XML_6125: _status(503)}, gate=XML_6125)
    leader = _in_thread(rig.resolver.resolve, SHIRT_ENTRY, SHIRT_DB_ID, SHIRT_TYPE)
    assert rig.fetch.entered.acquire(timeout=WAIT)
    follower = _in_thread(rig.resolver.resolve, SHIRT_ENTRY, SHIRT_DB_ID, SHIRT_TYPE)
    _wait_until(lambda: _followers(rig.resolver, SHIRT_ENTRY) == 1, "the second resolve to join the first")
    rig.fetch.release.set()
    assert _joined(*leader) is None and _joined(*follower) is None
    assert rig.fetch.urls().count(XML_6125) == 1
    assert not rig.map_path.exists()


def test_resolver_follower_gives_up_after_its_wait_budget(tmp_path):
    rig = Rig(tmp_path, {XML_6125: _ok(ITEM_6125_XML), MV + SHIRT_WH_META: _ok()}, gate=XML_6125, follow_timeout_s=0.1)
    leader = _in_thread(rig.resolver.resolve, SHIRT_ENTRY, SHIRT_DB_ID, SHIRT_TYPE)
    assert rig.fetch.entered.acquire(timeout=WAIT)
    assert rig.resolver.resolve(SHIRT_ENTRY, SHIRT_DB_ID, SHIRT_TYPE) is None  # waited 0.1 s, then gave up
    rig.fetch.release.set()
    assert _joined(*leader) == SHIRT_WH_ID
    assert rig.fetch.urls().count(XML_6125) == 1


# --- the map file ---


def test_resolver_map_file_is_private_and_written_atomically(tmp_path):
    rig = Rig(tmp_path, {MV + "wrath/meta/armor/5/33983.json": _ok(), MV + "wrath/meta/item/30606.json": _ok()})
    assert rig.resolver.resolve(16966, 33983, 5) == 33983
    rig.wall.now += 10
    assert rig.resolver.resolve(19019, 30606, 13) == 30606
    assert _mode(rig.map_path) == 0o600 and _mode(rig.data_dir) == 0o700
    assert rig.saved() == {
        "16966": {"display_id": 33983, "checked_at": int(T0)},
        "19019": {"display_id": 30606, "checked_at": int(T0 + 10)},
    }
    assert sorted(p.name for p in rig.data_dir.iterdir()) == ["cache", MAP_FILE]  # no temp file left


@pytest.mark.parametrize(
    "content",
    [b"{not json", b"[]", b'"x"', b"null", b"\xff\xfe", b""],
    ids=["bad-json", "list", "string", "null", "bad-utf8", "empty"],
)
def test_resolver_corrupt_map_starts_empty_and_logs_once(tmp_path, caplog, content):
    rig = Rig(tmp_path, {MV + "wrath/meta/armor/5/33983.json": _ok()})
    rig.data_dir.mkdir(mode=0o700)
    rig.map_path.write_bytes(content)
    with caplog.at_level(logging.WARNING, logger=armory_resolver.__name__):
        assert rig.resolver.resolve(16966, 33983, 5) == 33983
        assert rig.resolver.resolve(16966, 33983, 5) == 33983
    warnings = _warnings(caplog)
    assert len(warnings) == 1 and "displayid-map.json" in warnings[0].getMessage()
    assert rig.saved() == {"16966": {"display_id": 33983, "checked_at": int(T0)}}  # replaced by a good one


def test_resolver_map_keeps_valid_entries_and_drops_bad_ones(tmp_path, caplog):
    rig = Rig(tmp_path)
    rig.data_dir.mkdir(mode=0o700)
    good = {
        "16966": {"display_id": 33983, "checked_at": int(T0)},
        "6125": {"display_id": None, "checked_at": int(T0)},
    }
    bad = {
        "abc": {"display_id": 1, "checked_at": int(T0)},
        "0": {"display_id": 1, "checked_at": int(T0)},
        "07": {"display_id": 1, "checked_at": int(T0)},
        "8": {"display_id": "9182", "checked_at": int(T0)},
        "9": {"display_id": True, "checked_at": int(T0)},
        "10": {"display_id": 123456789, "checked_at": int(T0)},
        "11": {"display_id": 5},
        "12": {"display_id": 5, "checked_at": "today"},
        "13": [5, int(T0)],
        "14": {"display_id": 5, "checked_at": float("nan")},
        "15": {"display_id": None, "checked_at": 10**400},  # would overflow the age check
        "16": {"display_id": None, "checked_at": -1},
        "4294967296": {"display_id": 5, "checked_at": int(T0)},
    }
    rig.map_path.write_text(json.dumps({**good, **bad}))
    with caplog.at_level(logging.WARNING, logger=armory_resolver.__name__):
        assert rig.resolver.resolve(16966, 33983, 5) == 33983
        assert rig.resolver.resolve(6125, 9995, 4) is None
        for entry in (8, 9, 10, 11, 12, 13, 14, 15, 16):
            rig.resolver.resolve(entry, 5, 4)  # bad entries are not used: each is resolved afresh
    assert len(_warnings(caplog)) == 1
    # One probe of the shared DB id, then the proxy remembers its 404.
    assert [url for url in rig.fetch.urls() if "item=" not in url] == [MV + "wrath/meta/armor/4/5.json"]
    assert {k: v for k, v in rig.saved().items() if k in good} == good


def test_resolver_unreadable_map_starts_empty(tmp_path, caplog):
    rig = Rig(tmp_path, {MV + "wrath/meta/armor/5/33983.json": _ok()})
    rig.map_path.mkdir(parents=True)  # a directory where the file should be: unreadable and unwritable
    with caplog.at_level(logging.WARNING, logger=armory_resolver.__name__):
        assert rig.resolver.resolve(16966, 33983, 5) == 33983
        assert rig.resolver.resolve(16966, 33983, 5) == 33983
    assert len(rig.fetch.calls) == 1  # kept in memory
    assert rig.map_path.is_dir()
    assert len(_warnings(caplog)) == 2  # cannot read it, cannot write it: each said once
    assert sorted(p.name for p in rig.data_dir.iterdir()) == ["cache", MAP_FILE]  # no temp file left


def test_resolver_unwritable_map_keeps_resolving_in_memory(tmp_path, monkeypatch, caplog):
    rig = Rig(tmp_path, {MV + "wrath/meta/armor/5/33983.json": _ok(), MV + "wrath/meta/item/30606.json": _ok()})
    real_write = armory_resolver._write_private_file

    def refuse(path, data):
        raise PermissionError(errno.EACCES, "Permission denied", str(path))

    monkeypatch.setattr(armory_resolver, "_write_private_file", refuse)
    with caplog.at_level(logging.INFO, logger=armory_resolver.__name__):
        assert rig.resolver.resolve(16966, 33983, 5) == 33983
        assert rig.resolver.resolve(19019, 30606, 13) == 30606
        assert rig.resolver.resolve(16966, 33983, 5) == 33983
        assert len(rig.fetch.calls) == 2  # remembered in memory
        assert not rig.map_path.exists()
        assert len(_warnings(caplog)) == 1

        monkeypatch.setattr(armory_resolver, "_write_private_file", real_write)
        assert rig.resolver.resolve(16966, 33983, 5) == 33983  # the next call writes what memory holds
    assert sorted(rig.saved()) == ["16966", "19019"]
    assert len(_warnings(caplog)) == 1


# --- several items ---


def test_resolve_items_swaps_ids_and_lists_unresolved_slots(tmp_path):
    rig = Rig(
        tmp_path,
        {
            MV + "wrath/meta/armor/3/28351.json": _ok(),  # shoulders: DB id exists
            XML_6125: _ok(ITEM_6125_XML),  # shirt: renumbered
            MV + SHIRT_WH_META: _ok(),
            MV + "wrath/meta/item/31000.json": _ok(),  # main hand: DB id exists
            MV + "wrath/meta/armor/19/5555.json": _status(502),  # tabard: Wowhead hiccup
        },
    )
    items = [
        ViewerItem(16, 22, 29581),  # off hand: DB id and Wowhead both 404
        ViewerItem(3, SHIRT_TYPE, SHIRT_DB_ID),
        ViewerItem(0, 1, 4444),  # head: entry unknown
        ViewerItem(15, 13, 31000),
        ViewerItem(2, 3, 28351),
        ViewerItem(18, 19, 5555),
    ]
    entries = {16: 19019, 3: SHIRT_ENTRY, 15: 30000, 2: 16000, 18: 5976}

    sent, unresolved, pending = rig.resolver.resolve_items(items, entries)
    assert sent == [ViewerItem(3, SHIRT_TYPE, SHIRT_WH_ID), ViewerItem(15, 13, 31000), ViewerItem(2, 3, 28351)]
    assert unresolved == [0, 16]  # definitive: no entry; DB id and Wowhead both 404
    assert pending == [18]  # transient: the tabard's hiccup
    assert MV + "wrath/meta/item/29581.json" in rig.fetch.urls()  # the folded off-hand type asks the item meta
    assert not any("4444" in url for url in rig.fetch.urls())  # an unknown entry is never looked up
    assert rig.saved() == {
        "6125": {"display_id": SHIRT_WH_ID, "checked_at": int(T0)},
        "16000": {"display_id": 28351, "checked_at": int(T0)},
        "19019": {"display_id": None, "checked_at": int(T0)},
        "30000": {"display_id": 31000, "checked_at": int(T0)},
    }  # the tabard's hiccup recorded nothing

    # Seen again: everything known comes from the map; only the tabard is asked again.
    calls = len(rig.fetch.calls)
    rig.mono.now += 61
    rig.fetch.answers[MV + "wrath/meta/armor/19/5555.json"] = _ok()
    sent, unresolved, pending = rig.resolver.resolve_items(items, entries)
    assert [item.slot for item in sent] == [3, 15, 2, 18]
    assert unresolved == [0, 16] and pending == []
    assert rig.fetch.urls()[calls:] == [MV + "wrath/meta/armor/19/5555.json"]
    assert rig.resolver.resolve_items([], {}) == ([], [], [])


def test_resolve_items_writes_the_map_once(tmp_path, monkeypatch):
    rig = Rig(tmp_path, {MV + f"wrath/meta/armor/5/{n}.json": _ok() for n in range(1, 6)})
    writes: list[Path] = []
    real_write = armory_resolver._write_private_file

    def counting(path, data):
        writes.append(path)
        real_write(path, data)

    monkeypatch.setattr(armory_resolver, "_write_private_file", counting)
    items = [ViewerItem(slot, 5, slot) for slot in range(1, 6)]
    sent, unresolved, pending = rig.resolver.resolve_items(items, {slot: 100 + slot for slot in range(1, 6)})
    assert [item.display_id for item in sent] == [1, 2, 3, 4, 5] and unresolved == [] and pending == []
    assert writes == [rig.map_path]
    rig.resolver.resolve_items(items, {slot: 100 + slot for slot in range(1, 6)})
    assert writes == [rig.map_path]  # nothing new: nothing written



# --- definitive vs transient misses (R23) ---


def test_resolve_outcome_tells_a_definitive_miss_from_a_transient_one(tmp_path):
    rig = Rig(tmp_path, {MV + SHIRT_DB_META: _ok(), XML_6125: _status(404)})
    assert rig.resolver.resolve_outcome(SHIRT_ENTRY, SHIRT_DB_ID, SHIRT_TYPE) == SHIRT_DB_ID

    # Definitive: the DB id's meta and the item lookup are both 404 (recorded null) ...
    assert rig.resolver.resolve_outcome(4242, 777, 7) is Miss.UNRESOLVABLE
    assert rig.resolver.resolve_outcome(4242, 777, 7) is Miss.UNRESOLVABLE  # ... also from the map
    # ... and so is an entry that cannot be looked up at all.
    assert rig.resolver.resolve_outcome(0, 777, 7) is Miss.UNRESOLVABLE

    # Transient: nothing recorded, a later view asks again.
    rig.fetch.answers[MV + "wrath/meta/armor/7/888.json"] = _status(503)
    assert rig.resolver.resolve_outcome(4343, 888, 7) is Miss.PENDING  # upstream failure
    assert rig.resolver.resolve_outcome(4444, 999, 7) is Miss.PENDING  # the host cools down
    rig.mono.now += 61
    rig.proxy.settings.set_enabled(False)
    assert rig.resolver.resolve_outcome(4545, 1111, 7) is Miss.PENDING  # switched off
    assert sorted(int(key) for key in rig.saved()) == [4242, 6125]

    # The plain resolve still answers None for both kinds.
    assert rig.resolver.resolve(4242, 777, 7) is None
    assert rig.resolver.resolve(4545, 1111, 7) is None


def test_resolve_outcome_follower_timeout_is_pending(tmp_path):
    rig = Rig(tmp_path, {XML_6125: _ok(ITEM_6125_XML), MV + SHIRT_WH_META: _ok()}, gate=XML_6125, follow_timeout_s=0.1)
    leader = _in_thread(rig.resolver.resolve_outcome, SHIRT_ENTRY, SHIRT_DB_ID, SHIRT_TYPE)
    assert rig.fetch.entered.acquire(timeout=WAIT)
    assert rig.resolver.resolve_outcome(SHIRT_ENTRY, SHIRT_DB_ID, SHIRT_TYPE) is Miss.PENDING
    rig.fetch.release.set()
    assert _joined(*leader) == SHIRT_WH_ID


def test_resolve_outcome_followers_share_the_leaders_definitive_miss(tmp_path):
    rig = Rig(tmp_path, {XML_6125: _status(404)}, gate=XML_6125)
    leader = _in_thread(rig.resolver.resolve_outcome, SHIRT_ENTRY, SHIRT_DB_ID, SHIRT_TYPE)
    assert rig.fetch.entered.acquire(timeout=WAIT)
    follower = _in_thread(rig.resolver.resolve_outcome, SHIRT_ENTRY, SHIRT_DB_ID, SHIRT_TYPE)
    _wait_until(lambda: _followers(rig.resolver, SHIRT_ENTRY) == 1, "the second resolve to join the first")
    rig.fetch.release.set()
    assert _joined(*leader) is Miss.UNRESOLVABLE
    assert _joined(*follower) is Miss.UNRESOLVABLE


class _Blocking:
    """A fetch that holds every call until ``release``; counts how many are inside at once."""

    def __init__(self):
        self.mu = threading.Lock()
        self.active = 0
        self.peak = 0
        self.entered = threading.Semaphore(0)
        self.release = threading.Event()

    def __call__(self, url: str, *, if_none_match: str | None = None) -> FetchResult:
        with self.mu:
            self.active += 1
            self.peak = max(self.peak, self.active)
        self.entered.release()
        try:
            if not self.release.wait(WAIT):
                raise AssertionError("the test never released the fetches")
            return _ok()
        finally:
            with self.mu:
                self.active -= 1


@pytest.mark.parametrize("workers, expected", [(1, 1), (2, 2), (4, 4), (10, 4)])
def test_resolve_items_runs_at_most_the_given_workers_and_writes_the_map_once(
    tmp_path, monkeypatch, workers, expected
):
    fetch = _Blocking()
    # More proxy slots than workers, so only the worker count can limit the lookups at once.
    proxy = AssetProxy(tmp_path / "armory", fetch=fetch, max_parallel=8)
    resolver = DisplayIdResolver(proxy, tmp_path / "armory")
    writes: list[Path] = []
    real_write = armory_resolver._write_private_file

    def counting(path, data):
        writes.append(path)
        real_write(path, data)

    monkeypatch.setattr(armory_resolver, "_write_private_file", counting)
    items = [ViewerItem(slot, 5, 100 + slot) for slot in range(6)]
    entries = {slot: 7000 + slot for slot in range(6)}
    run = _in_thread(lambda: resolver.resolve_items(items, entries, workers=workers))
    for _ in range(expected):
        assert fetch.entered.acquire(timeout=WAIT)
    time.sleep(0.2)  # give a worker beyond the limit the chance to show up
    with fetch.mu:
        peak = fetch.peak
    fetch.release.set()
    sent, unresolved, pending = _joined(*run)
    assert peak == expected
    assert [item.slot for item in sent] == list(range(6))  # input order kept
    assert unresolved == [] and pending == []
    assert writes == [resolver.path]  # one write per call, however many workers


# --- the process-wide instance ---


def test_get_resolver_is_a_lazy_singleton_on_the_proxy(tmp_path, monkeypatch):
    admin_data = tmp_path / "admin-data"
    monkeypatch.setenv("ADMIN_DATA_DIR", str(admin_data))
    resolver = get_resolver()
    assert get_resolver() is resolver
    assert resolver.proxy is armory_assets.get_proxy()
    assert resolver.path == admin_data / "armory" / MAP_FILE
    assert not admin_data.exists()  # building it touches neither the disk nor the network
    reset_resolver()
    assert get_resolver() is not resolver
    # A new proxy (tests reset it) gets a new resolver, never one bound to the old proxy.
    current = get_resolver()
    armory_assets.reset_proxy()
    assert get_resolver() is not current
    assert get_resolver().proxy is armory_assets.get_proxy()
