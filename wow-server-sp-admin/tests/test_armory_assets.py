"""Armory assets: the upstream allow-list, the locked-down fetcher, the disk cache and the proxy.

No test here opens a network socket: ``urllib.request.build_opener`` is replaced by a
fake opener that records the request and answers from canned data, the proxy gets a
fake ``fetch``, and an autouse fixture makes any real connection attempt fail the test.
Thread tests wait on events with a timeout, so a bug fails the test instead of hanging it.
"""

from __future__ import annotations

import email.message
import errno
import hashlib
import http.client
import io
import json
import os
import socket
import stat
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.services import armory_assets
from app.services.armory_assets import (
    ArmorySettings,
    AssetCache,
    AssetProxy,
    FetchResult,
    ProxyStatus,
    Served,
    Upstream,
    UpstreamError,
    ViewerError,
    ViewerStatus,
    fetch_upstream,
    get_proxy,
    item_xml_url,
    reset_proxy,
    resolve_upstream,
)

MV = "https://wow.zamimg.com/modelviewer/"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("a test tried to open a network connection")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)


# --- allow-list ---


def test_resolve_upstream_exact_urls_for_every_allowed_shape():
    js, json_, bin_ = "application/javascript", "application/json", "application/octet-stream"
    expected = {
        "live/viewer/viewer.min.js": Upstream(MV + "live/viewer/viewer.min.js", js, "viewer"),
        "wrath/meta/character/1.json": Upstream(MV + "wrath/meta/character/1.json", json_, "meta"),
        "wrath/meta/character/22.json": Upstream(MV + "wrath/meta/character/22.json", json_, "meta"),
        "wrath/meta/charactercustomization/11.json": Upstream(
            MV + "wrath/meta/charactercustomization/11.json", json_, "meta"
        ),
        "wrath/meta/armor/1/45020.json": Upstream(MV + "wrath/meta/armor/1/45020.json", json_, "meta"),
        "wrath/meta/item/9182.json": Upstream(MV + "wrath/meta/item/9182.json", json_, "meta"),
        "wrath/meta/itemvisual/12345678.json": Upstream(MV + "wrath/meta/itemvisual/12345678.json", json_, "meta"),
        "wrath/m2/123.m2": Upstream(MV + "wrath/m2/123.m2", bin_, "model"),
        "wrath/skin/123.skin": Upstream(MV + "wrath/skin/123.skin", bin_, "model"),
        "wrath/textures/7.webp": Upstream(MV + "wrath/textures/7.webp", "image/webp", "texture"),
        "icons/inv_chest_leather_04.jpg": Upstream(
            "https://wow.zamimg.com/images/wow/icons/large/inv_chest_leather_04.jpg", "image/jpeg", "icon"
        ),
        "icons/ability_warrior-shield_wall.jpg": Upstream(
            "https://wow.zamimg.com/images/wow/icons/large/ability_warrior-shield_wall.jpg", "image/jpeg", "icon"
        ),
    }
    for path, upstream in expected.items():
        assert resolve_upstream(path) == upstream, path
    # Every armor type the viewer asks for, and only those.
    for t in (1, 3, 4, 5, 6, 7, 8, 9, 10, 16, 19, 20):
        assert resolve_upstream(f"wrath/meta/armor/{t}/1.json") == Upstream(
            MV + f"wrath/meta/armor/{t}/1.json", json_, "meta"
        )
    for t in (0, 2, 11, 12, 13, 14, 15, 17, 18, 21, 22, 23, 26, 28):
        assert resolve_upstream(f"wrath/meta/armor/{t}/1.json") is None, t
    # Every character model id 1-22 (race*2 - 1 + gender), for both meta kinds.
    for n in range(1, 23):
        assert resolve_upstream(f"wrath/meta/character/{n}.json").url == MV + f"wrath/meta/character/{n}.json"
        assert resolve_upstream(f"wrath/meta/charactercustomization/{n}.json").kind == "meta"


def test_resolve_upstream_rejects_crafted_paths():
    crafted = [
        # From the plan.
        "../x",
        "wrath/m2/../../etc/passwd",
        "wrath/meta/armor/2/1.json",
        "wrath/meta/character/23.json",
        "https://wow.zamimg.com/x",
        "wrath/m2/1.m2?x=1",
        "icons/INV.jpg",
        "live/viewer/viewer.js",
        # Leading slash, doubled slashes, dot segments, encodings, separators.
        "/wrath/m2/1.m2",
        "wrath//m2/1.m2",
        "wrath/m2//1.m2",
        "./wrath/m2/1.m2",
        "wrath/./m2/1.m2",
        "wrath/m2/1.m2/",
        "wrath\\m2\\1.m2",
        "wrath/m2/%31.m2",
        "wrath%2fm2%2f1.m2",
        "wrath/m2/1.m2#x",
        "live/viewer/viewer.min.js?v=1",
        "live/viewer/viewer.min.js.prev",
        "live/viewer/viewer.meta.json",
        "//wow.zamimg.com/modelviewer/wrath/m2/1.m2",
        # Whitespace, control and non-ASCII characters.
        "wrath/m2/1.m2\n",
        "wrath/m2/1.m2 ",
        " wrath/m2/1.m2",
        "wrath/m2/1.m2\x00",
        "wrath/m2/١.m2",  # Arabic-Indic digit one
        "icons/inv_chest_leather_04.jpg\r",
        # Out-of-range ids and shapes.
        "wrath/meta/character/0.json",
        "wrath/meta/character/01.json",
        "wrath/meta/character/-1.json",
        "wrath/meta/charactercustomization/23.json",
        "wrath/meta/armor/01/1.json",
        "wrath/meta/armor/1/123456789.json",
        "wrath/m2/123456789.m2",
        "wrath/m2/.m2",
        "wrath/m2/1.M2",
        "wrath/m2/1.mo3",
        "wrath/mo3/1.mo3",
        "wrath/textures/1.png",
        "wrath/textures/1.webp.js",
        "classic/m2/1.m2",
        "live/meta/character/1.json",
        "wrath/viewer/viewer.min.js",
        "modelviewer/wrath/m2/1.m2",
        "wrath/meta/character/1",
        # Icons: lowercase basename only, 1-64 characters, .jpg only.
        "icons/Inv_chest_leather_04.jpg",
        "icons/.jpg",
        "icons/" + "a" * 65 + ".jpg",
        "icons/inv.chest.jpg",
        "icons/inv chest.jpg",
        "icons/inv_chest.png",
        "icons/large/inv_chest.jpg",
        "icons/inv_chest.JPG",
        # The XML lookup is internal only; it is not a proxy path.
        "item-displayid/6125",
        "wotlk/item=6125&xml",
        # Nothing at all.
        "",
        "/",
        ".",
        "..",
    ]
    for path in crafted:
        assert resolve_upstream(path) is None, repr(path)
    assert resolve_upstream("icons/" + "a" * 64 + ".jpg") is not None
    for not_a_string in (None, 1, b"wrath/m2/1.m2", ["wrath/m2/1.m2"]):
        assert resolve_upstream(not_a_string) is None


def test_resolve_upstream_only_ever_points_at_the_model_cdn():
    for path in ("live/viewer/viewer.min.js", "wrath/meta/item/1.json", "wrath/m2/1.m2", "icons/a.jpg"):
        assert resolve_upstream(path).url.startswith("https://wow.zamimg.com/"), path


def test_item_xml_url_is_exact_and_takes_positive_ints_only():
    assert item_xml_url(6125) == "https://www.wowhead.com/wotlk/item=6125&xml"
    assert item_xml_url(1) == "https://www.wowhead.com/wotlk/item=1&xml"
    for bad in (0, -1, True, False, "6125", 6125.0, None):
        with pytest.raises(ValueError):
            item_xml_url(bad)


# --- fetcher ---


def _headers(**fields: str) -> email.message.Message:
    msg = email.message.Message()
    for name, value in fields.items():
        msg[name.replace("_", "-")] = value
    return msg


class FakeResponse:
    def __init__(self, body: bytes = b"", *, status: int = 200, headers=None, url: str | None = None):
        self._stream = io.BytesIO(body)
        self.status = status
        self.headers = headers if headers is not None else _headers()
        self._url = url
        self.reads: list[int] = []
        self.closed = False

    def read(self, amt: int | None = None) -> bytes:
        self.reads.append(amt)
        return self._stream.read(amt)

    def getcode(self) -> int:
        return self.status

    def geturl(self) -> str | None:
        return self._url

    def close(self) -> None:
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class FakeOpener:
    def __init__(self, answer):
        self.answer = answer
        self.requests: list[urllib.request.Request] = []
        self.timeouts: list[float | None] = []
        self.addheaders = [("User-agent", "Python-urllib/3.12")]

    def open(self, req, data=None, timeout=None):
        self.requests.append(req)
        self.timeouts.append(timeout)
        if isinstance(self.answer, BaseException):
            raise self.answer
        if isinstance(self.answer, FakeResponse) and self.answer._url is None:
            self.answer._url = req.full_url
        return self.answer


@pytest.fixture
def fake_opener(monkeypatch):
    """Replace ``urllib.request.build_opener``; the returned holder exposes the handlers and opener."""

    class Holder:
        handlers: list = []
        opener: FakeOpener | None = None
        answer = FakeResponse(b"")
        builds = 0

    def build_opener(*handlers):
        Holder.builds += 1
        Holder.handlers = list(handlers)
        Holder.opener = FakeOpener(Holder.answer)
        return Holder.opener

    monkeypatch.setattr(urllib.request, "build_opener", build_opener)
    return Holder


def test_fetch_upstream_sends_only_the_fixed_headers_and_no_cookies(fake_opener):
    url = MV + "wrath/m2/123.m2"
    fake_opener.answer = FakeResponse(
        b"m2-bytes", headers=_headers(ETag='"abc"', Last_Modified="Wed, 07 Oct 2026 10:00:00 GMT", Set_Cookie="a=b")
    )

    result = fetch_upstream(url)

    assert result == FetchResult(200, b"m2-bytes", '"abc"', "Wed, 07 Oct 2026 10:00:00 GMT")
    opener = fake_opener.opener
    (req,) = opener.requests
    assert req.full_url == url
    assert req.get_method() == "GET"
    assert req.data is None
    assert {k.lower(): v for k, v in req.header_items()} == {
        "user-agent": "wow-server-sp-admin/1.0",
        "accept-encoding": "identity",
        "accept": "*/*",
    }
    assert opener.timeouts == [20.0]
    # The opener's default headers (Python's own User-Agent) are never added.
    assert opener.addheaders == []
    # No environment proxy, redirects refused, no cookie jar.
    proxies = [h for h in fake_opener.handlers if isinstance(h, urllib.request.ProxyHandler)]
    assert len(proxies) == 1 and proxies[0].proxies == {}
    assert any(isinstance(h, armory_assets._NoRedirect) for h in fake_opener.handlers)
    assert not any(isinstance(h, urllib.request.HTTPCookieProcessor) for h in fake_opener.handlers)

    # A conditional GET adds If-None-Match and nothing else; a custom timeout is passed through.
    fake_opener.answer = FakeResponse(b"js")
    fetch_upstream(MV + "live/viewer/viewer.min.js", if_none_match='"abc"', timeout=5.0)
    req = fake_opener.opener.requests[0]
    assert {k.lower(): v for k, v in req.header_items()} == {
        "user-agent": "wow-server-sp-admin/1.0",
        "accept-encoding": "identity",
        "accept": "*/*",
        "if-none-match": '"abc"',
    }
    assert fake_opener.opener.timeouts == [5.0]

    # An empty etag is an unconditional GET; one carrying CR/LF never reaches a request.
    fake_opener.answer = FakeResponse(b"js")
    fetch_upstream(MV + "live/viewer/viewer.min.js", if_none_match="")
    assert "if-none-match" not in {k.lower() for k, _ in fake_opener.opener.requests[0].header_items()}
    builds = fake_opener.builds
    for bad in ('"a"\r\nCookie: x=1', '"a"\n', 5):
        with pytest.raises(UpstreamError) as exc:
            fetch_upstream(MV + "live/viewer/viewer.min.js", if_none_match=bad)  # type: ignore[arg-type]
        assert exc.value.reason == "not allowed"
    assert fake_opener.builds == builds
    fetch_upstream(MV + "live/viewer/viewer.min.js", if_none_match='W/"5f3a-17b"')


def test_real_opener_has_no_proxy_cookie_jar_or_redirect_following(monkeypatch):
    monkeypatch.setenv("https_proxy", "http://proxy.invalid:3128")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")
    # Control: a default opener would route through the environment's proxy.
    default = urllib.request.build_opener()
    assert any(isinstance(h, urllib.request.ProxyHandler) for h in default.handlers)

    opener = armory_assets._build_opener()
    assert opener.addheaders == []
    # ProxyHandler({}) registers nothing, so no proxy handler is installed at all.
    assert not any(isinstance(h, urllib.request.ProxyHandler) for h in opener.handlers)
    assert [type(h) for h in opener.handle_open["https"]] == [urllib.request.HTTPSHandler]
    redirects = [h for h in opener.handlers if isinstance(h, urllib.request.HTTPRedirectHandler)]
    assert len(redirects) == 1 and isinstance(redirects[0], armory_assets._NoRedirect)
    assert not any(isinstance(h, urllib.request.HTTPCookieProcessor) for h in opener.handlers)
    req = urllib.request.Request(MV + "wrath/m2/1.m2")
    assert redirects[0].redirect_request(req, None, 302, "Found", _headers(), "https://evil.invalid/") is None


def test_fetch_upstream_treats_redirect_and_oversize_as_errors(fake_opener):
    url = MV + "wrath/m2/1.m2"
    for code in (300, 301, 302, 303, 307, 308):
        fake_opener.answer = urllib.error.HTTPError(
            url, code, "Moved", _headers(Location="https://evil.invalid/x"), io.BytesIO(b"")
        )
        with pytest.raises(UpstreamError) as exc:
            fetch_upstream(url)
        assert exc.value.reason == "redirect", code

    # A response for another URL than the one asked for is a redirect too.
    fake_opener.answer = FakeResponse(b"x", url="https://wow.zamimg.com/elsewhere")
    with pytest.raises(UpstreamError) as exc:
        fetch_upstream(url)
    assert exc.value.reason == "redirect"

    # Exactly at the cap is fine; one byte over is an error.
    fake_opener.answer = FakeResponse(b"a" * 1000)
    assert fetch_upstream(url, max_bytes=1000).body == b"a" * 1000
    fake_opener.answer = FakeResponse(b"a" * 1001)
    with pytest.raises(UpstreamError) as exc:
        fetch_upstream(url, max_bytes=1000)
    assert exc.value.reason == "too large"
    assert all(n is not None and n <= 1001 for n in fake_opener.answer.reads)
    assert fake_opener.answer.closed

    # A declared Content-Length over the cap fails before any body is read.
    fake_opener.answer = FakeResponse(b"a" * 10, headers=_headers(Content_Length="999999999"))
    with pytest.raises(UpstreamError) as exc:
        fetch_upstream(url, max_bytes=1000)
    assert exc.value.reason == "too large"
    assert fake_opener.answer.reads == []

    # The default cap is 16 MB.
    big = 16 * 1024 * 1024
    fake_opener.answer = FakeResponse(b"\0" * (big + 1))
    with pytest.raises(UpstreamError):
        fetch_upstream(url)


def test_fetch_upstream_refuses_urls_outside_the_allow_list_before_opening(fake_opener):
    refused = [
        "http://wow.zamimg.com/modelviewer/wrath/m2/1.m2",
        "ftp://wow.zamimg.com/modelviewer/wrath/m2/1.m2",
        "file:///etc/passwd",
        "data:,hello",
        "//wow.zamimg.com/modelviewer/wrath/m2/1.m2",
        "https://evil.invalid/modelviewer/wrath/m2/1.m2",
        "https://wow.zamimg.com.evil.invalid/modelviewer/wrath/m2/1.m2",
        "https://user@wow.zamimg.com/modelviewer/wrath/m2/1.m2",
        "https://wow.zamimg.com:8443/modelviewer/wrath/m2/1.m2",
        "https://WOW.ZAMIMG.COM/modelviewer/wrath/m2/1.m2",
        "https://wow.zamimg.com/modelviewer/wrath/m2/1.m2?x=1",
        "https://wow.zamimg.com/modelviewer/wrath/m2/1.m2#x",
        "https://wow.zamimg.com/modelviewer/wrath/m2/../../x",
        "https://wow.zamimg.com/modelviewer/wrath/mo3/1.mo3",
        "https://wow.zamimg.com/modelviewer/viewer/viewer.css",
        "https://wow.zamimg.com/images/wow/icons/large/INV.jpg",
        "https://wow.zamimg.com/images/wow/icons/medium/inv.jpg",
        "https://wow.zamimg.com/modelviewer/icons/inv.jpg",
        "https://www.wowhead.com/wotlk/item=0&xml",
        "https://www.wowhead.com/wotlk/item=6125",
        "https://www.wowhead.com/wotlk/item=6125&xml&x=1",
        "https://www.wowhead.com/item=6125&xml",
        "https://www.wowhead.com/modelviewer/wrath/m2/1.m2",
        "https://wowhead.com/wotlk/item=6125&xml",
        "https://www.wowhead.com/wotlk/item=6125&xml\n",
        "https://wow.zamimg.com/modelviewer/wrath/m2/1.m2 ",
        "",
    ]
    for url in refused:
        with pytest.raises(UpstreamError) as exc:
            fetch_upstream(url)
        assert exc.value.reason == "not allowed", url
    assert fake_opener.builds == 0
    with pytest.raises(UpstreamError):
        fetch_upstream(None)  # type: ignore[arg-type]

    # The two hosts' allowed shapes do go out.
    fake_opener.answer = FakeResponse(b"<wowhead/>")
    assert fetch_upstream(item_xml_url(6125)).status == 200
    assert fake_opener.opener.requests[0].full_url == "https://www.wowhead.com/wotlk/item=6125&xml"
    fake_opener.answer = FakeResponse(b"jpg")
    assert fetch_upstream(resolve_upstream("icons/inv_chest_leather_04.jpg").url).body == b"jpg"


def test_fetch_upstream_returns_404_304_and_other_statuses(fake_opener):
    url = MV + "wrath/meta/item/1.json"
    fake_opener.answer = urllib.error.HTTPError(url, 404, "Not Found", _headers(), io.BytesIO(b"<html>"))
    assert fetch_upstream(url) == FetchResult(404, b"", None, None)

    fake_opener.answer = urllib.error.HTTPError(
        url, 304, "Not Modified", _headers(ETag='"v1"', Last_Modified="Tue"), io.BytesIO(b"")
    )
    assert fetch_upstream(url, if_none_match='"v1"') == FetchResult(304, b"", '"v1"', "Tue")

    for code in (403, 429, 500, 502, 503):
        fake_opener.answer = urllib.error.HTTPError(url, code, "Err", _headers(), io.BytesIO(b"x"))
        assert fetch_upstream(url).status == code


def test_fetch_upstream_maps_network_failures_to_short_reasons(fake_opener, monkeypatch):
    url = MV + "wrath/m2/1.m2"
    cases = [
        (urllib.error.URLError(socket.timeout("timed out")), "timeout"),
        (TimeoutError("timed out"), "timeout"),
        (urllib.error.URLError(ConnectionRefusedError(111, "refused")), "connection failed"),
        (urllib.error.URLError("unknown url type"), "connection failed"),
        (ConnectionResetError(104, "reset"), "connection failed"),
        (http.client.RemoteDisconnected("closed"), "connection failed"),
        (http.client.BadStatusLine("garbage"), "bad response"),
    ]
    for error, reason in cases:
        fake_opener.answer = error
        with pytest.raises(UpstreamError) as exc:
            fetch_upstream(url)
        assert exc.value.reason == reason, error
        assert str(exc.value) == reason

    class BrokenRead(FakeResponse):
        def __init__(self, error):
            super().__init__(b"")
            self.error = error

        def read(self, amt=None):
            raise self.error

    for error, reason in (
        (TimeoutError("timed out"), "timeout"),
        (http.client.IncompleteRead(b"ab", 10), "bad response"),
        (ConnectionResetError(104, "reset"), "connection failed"),
    ):
        fake_opener.answer = BrokenRead(error)
        with pytest.raises(UpstreamError) as exc:
            fetch_upstream(url)
        assert exc.value.reason == reason

    # A body that keeps trickling in past the timeout is abandoned.
    ticks = iter([0.0, 5.0, 25.0, 30.0, 35.0])
    monkeypatch.setattr(armory_assets, "_monotonic", lambda: next(ticks))
    monkeypatch.setattr(armory_assets, "_READ_CHUNK", 4)
    fake_opener.answer = FakeResponse(b"x" * 64)
    with pytest.raises(UpstreamError) as exc:
        fetch_upstream(url)
    assert exc.value.reason == "timeout"


# --- disk cache ---


class Clock:
    def __init__(self, now: float = 1_800_000_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def _files(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file() or p.is_symlink()}


def test_cache_put_get_touch_and_negative_expiry(tmp_path):
    clock = Clock()
    root = tmp_path / "cache"
    cache = AssetCache(root, clock=clock)

    assert cache.get("wrath/m2/1.m2") is None
    stored = cache.put("wrath/m2/1.m2", b"model")
    assert stored == root.resolve() / "wrath" / "m2" / "1.m2"
    assert stored.read_bytes() == b"model"
    assert _mode(stored) == 0o600
    for d in (root, root / "wrath", root / "wrath" / "m2"):
        assert _mode(d) == 0o700, d
    assert os.stat(stored).st_mtime == pytest.approx(clock.now, abs=0.01)
    assert _files(root) == {"wrath/m2/1.m2"}  # no temp file left behind

    # A hit touches the mtime (least-recently-hit eviction).
    clock.now += 500
    assert cache.get("wrath/m2/1.m2") == stored
    assert os.stat(stored).st_mtime == pytest.approx(clock.now, abs=0.01)

    # Replacing keeps the file private and swaps the content.
    cache.put("wrath/m2/1.m2", b"model-v2")
    assert stored.read_bytes() == b"model-v2"
    assert _mode(stored) == 0o600

    # Negative entries: valid for 3600 s, then gone.
    assert cache.negative("wrath/m2/2.m2") is False
    start = clock.now
    cache.mark_negative("wrath/m2/2.m2")
    marker = root.resolve() / ".neg" / "wrath" / "m2" / "2.m2"
    assert marker.is_file() and _mode(marker) == 0o600
    assert cache.get("wrath/m2/2.m2") is None
    clock.now = start + 3599
    assert cache.negative("wrath/m2/2.m2") is True
    clock.now = start + 3600
    assert cache.negative("wrath/m2/2.m2") is False
    assert not marker.exists()

    # A successful store supersedes a negative entry.
    cache.mark_negative("wrath/m2/3.m2")
    assert cache.negative("wrath/m2/3.m2") is True
    cache.put("wrath/m2/3.m2", b"found")
    assert cache.negative("wrath/m2/3.m2") is False

    # A directory where a file is expected is not a hit.
    (root / "wrath" / "skin" / "5.skin").mkdir(parents=True)
    assert cache.get("wrath/skin/5.skin") is None


def test_cache_put_failure_leaves_old_file_and_no_temp(tmp_path, monkeypatch):
    cache = AssetCache(tmp_path / "cache", clock=Clock())
    cache.put("wrath/m2/1.m2", b"old")

    def broken_replace(src, dst):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(armory_assets.os, "replace", broken_replace)
    with pytest.raises(OSError):
        cache.put("wrath/m2/1.m2", b"new")
    monkeypatch.undo()
    assert (tmp_path / "cache" / "wrath" / "m2" / "1.m2").read_bytes() == b"old"
    assert _files(tmp_path / "cache") == {"wrath/m2/1.m2"}


def test_cache_evicts_oldest_to_eighty_percent_but_keeps_viewer_files(tmp_path):
    clock = Clock(1_000_000.0)
    root = tmp_path / "cache"
    cache = AssetCache(root, cap_bytes=1000, clock=clock)

    # The pinned viewer files are the oldest of all, yet never evicted.
    for name, size in (("viewer.min.js", 300), ("viewer.min.js.prev", 100), ("viewer.meta.json", 20)):
        cache.put(f"live/viewer/{name}", b"v" * size)
    for i in range(1, 5):  # four 100-byte assets, a=1 oldest
        clock.now += 10
        cache.put(f"wrath/m2/{i}.m2", b"m" * 100)
    assert cache.size_bytes() == 820

    clock.now += 10
    cache.get("wrath/m2/1.m2")  # the oldest is hit again: now the most recently used

    clock.now += 10
    cache.put("wrath/textures/9.webp", b"t" * 150)  # 970 bytes: still under the cap
    assert cache.size_bytes() == 970

    clock.now += 10
    cache.put("wrath/skin/8.skin", b"s" * 120)  # 1090 > 1000 -> evict to <= 800
    assert cache.size_bytes() <= 800
    remaining = _files(root)
    assert {"live/viewer/viewer.min.js", "live/viewer/viewer.min.js.prev", "live/viewer/viewer.meta.json"} <= remaining
    assert "wrath/m2/2.m2" not in remaining and "wrath/m2/3.m2" not in remaining  # oldest-hit first
    assert {"wrath/m2/1.m2", "wrath/textures/9.webp", "wrath/skin/8.skin"} <= remaining
    assert cache.get("wrath/m2/2.m2") is None

    # Eviction runs only from put: a get on an over-cap tree deletes nothing.
    tight = AssetCache(root, cap_bytes=100, clock=clock)
    before = _files(root)
    assert tight.get("wrath/m2/1.m2") is not None
    tight.negative("wrath/m2/77.m2")
    assert _files(root) == before
    # An explicit evict_if_needed() still never touches the viewer files.
    tight.evict_if_needed()
    assert _files(root) == {"live/viewer/viewer.min.js", "live/viewer/viewer.min.js.prev", "live/viewer/viewer.meta.json"}

    # The file just stored is kept even when it alone is over the cap.
    huge = tight.put("wrath/m2/5.m2", b"h" * 500)
    assert huge.read_bytes() == b"h" * 500


def test_cache_put_under_the_cap_does_not_walk_the_tree(tmp_path, monkeypatch):
    """A miss write is O(1): the byte total is kept running and the tree is walked only over the cap."""
    clock = Clock()
    root = tmp_path / "cache"
    cache = AssetCache(root, cap_bytes=1000, clock=clock)
    walks: list[int] = []
    real_files = AssetCache._files

    def counting_files(self):
        walks.append(1)
        return real_files(self)

    monkeypatch.setattr(AssetCache, "_files", counting_files)

    def fresh_walk() -> int:
        return sum(os.lstat(p).st_size for p in root.rglob("*") if p.is_file() and not p.is_symlink())

    cache.put("wrath/m2/1.m2", b"a" * 100)  # the first write learns the total with one walk
    assert len(walks) == 1
    walks.clear()
    for i in range(2, 8):
        clock.now += 1
        cache.put(f"wrath/m2/{i}.m2", b"b" * 100)
    cache.mark_negative("wrath/m2/99.m2")
    assert cache.get("wrath/m2/1.m2") is not None  # touched at the newest time
    assert cache.negative("wrath/m2/99.m2") is True
    assert cache.size_bytes() == 700 == fresh_walk()
    assert walks == []

    # Replacing a file counts only the difference.
    cache.put("wrath/m2/2.m2", b"c" * 250)
    assert cache.size_bytes() == 850 == fresh_walk()
    assert walks == []

    # Over the cap: one walk, eviction to <= 80 %, and the running total matches the tree again.
    clock.now += 1
    cache.put("wrath/skin/1.skin", b"d" * 200)  # 1050 > 1000
    assert len(walks) == 1
    assert cache.size_bytes() == fresh_walk() == 750  # 3.m2, 4.m2, 5.m2 (least recently hit) went
    walks.clear()
    clock.now += 1
    cache.put("wrath/skin/2.skin", b"e" * 10)
    assert walks == []
    assert cache.size_bytes() == fresh_walk() == 760

    # Clear resets it; the next size is learnt from the tree once.
    cache.put("live/viewer/viewer.min.js", b"v" * 30)
    assert cache.clear() == 7  # 1, 2, 6, 7 .m2, two .skin files and the negative marker
    assert cache.size_bytes() == fresh_walk() == 30
    walks.clear()
    cache.put("wrath/m2/1.m2", b"f" * 5)
    assert walks == []
    assert cache.size_bytes() == fresh_walk() == 35


def test_cache_clear_keeps_viewer_files_and_drops_negatives(tmp_path):
    clock = Clock()
    root = tmp_path / "cache"
    cache = AssetCache(root, clock=clock)
    cache.put("live/viewer/viewer.min.js", b"js" * 10)
    cache.put("live/viewer/viewer.min.js.prev", b"old")
    cache.put("live/viewer/viewer.meta.json", b"{}")
    cache.put("wrath/m2/1.m2", b"a")
    cache.put("wrath/meta/character/1.json", b"{}")
    cache.put("icons/inv_chest_leather_04.jpg", b"jpg")
    cache.mark_negative("wrath/m2/2.m2")
    cache.mark_negative("wrath/meta/item/5.json")
    stray_tmp = root / "wrath" / "m2" / ".1.m2.abc.tmp"
    stray_tmp.write_bytes(b"partial")

    removed = cache.clear()

    assert removed == 6  # three assets, two negative markers, one stray temp file
    assert _files(root) == {"live/viewer/viewer.min.js", "live/viewer/viewer.min.js.prev", "live/viewer/viewer.meta.json"}
    assert (root / "live" / "viewer" / "viewer.min.js").read_bytes() == b"js" * 10
    assert cache.negative("wrath/m2/2.m2") is False
    assert cache.get("wrath/m2/1.m2") is None
    assert not (root / "wrath").exists() and not (root / "icons").exists() and not (root / ".neg").exists()
    assert cache.size_bytes() == 20 + 3 + 2
    assert cache.clear() == 0

    # Clearing a cache that was never written is a no-op.
    assert AssetCache(tmp_path / "absent").clear() == 0


def test_cache_clear_is_best_effort_and_never_follows_links(tmp_path, monkeypatch):
    root = tmp_path / "cache"
    cache = AssetCache(root, clock=Clock())
    cache.put("wrath/m2/1.m2", b"a")
    cache.put("wrath/m2/2.m2", b"b")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.bin").write_bytes(b"not ours")
    (root / "wrath" / "dirlink").symlink_to(outside)

    real_unlink = os.unlink

    def stubborn_unlink(path, *args, **kwargs):
        if str(path).endswith("1.m2"):
            raise PermissionError(13, "Permission denied")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(armory_assets.os, "unlink", stubborn_unlink)
    assert cache.clear() == 2  # 2.m2 and the directory link; 1.m2 could not be removed
    monkeypatch.undo()
    assert _files(root) == {"wrath/m2/1.m2"}
    assert (outside / "keep.bin").read_bytes() == b"not ours"


def test_cache_size_bytes_counts_every_regular_file(tmp_path):
    root = tmp_path / "cache"
    cache = AssetCache(root, clock=Clock())
    assert cache.size_bytes() == 0
    cache.put("live/viewer/viewer.min.js", b"x" * 7)
    cache.put("wrath/m2/1.m2", b"x" * 11)
    cache.mark_negative("wrath/m2/9.m2")
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"x" * 1000)
    (root / "wrath" / "link.m2").symlink_to(outside)  # not counted: never followed
    assert cache.size_bytes() == 18


def test_cache_path_for_refuses_escape(tmp_path):
    root = tmp_path / "cache"
    root.mkdir()
    cache = AssetCache(root, clock=Clock())
    assert cache.path_for("wrath/m2/1.m2") == root.resolve() / "wrath" / "m2" / "1.m2"
    assert cache.path_for("live/viewer/viewer.min.js.prev") == root.resolve() / "live" / "viewer" / "viewer.min.js.prev"

    for bad in (
        "../x",
        "a/../../x",
        "wrath/m2/../../../etc/passwd",
        "/etc/passwd",
        "a//b",
        "a/./b",
        "./a",
        "a/",
        "",
        ".",
        "..",
        ".neg/wrath/m2/1.m2",
        "wrath/.hidden",
        "a\\b",
        "a\x00b",
        None,
        5,
    ):
        with pytest.raises(ValueError):
            cache.path_for(bad)  # type: ignore[arg-type]

    # Symlink tricks: a directory link that leaves root, and a file link.
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_bytes(b"secret")
    (root / "evil").symlink_to(outside)
    with pytest.raises(ValueError):
        cache.path_for("evil/secret")
    with pytest.raises(ValueError):
        cache.put("evil/x.m2", b"x")
    assert not (outside / "x.m2").exists()
    (root / "wrath").mkdir()
    (root / "wrath" / "link.m2").symlink_to(outside / "secret")
    with pytest.raises(ValueError):
        cache.path_for("wrath/link.m2")
    with pytest.raises(ValueError):
        cache.get("wrath/link.m2")
    assert (outside / "secret").read_bytes() == b"secret"

    # A root that is itself a symlink is fine: containment is checked on resolved paths.
    linked_root = tmp_path / "linked"
    linked_root.symlink_to(root)
    assert AssetCache(linked_root, clock=Clock()).path_for("wrath/m2/1.m2") == root.resolve() / "wrath" / "m2" / "1.m2"


def test_cache_defaults():
    cache = AssetCache(Path("/nonexistent-armory-cache"))
    assert cache.cap_bytes == 300 * 1024 * 1024
    assert armory_assets.NEGATIVE_TTL_SECONDS == 3600


def test_cache_creates_missing_ancestors_private_and_leaves_existing_dirs_alone(tmp_path):
    existing = tmp_path / "admin-data"
    existing.mkdir(mode=0o755)
    existing.chmod(0o755)
    cache = AssetCache(existing / "armory" / "cache", clock=Clock())
    cache.put("wrath/m2/1.m2", b"x")
    for created in ("armory", "armory/cache", "armory/cache/wrath", "armory/cache/wrath/m2"):
        assert _mode(existing / created) == 0o700, created
    assert _mode(existing) == 0o755  # not ours to change


def test_cache_remove_keeps_the_running_total(tmp_path):
    root = tmp_path / "cache"
    cache = AssetCache(root, clock=Clock())
    cache.put("live/viewer/viewer.min.js.prev", b"x" * 40)
    cache.put("wrath/m2/1.m2", b"x" * 2)
    assert cache.size_bytes() == 42
    assert cache.remove("live/viewer/viewer.min.js.prev") is True
    assert cache.remove("live/viewer/viewer.min.js.prev") is False
    assert not (root / "live" / "viewer" / "viewer.min.js.prev").exists()
    assert cache.size_bytes() == 2
    with pytest.raises(ValueError):
        cache.remove("../x")


# --- asset proxy ---

M2 = "wrath/m2/123.m2"
M2_URL = MV + M2
SKIN = "wrath/skin/5.skin"
VIEWER = "live/viewer/viewer.min.js"
VIEWER_URL = MV + VIEWER
ICON = "icons/inv_chest_leather_04.jpg"
ICON_URL = "https://wow.zamimg.com/images/wow/icons/large/inv_chest_leather_04.jpg"
IMMUTABLE = "public, max-age=31536000, immutable"
WAIT = 5.0  # every wait in the thread tests is bounded


def _ok(body: bytes, etag: str | None = None, last_modified: str | None = None) -> FetchResult:
    return FetchResult(200, body, etag, last_modified)


def _status(code: int) -> FetchResult:
    return FetchResult(code, b"", None, None)


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class FakeFetch:
    """Records every call; answers from ``answers`` (url -> FetchResult or exception), else a 200."""

    def __init__(self, answers: dict | None = None):
        self.answers = dict(answers or {})
        self.calls: list[tuple[str, str | None]] = []
        self.lock = threading.Lock()

    def answer(self, url: str) -> FetchResult:
        answer = self.answers.get(url)
        if answer is None:
            return _ok(b"body of " + url.encode())
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def __call__(self, url: str, *, if_none_match: str | None = None) -> FetchResult:
        with self.lock:
            self.calls.append((url, if_none_match))
        return self.answer(url)

    def urls(self) -> list[str]:
        with self.lock:
            return [url for url, _etag in self.calls]


class GateFetch(FakeFetch):
    """Every call blocks until ``release`` is set; ``entered`` is released once per call that got in."""

    def __init__(self, answers: dict | None = None):
        super().__init__(answers)
        self.release = threading.Event()
        self.entered = threading.Semaphore(0)
        self.inside = 0
        self.max_inside = 0

    def __call__(self, url: str, *, if_none_match: str | None = None) -> FetchResult:
        with self.lock:
            self.calls.append((url, if_none_match))
            self.inside += 1
            self.max_inside = max(self.max_inside, self.inside)
        self.entered.release()
        try:
            if not self.release.wait(WAIT):
                raise AssertionError("the test never opened the gate")
            return self.answer(url)
        finally:
            with self.lock:
                self.inside -= 1


class Rig:
    def __init__(self, tmp_path: Path, fetch: FakeFetch | None = None, **kwargs):
        self.fetch = fetch if fetch is not None else FakeFetch()
        self.mono = Clock(1000.0)
        self.wall = Clock()
        self.data_dir = tmp_path / "armory"
        self.root = self.data_dir / "cache"
        self.proxy = AssetProxy(self.data_dir, fetch=self.fetch, clock=self.mono, now=self.wall, **kwargs)

    def file(self, local_path: str) -> Path:
        return self.root.resolve().joinpath(*local_path.split("/"))


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


def _followers(proxy: AssetProxy, local_path: str) -> int:
    with proxy._inflight_lock:
        flight = proxy._inflight.get(local_path)
        return flight.followers if flight is not None else 0


def _tree_bytes(root: Path) -> int:
    return sum(p.stat().st_size for p in root.rglob("*") if p.is_file() and not p.is_symlink())


def test_settings_default_on_persist_private_and_corrupt_means_on(tmp_path):
    data_dir = tmp_path / "admin-data" / "armory"
    settings = ArmorySettings(data_dir)
    assert settings.enabled() is True  # no file: spec default On
    assert not data_dir.exists()  # reading never creates anything

    settings.set_enabled(False)
    path = data_dir / "armory.json"
    assert json.loads(path.read_text()) == {"enabled": False}
    assert _mode(path) == 0o600 and _mode(data_dir) == 0o700
    assert ArmorySettings(data_dir).enabled() is False
    assert sorted(p.name for p in data_dir.iterdir()) == ["armory.json"]  # no temp file left
    settings.set_enabled(True)
    assert settings.enabled() is True

    for corrupt in ("{not json", "[]", '{"enabled": "no"}', '{"enabled": 0}', "{}", ""):
        path.write_text(corrupt)
        assert settings.enabled() is True, corrupt
    path.write_bytes(b"\xff\xfe")
    assert settings.enabled() is True
    with pytest.raises(TypeError):
        settings.set_enabled("off")  # type: ignore[arg-type]


def test_serve_miss_fetches_once_then_hits_without_fetch(tmp_path):
    rig = Rig(tmp_path, FakeFetch({M2_URL: _ok(b"m2 bytes")}))
    first = rig.proxy.serve(M2)
    assert first.status == 200
    assert first.path == rig.file(M2) and first.body is None
    assert first.path.read_bytes() == b"m2 bytes"
    assert first.content_type == "application/octet-stream"
    assert first.cache_control == IMMUTABLE
    assert first.etag and first.etag.startswith('"') and first.etag.endswith('"')
    assert _mode(rig.data_dir) == 0o700  # the cache creates armory/ too, private like its own tree

    # Hits never contact upstream; the ETag stays the same although every hit touches the mtime.
    rig.wall.now += 1000
    again = rig.proxy.serve(M2)
    assert again == first
    rig.wall.now += 1000
    assert rig.proxy.serve(M2).etag == first.etag
    assert rig.fetch.calls == [(M2_URL, None)]

    # The ETag is the file's identity: another path gives another one.
    rig.fetch.answers[MV + "wrath/m2/124.m2"] = _ok(b"m2 bytes")
    assert rig.proxy.serve("wrath/m2/124.m2").etag != first.etag
    icon = rig.proxy.serve(ICON)
    assert (icon.status, icon.path, icon.content_type, icon.cache_control) == (
        200,
        rig.file(ICON),
        "image/jpeg",
        IMMUTABLE,
    )
    assert rig.fetch.urls() == [M2_URL, MV + "wrath/m2/124.m2", ICON_URL]

    # Anything outside the allow-list is a 404 that never reaches upstream or the disk.
    for bad in (
        "../x",
        "wrath/m2/abc.m2",
        "live/viewer/viewer.min.js.prev",
        "live/viewer/viewer.meta.json",
        "item-displayid/6125",
        ".neg/wrath/m2/123.m2",
        "wrath/m2/123.m2?x=1",
        "",
        None,
    ):
        served = rig.proxy.serve(bad)  # type: ignore[arg-type]
        assert served.status == 404 and served.path is None and served.body is None, bad
        assert served.cache_control == "no-store"
    assert len(rig.fetch.calls) == 3


def test_serve_single_flight_concurrent_same_path_fetches_once(tmp_path):
    rig = Rig(tmp_path, GateFetch({M2_URL: _ok(b"one copy")}))
    leader = _in_thread(rig.proxy.serve, M2)
    assert rig.fetch.entered.acquire(timeout=WAIT)
    follower = _in_thread(rig.proxy.serve, M2)
    _wait_until(lambda: _followers(rig.proxy, M2) == 1, "the second request to join the fetch")
    rig.fetch.release.set()
    first, second = _joined(*leader), _joined(*follower)
    assert first.status == second.status == 200
    assert first.path == second.path == rig.file(M2)
    assert second.etag == first.etag
    assert rig.fetch.urls() == [M2_URL]
    assert rig.proxy._inflight == {}


def test_serve_single_flight_shares_a_failure_without_a_second_fetch(tmp_path):
    rig = Rig(tmp_path, GateFetch({M2_URL: _status(502)}))
    leader = _in_thread(rig.proxy.serve, M2)
    assert rig.fetch.entered.acquire(timeout=WAIT)
    follower = _in_thread(rig.proxy.serve, M2)
    _wait_until(lambda: _followers(rig.proxy, M2) == 1, "the second request to join the fetch")
    rig.fetch.release.set()
    first, second = _joined(*leader), _joined(*follower)
    assert first.status == second.status == 503
    assert second == first
    assert rig.fetch.urls() == [M2_URL]


def test_serve_caps_parallel_upstream_fetches_at_four(tmp_path):
    rig = Rig(tmp_path, GateFetch())
    workers = [_in_thread(rig.proxy.serve, f"wrath/m2/{n}.m2") for n in range(8)]
    for _ in range(4):
        assert rig.fetch.entered.acquire(timeout=WAIT)
    assert not rig.fetch.entered.acquire(timeout=0.3)  # a fifth fetch would have started by now
    assert rig.fetch.inside == 4
    rig.fetch.release.set()
    results = [_joined(*worker) for worker in workers]
    assert [r.status for r in results] == [200] * 8
    assert rig.fetch.max_inside == 4
    assert sorted(rig.fetch.urls()) == sorted(MV + f"wrath/m2/{n}.m2" for n in range(8))


def test_serve_slot_wait_is_bounded_and_does_not_start_a_cooldown(tmp_path):
    rig = Rig(tmp_path, GateFetch(), max_parallel=1, slot_timeout_s=0.2)
    holder = _in_thread(rig.proxy.serve, "wrath/m2/1.m2")
    assert rig.fetch.entered.acquire(timeout=WAIT)
    started = time.monotonic()
    waited = rig.proxy.serve("wrath/m2/2.m2")
    assert waited.status == 503
    assert time.monotonic() - started < WAIT
    assert rig.fetch.urls() == [MV + "wrath/m2/1.m2"]  # it never got a slot
    assert rig.proxy.status().unreachable_since is None  # nobody failed upstream
    rig.fetch.release.set()
    assert _joined(*holder).status == 200
    assert rig.proxy.serve("wrath/m2/2.m2").status == 200  # no cool-down to wait out


@pytest.mark.parametrize(
    "failure",
    [_status(429), _status(500), _status(503), _status(403), _status(304), UpstreamError("timeout")],
    ids=["429", "500", "503", "403", "unasked-304", "upstream-error"],
)
def test_serve_upstream_error_starts_cooldown_and_misses_return_503_until_it_ends(tmp_path, failure):
    rig = Rig(tmp_path, FakeFetch({M2_URL: failure}))
    rig.proxy.cache.put("wrath/m2/7.m2", b"cached")

    failed = rig.proxy.serve(M2)
    assert failed.status == 503 and failed.path is None and failed.body is None
    assert failed.cache_control == "no-store"
    assert failed.reason
    assert not rig.file(M2).exists()  # nothing but a 200 is ever stored
    assert rig.proxy.cache.negative(M2) is False
    assert rig.fetch.urls() == [M2_URL]  # never retried inside the request

    # During the cool-down every miss to that host answers 503 at once; hits still serve.
    for _ in range(3):
        assert rig.proxy.serve(SKIN).status == 503
        assert rig.proxy.serve(ICON).status == 503
        assert rig.proxy.serve("wrath/m2/7.m2").status == 200
    rig.mono.now += 59.9
    assert rig.proxy.serve(SKIN).status == 503
    assert len(rig.fetch.calls) == 1

    rig.mono.now += 0.1  # 60 s after the failure
    assert rig.proxy.serve(SKIN).status == 200
    assert rig.fetch.urls() == [M2_URL, MV + SKIN]
    assert rig.proxy.status().unreachable_since is None  # the success cleared the host


def test_cooldown_is_per_host_and_status_reports_the_earliest_active_one(tmp_path):
    xml_url = item_xml_url(6125)
    rig = Rig(tmp_path, FakeFetch({M2_URL: _status(500), xml_url: _ok(b"<wowhead/>")}))
    start = rig.wall.now
    assert rig.proxy.serve(M2).status == 503  # wow.zamimg.com cools down for 60 s
    rig.mono.now += 30
    rig.wall.now += 30
    # The resolver's XML lookup goes through the same guard; another host is not cooling.
    assert rig.proxy.fetch_item_xml(6125).status == 200
    rig.fetch.answers[xml_url] = UpstreamError("connection failed")
    assert rig.proxy.fetch_item_xml(6125) is None  # www.wowhead.com cools down until 30 s after zamimg
    assert rig.proxy.status().unreachable_since == _iso(start)  # both active: the earliest
    rig.mono.now += 31  # zamimg's cool-down is over, wowhead's is not
    assert rig.proxy.status().unreachable_since == _iso(start + 30)
    rig.fetch.answers[M2_URL] = _ok(b"back")
    assert rig.proxy.serve(M2).status == 200
    assert rig.proxy.status().unreachable_since == _iso(start + 30)
    rig.mono.now += 30  # wowhead's is over too: nothing is reported (R16b)
    assert rig.proxy.status().unreachable_since is None


def test_fetch_item_xml_goes_through_the_guard_and_is_never_cached(tmp_path):
    xml_url = "https://www.wowhead.com/wotlk/item=6125&xml"
    body = b'<wowhead><item id="6125"><icon displayId="9182">inv_shirt_06</icon></item></wowhead>'
    rig = Rig(tmp_path, FakeFetch({xml_url: _ok(body)}))

    found = rig.proxy.fetch_item_xml(6125)
    assert found == FetchResult(200, body, None, None)
    assert rig.proxy.fetch_item_xml(6125).status == 200  # never cached: asked again
    assert rig.fetch.calls == [(xml_url, None), (xml_url, None)]
    assert not rig.data_dir.exists()  # nothing stored, not even a negative entry
    rig.fetch.answers[xml_url] = _status(404)
    assert rig.proxy.fetch_item_xml(6125) == FetchResult(404, b"", None, None)  # an answer, not an outage
    assert not rig.data_dir.exists()
    assert rig.proxy.serve("item-displayid/6125").status == 404  # no asset path reaches it
    assert len(rig.fetch.calls) == 3

    with pytest.raises(ValueError):
        rig.proxy.fetch_item_xml(0)
    with pytest.raises(ValueError):
        rig.proxy.fetch_item_xml("6125")  # type: ignore[arg-type]
    assert len(rig.fetch.calls) == 3

    # Switched off: not attempted.
    rig.proxy.settings.set_enabled(False)
    assert rig.proxy.fetch_item_xml(6125) is None
    rig.proxy.settings.set_enabled(True)
    assert len(rig.fetch.calls) == 3

    # 429/5xx and network errors: None, and www.wowhead.com cools down (zamimg does not).
    for failure in (_status(429), _status(503), UpstreamError("timeout")):
        rig.fetch.answers[xml_url] = failure
        assert rig.proxy.fetch_item_xml(6125) is None
        assert rig.proxy.status().unreachable_since is not None
        calls = len(rig.fetch.calls)
        assert rig.proxy.fetch_item_xml(6125) is None  # cooling down: not attempted
        assert len(rig.fetch.calls) == calls
        assert rig.proxy.serve(M2).status == 200  # the model CDN is another host
        rig.mono.now += 61
        rig.proxy.cache.remove(M2)
    rig.fetch.answers[xml_url] = _ok(body)
    assert rig.proxy.fetch_item_xml(6125).status == 200
    assert rig.proxy.status().unreachable_since is None


def test_fetch_item_xml_shares_the_slots_and_gives_up_without_a_cooldown(tmp_path):
    rig = Rig(tmp_path, GateFetch(), max_parallel=1, slot_timeout_s=0.2)
    holder = _in_thread(rig.proxy.serve, M2)
    assert rig.fetch.entered.acquire(timeout=WAIT)
    assert rig.proxy.fetch_item_xml(6125) is None  # no free slot within the wait budget
    assert rig.fetch.urls() == [M2_URL]
    assert rig.proxy.status().unreachable_since is None
    rig.fetch.release.set()
    assert _joined(*holder).status == 200
    assert rig.proxy.fetch_item_xml(6125).status == 200


def test_serve_disabled_returns_503_for_misses_only(tmp_path):
    rig = Rig(tmp_path)
    rig.proxy.cache.put("wrath/m2/7.m2", b"cached")
    rig.proxy.cache.mark_negative("wrath/m2/9.m2")
    rig.proxy.settings.set_enabled(False)

    assert rig.proxy.serve("wrath/m2/7.m2").status == 200
    assert rig.proxy.serve("wrath/m2/9.m2").status == 404  # a cached answer, no upstream needed
    off = rig.proxy.serve(M2)
    assert off.status == 503 and "switched off" in off.reason
    assert rig.proxy.serve(VIEWER).status == 503  # not pinned yet, so it is a miss too
    assert rig.fetch.calls == []
    assert rig.proxy.status().unreachable_since is None  # switched off is not an outage

    rig.proxy.settings.set_enabled(True)
    assert rig.proxy.serve(M2).status == 200
    assert rig.fetch.urls() == [M2_URL]


def test_serve_404_negative_cached_for_an_hour(tmp_path):
    rig = Rig(tmp_path, FakeFetch({M2_URL: _status(404)}))
    missing = rig.proxy.serve(M2)
    assert missing.status == 404 and missing.path is None and missing.body is None
    assert rig.proxy.cache.negative(M2) is True
    rig.wall.now += 3599
    assert rig.proxy.serve(M2).status == 404
    assert len(rig.fetch.calls) == 1
    rig.wall.now += 1  # the hour is over: asked again once
    assert rig.proxy.serve(M2).status == 404
    assert len(rig.fetch.calls) == 2
    # A 404 is an answer, not an outage: no cool-down, other misses still fetch.
    assert rig.proxy.status().unreachable_since is None
    assert rig.proxy.serve(SKIN).status == 200


def test_viewer_first_use_fetches_and_pins_then_never_refetches_in_serve(tmp_path):
    v1 = b"/* viewer v1 */"
    lm = "Wed, 29 Oct 2026 10:00:00 GMT"
    rig = Rig(tmp_path, FakeFetch({VIEWER_URL: _ok(v1, etag='"v1"', last_modified=lm)}))
    pinned = rig.proxy.serve(VIEWER)
    assert pinned == Served(200, rig.file(VIEWER), "application/javascript", "no-cache", f'"{_sha(v1)}"')
    assert rig.file(VIEWER).read_bytes() == v1
    meta_path = rig.file("live/viewer/viewer.meta.json")
    assert json.loads(meta_path.read_text()) == {
        "etag": '"v1"',
        "last_modified": lm,
        "fetched_at": _iso(rig.wall.now),
        "sha256": _sha(v1),
        "size": len(v1),
    }
    assert _mode(rig.file(VIEWER)) == 0o600 and _mode(meta_path) == 0o600

    # Upstream changes; serve keeps the pinned copy whatever happens.
    rig.fetch.answers[VIEWER_URL] = _ok(b"/* viewer v2 */", etag='"v2"')
    for _ in range(3):
        rig.wall.now += 10 * 86400
        assert rig.proxy.serve(VIEWER) == pinned
    rig.proxy.clear_cache()
    assert rig.proxy.serve(VIEWER) == pinned
    rig.proxy.settings.set_enabled(False)
    assert rig.proxy.serve(VIEWER) == pinned  # a hit: still served while switched off
    assert rig.fetch.calls == [(VIEWER_URL, None)]


def test_update_viewer_keeps_prev_and_revert_restores_exact_bytes(tmp_path):
    v1, v2 = b"/* viewer v1 */", b"/* viewer v2, a little longer */"
    rig = Rig(tmp_path, FakeFetch({VIEWER_URL: _ok(v1, etag='"v1"', last_modified="Mon, 1 Jan 2026 00:00:00 GMT")}))
    first_fetch = rig.wall.now
    rig.proxy.serve(VIEWER)
    meta_path = rig.file("live/viewer/viewer.meta.json")
    meta_v1 = meta_path.read_bytes()

    rig.fetch.answers[VIEWER_URL] = _ok(v2, etag='"v2"', last_modified="Thu, 29 Oct 2026 00:00:00 GMT")
    rig.wall.now += 86400
    status = rig.proxy.update_viewer()
    assert rig.fetch.calls[-1] == (VIEWER_URL, '"v1"')  # conditional GET with the pinned etag
    assert status == ViewerStatus(fetched_at=_iso(rig.wall.now), etag='"v2"', size=len(v2), has_prev=True)
    assert rig.proxy.viewer_status() == status
    assert rig.file(VIEWER).read_bytes() == v2
    assert rig.file("live/viewer/viewer.min.js.prev").read_bytes() == v1
    assert rig.file("live/viewer/viewer.meta.json.prev").read_bytes() == meta_v1
    assert json.loads(meta_path.read_text())["sha256"] == _sha(v2)
    assert rig.proxy.serve(VIEWER).etag == f'"{_sha(v2)}"'

    reverted = rig.proxy.revert_viewer()
    assert reverted == ViewerStatus(fetched_at=_iso(first_fetch), etag='"v1"', size=len(v1), has_prev=False)
    assert rig.file(VIEWER).read_bytes() == v1
    assert meta_path.read_bytes() == meta_v1  # the exact previous metadata too
    assert not rig.file("live/viewer/viewer.min.js.prev").exists()
    assert not rig.file("live/viewer/viewer.meta.json.prev").exists()
    assert rig.proxy.serve(VIEWER).etag == f'"{_sha(v1)}"'
    assert rig.proxy.status().cache_bytes == _tree_bytes(rig.root)  # the running total followed
    with pytest.raises(ViewerError, match="no previous"):
        rig.proxy.revert_viewer()
    assert len(rig.fetch.calls) == 2  # revert never fetches


def test_update_viewer_304_changes_nothing(tmp_path):
    v1 = b"/* viewer v1 */"
    rig = Rig(tmp_path, FakeFetch({VIEWER_URL: _ok(v1, etag='"v1"')}))
    # Nothing pinned yet: Update is an unconditional GET that pins without a .prev.
    first = rig.proxy.update_viewer()
    assert rig.fetch.calls == [(VIEWER_URL, None)]
    assert first == ViewerStatus(fetched_at=_iso(rig.wall.now), etag='"v1"', size=len(v1), has_prev=False)

    def snapshot():
        return {
            p.name: (p.read_bytes(), os.lstat(p).st_mtime_ns, os.lstat(p).st_ino)
            for p in rig.file("live/viewer").iterdir()
        }

    before = snapshot()
    rig.fetch.answers[VIEWER_URL] = _status(304)
    rig.wall.now += 3600
    assert rig.proxy.update_viewer() == first
    assert rig.fetch.calls[-1] == (VIEWER_URL, '"v1"')
    assert snapshot() == before

    # Upstream answers 200 with the very same bytes: only the validators are refreshed, .prev is not
    # overwritten with an identical copy.
    rig.fetch.answers[VIEWER_URL] = _ok(v1, etag='"v1-gz"')
    same = rig.proxy.update_viewer()
    assert same == ViewerStatus(fetched_at=_iso(rig.wall.now), etag='"v1-gz"', size=len(v1), has_prev=False)
    assert rig.file(VIEWER).read_bytes() == v1

    # A failed update keeps the pinned copy and starts the host's cool-down.
    before = snapshot()
    rig.fetch.answers[VIEWER_URL] = _status(500)
    with pytest.raises(ViewerError, match="pinned copy is kept"):
        rig.proxy.update_viewer()
    assert snapshot() == before
    assert rig.proxy.status().unreachable_since is not None
    calls = len(rig.fetch.calls)
    with pytest.raises(ViewerError, match="unreachable"):
        rig.proxy.update_viewer()  # in the cool-down: refused without a fetch
    assert len(rig.fetch.calls) == calls

    # R18: switched off, Update refuses and nothing goes upstream.
    rig.mono.now += 61
    rig.proxy.settings.set_enabled(False)
    with pytest.raises(ViewerError, match="switched off"):
        rig.proxy.update_viewer()
    assert len(rig.fetch.calls) == calls
    assert snapshot() == before


def test_clear_cache_keeps_pinned_viewer(tmp_path):
    v1, v2 = b"/* v1 */", b"/* v2 */"
    rig = Rig(tmp_path, FakeFetch({VIEWER_URL: _ok(v1, etag='"v1"'), MV + "wrath/m2/9.m2": _status(404)}))
    rig.proxy.serve(VIEWER)
    rig.fetch.answers[VIEWER_URL] = _ok(v2, etag='"v2"')
    rig.proxy.update_viewer()
    rig.proxy.serve(M2)
    rig.proxy.serve(ICON)
    rig.proxy.serve("wrath/m2/9.m2")
    viewer_files = {
        name: rig.file(f"live/viewer/{name}").read_bytes()
        for name in ("viewer.min.js", "viewer.min.js.prev", "viewer.meta.json", "viewer.meta.json.prev")
    }

    assert rig.proxy.clear_cache() == 3  # the model, the icon and the negative marker
    assert {p.name: p.read_bytes() for p in rig.file("live/viewer").iterdir()} == viewer_files
    assert not rig.file(M2).exists() and not rig.file(ICON).exists()
    assert rig.proxy.cache.negative("wrath/m2/9.m2") is False
    assert rig.proxy.status().cache_bytes == sum(len(b) for b in viewer_files.values())
    assert rig.proxy.viewer_status().has_prev is True  # Revert still offered
    calls = len(rig.fetch.calls)
    assert rig.proxy.serve(VIEWER).etag == f'"{_sha(v2)}"'
    assert len(rig.fetch.calls) == calls


def test_status_reports_cache_size_viewer_and_unreachable_since(tmp_path):
    rig = Rig(tmp_path, FakeFetch({VIEWER_URL: _ok(b"/* v1 */", etag='"v1"'), MV + SKIN: _status(502)}))
    assert rig.proxy.status() == ProxyStatus(
        enabled=True,
        cache_bytes=0,
        viewer=ViewerStatus(fetched_at=None, etag=None, size=0, has_prev=False),
        unreachable_since=None,
        cache_writable=True,
        pass_through=False,
        cache_error=None,
    )
    assert not rig.data_dir.exists()  # status never creates anything

    pinned_at = rig.wall.now
    rig.proxy.serve(VIEWER)
    rig.proxy.serve(M2)
    status = rig.proxy.status()
    assert status.cache_bytes == _tree_bytes(rig.root) > 0
    assert status.viewer == ViewerStatus(fetched_at=_iso(pinned_at), etag='"v1"', size=8, has_prev=False)

    rig.wall.now += 100
    failed_at = rig.wall.now
    assert rig.proxy.serve(SKIN).status == 503
    assert rig.proxy.status().unreachable_since == _iso(failed_at)
    # R16b: once the cool-down has passed it is no longer reported.
    rig.mono.now += 60
    rig.wall.now += 60
    assert rig.proxy.status().unreachable_since is None
    # A re-arm during a sustained outage (the next miss fails again) keeps the original since.
    rig.mono.now += 30
    rig.wall.now += 30
    assert rig.proxy.serve(SKIN).status == 503
    assert rig.proxy.status().unreachable_since == _iso(failed_at)
    rig.mono.now += 60
    rig.fetch.answers[MV + SKIN] = _ok(b"skin")
    assert rig.proxy.serve(SKIN).status == 200  # a success ends the outage
    assert rig.proxy.status().unreachable_since is None

    # One failure, then a quiet hour: the next failure is a new outage with its own since.
    blip = "wrath/skin/6.skin"
    rig.fetch.answers[MV + blip] = _status(502)
    assert rig.proxy.serve(blip).status == 503
    rig.mono.now += 3600
    rig.wall.now += 3600
    assert rig.proxy.status().unreachable_since is None
    assert rig.proxy.serve(blip).status == 503
    assert rig.proxy.status().unreachable_since == _iso(rig.wall.now)

    rig.proxy.settings.set_enabled(False)
    assert rig.proxy.status().enabled is False


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_unwritable_cache_dir_serves_pass_through_and_flags_it(tmp_path):
    rig = Rig(tmp_path, FakeFetch({M2_URL: _ok(b"m2 bytes"), MV + "wrath/m2/9.m2": _status(404)}))
    rig.root.mkdir(parents=True)
    os.chmod(rig.root, 0o500)
    try:
        served = rig.proxy.serve(M2)
        assert served.status == 200 and served.path is None and served.body == b"m2 bytes"
        assert served.cache_control == IMMUTABLE and served.content_type == "application/octet-stream"
        status = rig.proxy.status()
        assert status.pass_through is True and status.cache_writable is False
        assert "writable" in status.cache_error
        assert rig.proxy.serve(M2).body == b"m2 bytes"
        assert len(rig.fetch.calls) == 2  # nothing stored, so fetched again

        assert rig.proxy.serve("wrath/m2/9.m2").status == 404  # the 404 still answers
        viewer = rig.proxy.serve(VIEWER)
        assert viewer.status == 200 and viewer.body is not None and viewer.cache_control == "no-cache"
        assert viewer.etag == f'"{_sha(viewer.body)}"'
        with pytest.raises(ViewerError, match="writable"):
            rig.proxy.update_viewer()
    finally:
        os.chmod(rig.root, 0o700)
    # Writable again: the next miss is stored and the flags clear.
    assert rig.proxy.serve(SKIN).path == rig.file(SKIN)
    status = rig.proxy.status()
    assert status.pass_through is False and status.cache_writable is True and status.cache_error is None


def test_cache_write_failure_switches_to_pass_through_and_recovers(tmp_path, monkeypatch):
    """The same pass-through path driven by a failing write, so it also runs where the suite runs as root."""
    rig = Rig(tmp_path, FakeFetch({M2_URL: _ok(b"m2 bytes")}))

    def full_disk(*_args, **_kwargs):
        raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))

    monkeypatch.setattr(rig.proxy.cache, "put", full_disk)
    monkeypatch.setattr(rig.proxy.cache, "mark_negative", full_disk)
    served = rig.proxy.serve(M2)
    assert served == Served(200, None, "application/octet-stream", IMMUTABLE, served.etag, b"m2 bytes")
    assert served.etag == rig.proxy.serve(M2).etag  # same identity as a stored copy would have
    status = rig.proxy.status()
    assert (status.pass_through, status.cache_writable) == (True, False)
    assert os.strerror(errno.ENOSPC) in status.cache_error
    rig.fetch.answers[MV + SKIN] = _status(404)
    assert rig.proxy.serve(SKIN).status == 404

    monkeypatch.undo()
    assert rig.proxy.serve(M2).path == rig.file(M2)
    assert rig.proxy.status().pass_through is False


def test_get_proxy_is_a_lazy_singleton_under_admin_data_dir(tmp_path, monkeypatch):
    admin_data = tmp_path / "admin-data"
    monkeypatch.setenv("ADMIN_DATA_DIR", str(admin_data))
    proxy = get_proxy()
    assert get_proxy() is proxy
    assert proxy.data_dir == admin_data / "armory"
    assert proxy.cache.root == admin_data / "armory" / "cache"
    assert proxy.settings.path == admin_data / "armory" / "armory.json"
    assert not admin_data.exists()  # building it touches neither the disk nor the network
    reset_proxy()
    assert get_proxy() is not proxy
