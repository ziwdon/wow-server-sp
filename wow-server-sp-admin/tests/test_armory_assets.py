"""Armory assets: the upstream allow-list, the locked-down fetcher and the disk cache.

No test here opens a network socket: ``urllib.request.build_opener`` is replaced by a
fake opener that records the request and answers from canned data, and an autouse
fixture makes any real connection attempt fail the test.
"""

from __future__ import annotations

import email.message
import http.client
import io
import os
import socket
import stat
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from app.services import armory_assets
from app.services.armory_assets import (
    AssetCache,
    FetchResult,
    Upstream,
    UpstreamError,
    fetch_upstream,
    item_xml_url,
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
