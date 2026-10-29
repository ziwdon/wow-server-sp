"""Armory assets: the only code in the admin app that contacts the internet.

The 3D viewer's files (Wowhead's viewer script, model metadata, ``.m2``/``.skin``
models, textures) and the item icons come from Wowhead's static asset CDN; the
browser never talks to it, the admin fetches each file once and serves it from a
local disk cache. Three pieces live here, modelled on ``ProtonDriveCli``'s
discipline (allow-list first, everything built from scratch, fail closed):

* ``resolve_upstream`` — the exact allow-list (spec 8). A local path either matches
  one of the fixed shapes, with numeric ids and a lowercase icon basename, and maps
  to one fixed upstream URL, or it is ``None``. No query string, no other host, no
  other path can be expressed. ``item_xml_url`` builds the one other URL (the
  display-ID fallback) and is not reachable from ``resolve_upstream``.
* ``fetch_upstream`` — one GET through an opener built from scratch: no environment
  proxy, no cookie jar, redirects refused, fixed headers only (no Referer, Origin or
  Cookie), a 16 MB cap, a 20 s budget. It refuses any URL the allow-list could not
  have produced before anything is opened. Network failures raise ``UpstreamError``
  with a short generic reason; HTTP statuses (404, 304, 429, 5xx …) are returned.
* ``AssetCache`` — plain private files under one root (0600 files, 0700 dirs,
  tmp + rename), a 1 h negative cache under ``.neg/``, and least-recently-hit
  eviction to 80 % of the cap that never touches the pinned viewer script
  (``live/viewer/``). Content types come from our own extension table, never from
  upstream.

Task 7 builds the ``AssetProxy`` (single-flight, concurrency limit, cool-down,
viewer pinning, on/off switch) on top of these.
"""

from __future__ import annotations

import contextlib
import http.client
import os
import re
import ssl
import stat
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

USER_AGENT = "wow-server-sp-admin/1.0"
DEFAULT_TIMEOUT_SECONDS = 20.0
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
CACHE_CAP_BYTES = 300 * 1024 * 1024
EVICT_TO_FRACTION = 0.8
NEGATIVE_TTL_SECONDS = 3600

ALLOWED_HOSTS = frozenset({"wow.zamimg.com", "www.wowhead.com"})
_MODELVIEWER_BASE = "https://wow.zamimg.com/modelviewer/"
_ICON_BASE = "https://wow.zamimg.com/images/wow/icons/large/"
_ITEM_XML_BASE = "https://www.wowhead.com/wotlk/item="

VIEWER_PATH = "live/viewer/viewer.min.js"
VIEWER_DIR = "live/viewer"  # everything under it is kept by eviction and clear()
_NEGATIVE_DIR = ".neg"

# Our own table; an upstream Content-Type is never passed on.
CONTENT_TYPES = {
    ".js": "application/javascript",
    ".json": "application/json",
    ".m2": "application/octet-stream",
    ".skin": "application/octet-stream",
    ".webp": "image/webp",
    ".jpg": "image/jpeg",
}

AssetKind = Literal["viewer", "meta", "model", "texture", "icon"]

_READ_CHUNK = 64 * 1024
_MAX_LOCAL_PATH_CHARS = 256
_CLOCK_SLACK_SECONDS = 5.0  # a negative marker a little "in the future" (clock step) still counts
_monotonic = time.monotonic  # the fetch budget's clock (a test seam)


# --- allow-list ---


@dataclass(frozen=True)
class Upstream:
    url: str
    content_type: str
    kind: AssetKind


_N = r"[0-9]{1,8}"
_MODEL_ID = r"(?:[1-9]|1[0-9]|2[0-2])"  # race * 2 - 1 + gender, races 1-11
_ARMOR_TYPE = r"(?:1|3|4|5|6|7|8|9|10|16|19|20)"  # the InventoryTypes the viewer asks armor meta for

# (pattern on the whole local path, kind); every one of them lives under modelviewer/.
_MODELVIEWER_RULES: tuple[tuple[re.Pattern[str], AssetKind], ...] = (
    (re.compile(r"live/viewer/viewer\.min\.js"), "viewer"),
    (re.compile(rf"wrath/meta/character/{_MODEL_ID}\.json"), "meta"),
    (re.compile(rf"wrath/meta/charactercustomization/{_MODEL_ID}\.json"), "meta"),
    (re.compile(rf"wrath/meta/armor/{_ARMOR_TYPE}/{_N}\.json"), "meta"),
    (re.compile(rf"wrath/meta/item/{_N}\.json"), "meta"),
    (re.compile(rf"wrath/meta/itemvisual/{_N}\.json"), "meta"),
    (re.compile(rf"wrath/m2/{_N}\.m2"), "model"),
    (re.compile(rf"wrath/skin/{_N}\.skin"), "model"),
    (re.compile(rf"wrath/textures/{_N}\.webp"), "texture"),
)
_ICON_RULE = re.compile(r"icons/([a-z0-9_-]{1,64})\.jpg")
_ITEM_XML_RULE = re.compile(r"https://www\.wowhead\.com/wotlk/item=[1-9][0-9]*&xml")

_FORBIDDEN_SUBSTRINGS = ("..", "//", "?", "#", "\\", "%")


def _printable_ascii(text: str) -> bool:
    """Only visible ASCII: no whitespace, control or non-ASCII characters."""
    return all("\x21" <= ch <= "\x7e" for ch in text)


def _content_type(path: str) -> str:
    return CONTENT_TYPES[path[path.rindex(".") :]]


def resolve_upstream(local_path: str) -> Upstream | None:
    """The upstream file for one allow-listed local path, or ``None`` for anything else."""
    if not isinstance(local_path, str) or not local_path or len(local_path) > _MAX_LOCAL_PATH_CHARS:
        return None
    if local_path.startswith("/") or not _printable_ascii(local_path):
        return None
    if any(bad in local_path for bad in _FORBIDDEN_SUBSTRINGS):
        return None
    for pattern, kind in _MODELVIEWER_RULES:
        if pattern.fullmatch(local_path):
            return Upstream(_MODELVIEWER_BASE + local_path, _content_type(local_path), kind)
    match = _ICON_RULE.fullmatch(local_path)
    if match:
        return Upstream(_ICON_BASE + match.group(1) + ".jpg", CONTENT_TYPES[".jpg"], "icon")
    return None


def item_xml_url(entry: int) -> str:
    """The Wowhead item lookup used by the display-ID fallback (spec 8.1); not a proxy path."""
    if not isinstance(entry, int) or isinstance(entry, bool) or entry <= 0:
        raise ValueError("item entry must be a positive integer")
    return f"{_ITEM_XML_BASE}{entry}&xml"


def upstream_url_allowed(url: str) -> bool:
    """True only for a URL that ``resolve_upstream`` or ``item_xml_url`` can produce."""
    if not isinstance(url, str) or not url or not _printable_ascii(url):
        return False
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.netloc not in ALLOWED_HOSTS or parts.query or parts.fragment:
        return False
    if _ITEM_XML_RULE.fullmatch(url):
        return True
    if url.startswith(_MODELVIEWER_BASE):
        upstream = resolve_upstream(url[len(_MODELVIEWER_BASE) :])
        return upstream is not None and upstream.kind != "icon" and upstream.url == url
    if url.startswith(_ICON_BASE):
        upstream = resolve_upstream("icons/" + url[len(_ICON_BASE) :])
        return upstream is not None and upstream.kind == "icon" and upstream.url == url
    return False


# --- fetcher ---


class UpstreamError(Exception):
    """A fetch that produced no HTTP answer: not allowed, redirect, too large, timeout, network failure.

    ``reason`` is short and generic (safe to log or show).
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class FetchResult:
    status: int
    body: bytes = field(repr=False)
    etag: str | None
    last_modified: str | None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: the 3xx then surfaces as an ``HTTPError``, which is an ``UpstreamError``."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _build_opener() -> urllib.request.OpenerDirector:
    """An opener with no environment proxy, no cookie jar, no redirects and no default headers."""
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _NoRedirect(),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()),
    )
    opener.addheaders = []  # never Python's own User-Agent
    return opener


def _header(headers, name: str) -> str | None:
    if headers is None:
        return None
    value = headers.get(name)
    return value if isinstance(value, str) else None


def _read_capped(response, max_bytes: int, deadline: float) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = response.read(min(_READ_CHUNK, max_bytes + 1 - total))
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        total += len(chunk)
        if total > max_bytes:
            raise UpstreamError("too large")
        if _monotonic() > deadline:
            raise UpstreamError("timeout")


def fetch_upstream(
    url: str,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    max_bytes: int = MAX_RESPONSE_BYTES,
    if_none_match: str | None = None,
) -> FetchResult:
    """One GET of an allow-listed URL. HTTP statuses are returned; everything else raises ``UpstreamError``.

    ``timeout`` bounds each socket operation and the whole body read. A 3xx other
    than 304 (a redirect), a body over ``max_bytes`` and a URL the allow-list does
    not produce are errors; the last is refused before anything is opened.
    """
    if not upstream_url_allowed(url):
        raise UpstreamError("not allowed")
    headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "identity", "Accept": "*/*"}
    if if_none_match:  # None or "" sends an unconditional GET
        # An etag is visible ASCII (spaces allowed); CR/LF could smuggle another header.
        if not isinstance(if_none_match, str) or not _printable_ascii(if_none_match.replace(" ", "")):
            raise UpstreamError("not allowed")
        headers["If-None-Match"] = if_none_match
    request = urllib.request.Request(url, headers=headers, method="GET")
    deadline = _monotonic() + timeout
    try:
        opener = _build_opener()
        with opener.open(request, timeout=timeout) as response:
            final_url = response.geturl()
            if final_url and final_url != url:
                raise UpstreamError("redirect")
            declared = _header(response.headers, "Content-Length")
            if declared and declared.isdigit() and int(declared) > max_bytes:
                raise UpstreamError("too large")
            body = _read_capped(response, max_bytes, deadline)
            status = getattr(response, "status", None) or response.getcode()
            return FetchResult(
                status, body, _header(response.headers, "ETag"), _header(response.headers, "Last-Modified")
            )
    except UpstreamError:
        raise
    except urllib.error.HTTPError as err:
        code, err_headers = err.code, err.headers
        with contextlib.suppress(Exception):
            err.close()
        if 300 <= code < 400 and code != 304:
            raise UpstreamError("redirect") from None
        return FetchResult(code, b"", _header(err_headers, "ETag"), _header(err_headers, "Last-Modified"))
    except urllib.error.URLError as err:
        reason = "timeout" if isinstance(err.reason, TimeoutError) else "connection failed"
        raise UpstreamError(reason) from None
    except TimeoutError:
        raise UpstreamError("timeout") from None
    except OSError:  # connection reset/refused, TLS failure, RemoteDisconnected
        raise UpstreamError("connection failed") from None
    except (http.client.HTTPException, ValueError):
        raise UpstreamError("bad response") from None


# --- disk cache ---


def _ns(seconds: float) -> int:
    return int(round(seconds * 1_000_000_000))


def _safe_parts(local_path: str) -> list[str]:
    """Split a relative cache path; refuse anything that is not plain ``a/b/c`` of visible ASCII."""
    if not isinstance(local_path, str) or not local_path or len(local_path) > _MAX_LOCAL_PATH_CHARS:
        raise ValueError("invalid cache path")
    if local_path.startswith("/") or "\\" in local_path or not _printable_ascii(local_path):
        raise ValueError("invalid cache path")
    parts = local_path.split("/")
    for part in parts:
        # No empty, "." or ".." segment; no hidden name (keeps .neg/ and temp files out of reach).
        if not part or part.startswith("."):
            raise ValueError("invalid cache path")
    return parts


class AssetCache:
    """Plain files under ``root``; ``live/viewer/`` (the pinned viewer script) is never evicted or cleared.

    Thread-safe for the proxy's worker threads: writes, eviction and ``clear`` hold
    one lock; a ``get`` racing an eviction is simply a miss.
    """

    def __init__(
        self,
        root: Path,
        cap_bytes: int = CACHE_CAP_BYTES,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.root = Path(root)
        self.cap_bytes = int(cap_bytes)
        self._clock = clock
        self._lock = threading.RLock()

    # paths

    def _contained(self, parts: list[str]) -> Path:
        root = self.root.resolve()
        candidate = root.joinpath(*parts)
        parent = candidate.parent.resolve()
        if parent != root and root not in parent.parents:
            raise ValueError("cache path leaves the cache directory")
        target = parent / candidate.name
        if target.is_symlink():
            raise ValueError("cache path is a symbolic link")
        return target

    def path_for(self, local_path: str) -> Path:
        """The file for ``local_path``; ``ValueError`` unless it stays inside ``root``."""
        return self._contained(_safe_parts(local_path))

    def _negative_path(self, local_path: str) -> Path:
        return self._contained([_NEGATIVE_DIR, *_safe_parts(local_path)])

    # writes

    def _make_private_dirs(self, directory: Path) -> None:
        root = self.root.resolve()
        if not root.exists():
            root.parent.mkdir(parents=True, exist_ok=True)
        current = root
        for part in (None, *directory.relative_to(root).parts):
            if part is not None:
                current = current / part
            try:
                os.mkdir(current, 0o700)
            except FileExistsError:
                continue
            os.chmod(current, 0o700)  # mkdir's mode is filtered by the umask

    def _write_file(self, target: Path, body: bytes) -> None:
        self._make_private_dirs(target.parent)
        fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                os.fchmod(fh.fileno(), 0o600)
                fh.write(body)
                fh.flush()
                os.fsync(fh.fileno())  # an immutable cache must never keep a torn file after a crash
            os.replace(tmp, target)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
        self._touch(target)

    def _touch(self, path: Path) -> None:
        now = _ns(self._clock())
        os.utime(path, ns=(now, now))

    @staticmethod
    def _discard(path: Path) -> None:
        with contextlib.suppress(FileNotFoundError, NotADirectoryError):
            os.unlink(path)

    def put(self, local_path: str, body: bytes) -> Path:
        """Store ``body`` (0600, tmp + rename), drop any negative entry, then evict if over the cap."""
        target = self.path_for(local_path)
        negative = self._negative_path(local_path)
        with self._lock:
            self._write_file(target, bytes(body))
            self._discard(negative)
            self._evict(keep=target)
        return target

    def mark_negative(self, local_path: str) -> None:
        """Remember that upstream has no such file (valid for ``NEGATIVE_TTL_SECONDS``)."""
        marker = self._negative_path(local_path)
        with self._lock:
            self._write_file(marker, b"")

    # reads

    def get(self, local_path: str) -> Path | None:
        """The cached file (its mtime touched: a hit), or ``None``."""
        path = self.path_for(local_path)
        try:
            st = os.lstat(path)
        except OSError:
            return None
        if not stat.S_ISREG(st.st_mode):
            return None
        try:
            self._touch(path)
        except FileNotFoundError:
            return None  # evicted a moment ago
        except OSError:
            pass  # a cache that cannot be touched still serves
        return path

    def negative(self, local_path: str) -> bool:
        """True while a negative entry is younger than ``NEGATIVE_TTL_SECONDS``; an expired one is removed."""
        marker = self._negative_path(local_path)
        try:
            st = os.lstat(marker)
        except OSError:
            return False
        if not stat.S_ISREG(st.st_mode):
            return False
        age = self._clock() - st.st_mtime_ns / 1_000_000_000
        if -_CLOCK_SLACK_SECONDS <= age < NEGATIVE_TTL_SECONDS:
            return True
        with contextlib.suppress(OSError):
            self._discard(marker)
        return False

    # size, eviction, clear

    def _files(self) -> Iterator[tuple[str, Path, os.stat_result]]:
        """(relative posix path, path, lstat) of every regular file; symlinks are never followed."""
        root = self.root.resolve()
        if not root.is_dir():
            return
        for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
            for name in filenames:
                path = Path(dirpath) / name
                try:
                    st = os.lstat(path)
                except OSError:
                    continue
                if stat.S_ISREG(st.st_mode):
                    yield path.relative_to(root).as_posix(), path, st

    def size_bytes(self) -> int:
        return sum(st.st_size for _rel, _path, st in self._files())

    @staticmethod
    def _kept(rel: str) -> bool:
        return rel.startswith(VIEWER_DIR + "/")

    def _evict(self, *, keep: Path | None) -> int:
        files = list(self._files())
        total = sum(st.st_size for _rel, _path, st in files)
        if total <= self.cap_bytes:
            return 0
        goal = int(self.cap_bytes * EVICT_TO_FRACTION)
        candidates = sorted(
            (st.st_mtime_ns, rel, path, st.st_size)
            for rel, path, st in files
            # Negative markers are empty and expire on their own; evicting them would only forget 404s.
            if not self._kept(rel) and not rel.startswith(_NEGATIVE_DIR + "/") and path != keep
        )
        removed = 0
        for _mtime, _rel, path, size in candidates:
            if total <= goal:
                break
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
            except OSError:
                continue
            total -= size
            removed += 1
        return removed

    def evict_if_needed(self) -> int:
        """Over the cap: delete least-recently-hit files until at most 80 % of it. Returns files removed."""
        with self._lock:
            return self._evict(keep=None)

    def clear(self) -> int:
        """Delete every cached asset, negative entry and stray temp file; keep ``live/viewer/``.

        Best effort: a file that cannot be removed is left and not counted. Returns files removed.
        """
        with self._lock:
            root = self.root.resolve()
            if not root.is_dir():
                return 0
            removed = 0
            for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
                here = Path(dirpath)
                rel = here.relative_to(root).as_posix()
                if rel == VIEWER_DIR or self._kept(rel):
                    dirnames[:] = []
                    continue
                links = [name for name in dirnames if (here / name).is_symlink()]
                for name in links:  # never descended into; the link itself goes
                    dirnames.remove(name)
                for name in (*filenames, *links):
                    with contextlib.suppress(OSError):
                        os.unlink(here / name)
                        removed += 1
            keep_dirs = {root, root / "live", root / VIEWER_DIR}
            for dirpath, _dirnames, _filenames in os.walk(root, topdown=False, followlinks=False):
                here = Path(dirpath)
                if here in keep_dirs or self._kept(here.relative_to(root).as_posix()):
                    continue
                with contextlib.suppress(OSError):
                    os.rmdir(here)  # only empty directories go
            return removed
