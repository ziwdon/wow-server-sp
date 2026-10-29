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

* ``AssetProxy`` — what the ``/armory/assets/…`` route calls (``serve``): allow-list,
  cache hit, negative cache, then one guarded upstream GET (the On/Off switch in
  ``ArmorySettings``, a 60 s per-host cool-down after a failure, at most 4 fetches at
  once with a bounded wait for a slot, single-flight per path, never a retry). It
  pins the viewer script (fetched once, then only by ``update_viewer``, with
  ``revert_viewer`` to the previous copy) and falls back to pass-through when the
  cache cannot be written. ``fetch_item_xml`` is the display-ID resolver's one Wowhead
  item lookup (``armory_resolver``), under the same guard and never cached.
  ``get_proxy()`` is the one long-lived instance.
"""

from __future__ import annotations

import contextlib
import hashlib
import http.client
import json
import logging
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
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

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


def _make_private_dir(path: Path) -> None:
    """Create ``path`` with mode 0700, and any missing parent the same way; an existing directory is left as it is.

    The one rule for every directory the armory creates (``armory/``, the cache tree):
    private to the admin's UID, like ``cloud/``.
    """
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        return
    except FileNotFoundError:
        if path.parent == path:
            raise
        _make_private_dir(path.parent)
        try:
            os.mkdir(path, 0o700)
        except FileExistsError:  # another thread made it meanwhile
            return
    os.chmod(path, 0o700)  # mkdir's mode is filtered by the umask


def _write_private_file(path: Path, data: bytes) -> None:
    """``data`` into ``path`` (0600) through a temp file, fsync and a rename; a failed write leaves no temp file.

    The only file-writing code of the armory (cache files, viewer files, ``armory.json``).
    The fsync is there because an immutable cache must never keep a torn file after a crash.
    """
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            os.fchmod(fh.fileno(), 0o600)
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


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

    The byte total is kept running under that lock (learnt with one walk the first
    time it is needed, and again after ``clear``), so a write below the cap never
    walks the tree. Only a total over the cap walks it: that walk re-counts the tree
    (correcting any drift, e.g. a file changed by hand) before evicting, and each
    eviction frees at least a fifth of the cap, so walks stay rare.
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
        self._total: int | None = None  # running byte total of the tree; None = not known yet

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

    @staticmethod
    def _regular_size(path: Path) -> int:
        try:
            st = os.lstat(path)
        except OSError:
            return 0
        return st.st_size if stat.S_ISREG(st.st_mode) else 0

    def _adjust_total(self, delta: int) -> None:
        """Caller holds the lock. An unknown total stays unknown (it is learnt by the next walk)."""
        if self._total is not None:
            self._total = max(0, self._total + delta)

    def _write_file(self, target: Path, body: bytes) -> None:
        """Caller holds the lock. A failed write changes neither the file nor the total."""
        _make_private_dir(target.parent)
        replaced = self._regular_size(target)
        _write_private_file(target, body)
        self._adjust_total(len(body) - replaced)
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
            marker_size = self._regular_size(negative)  # 0 for our own (empty) markers
            self._discard(negative)
            self._adjust_total(-marker_size)
            self._evict(keep=target)
        return target

    def mark_negative(self, local_path: str) -> None:
        """Remember that upstream has no such file (valid for ``NEGATIVE_TTL_SECONDS``)."""
        marker = self._negative_path(local_path)
        with self._lock:
            self._write_file(marker, b"")

    def remove(self, local_path: str) -> bool:
        """Delete one cached file (the running total follows). False when there was none."""
        target = self.path_for(local_path)
        with self._lock:
            size = self._regular_size(target)
            try:
                os.unlink(target)
            except (FileNotFoundError, NotADirectoryError):
                return False
            self._adjust_total(-size)
            return True

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
        # Markers are empty, so expiring one leaves the running total as it is (no lock needed).
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
        """Bytes of every regular file under ``root`` (the running total; one walk if not known yet)."""
        with self._lock:
            if self._total is None:
                self._total = sum(st.st_size for _rel, _path, st in self._files())
            return self._total

    @staticmethod
    def _kept(rel: str) -> bool:
        return rel.startswith(VIEWER_DIR + "/")

    def _evict(self, *, keep: Path | None) -> int:
        """Caller holds the lock. Walks the tree only when the total is unknown or over the cap."""
        if self._total is not None and self._total <= self.cap_bytes:
            return 0
        files = list(self._files())
        total = sum(st.st_size for _rel, _path, st in files)
        self._total = total  # re-counted: any drift is corrected here
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
        self._total = total
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
            self._total = None  # what is left (live/viewer/, stubborn files) is re-counted when next needed
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


# --- proxy ---

IMMUTABLE_CACHE_CONTROL = "public, max-age=31536000, immutable"
VIEWER_CACHE_CONTROL = "no-cache"  # pinned, yet Update/Revert can replace it: browsers revalidate
ERROR_CACHE_CONTROL = "no-store"
ERROR_CONTENT_TYPE = "text/plain; charset=utf-8"
MAX_PARALLEL_FETCHES = 4
COOLDOWN_SECONDS = 60.0
SLOT_WAIT_MARGIN_SECONDS = 5.0  # R17: waiting for a slot gives up after the fetch timeout plus this
SETTINGS_FILE = "armory.json"
VIEWER_META_PATH = VIEWER_DIR + "/viewer.meta.json"
VIEWER_PREV_PATH = VIEWER_DIR + "/viewer.min.js.prev"
VIEWER_PREV_META_PATH = VIEWER_DIR + "/viewer.meta.json.prev"

_VIEWER_UPSTREAM = resolve_upstream(VIEWER_PATH)
if _VIEWER_UPSTREAM is None or _VIEWER_UPSTREAM.kind != "viewer":  # pragma: no cover - fixed by the allow-list
    raise RuntimeError("the viewer script must be allow-listed")
_MAX_META_TEXT = 256


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_bytes(data: dict) -> bytes:
    return (json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8")


class ArmorySettings:
    """``armory.json``: the 3D viewer's On/Off switch (spec 9). A missing or unreadable file means On."""

    def __init__(self, data_dir: Path) -> None:
        self.dir = Path(data_dir)
        self.path = self.dir / SETTINGS_FILE
        self._warned = False

    def enabled(self) -> bool:
        """Read on every call (the file is tiny), so a switch takes effect at once."""
        try:
            raw = json.loads(self.path.read_bytes())
        except FileNotFoundError:
            return True
        except (OSError, ValueError):  # ValueError: bad JSON or bad UTF-8
            raw = None
        value = raw.get("enabled") if isinstance(raw, dict) else None
        if isinstance(value, bool):
            return value
        if not self._warned:
            self._warned = True
            log.warning("armory settings %s unreadable; the 3D viewer stays on", self.path)
        return True

    def set_enabled(self, enabled: bool) -> None:
        if not isinstance(enabled, bool):
            raise TypeError("enabled must be a bool")
        _make_private_dir(self.dir)
        _write_private_file(self.path, _json_bytes({"enabled": enabled}))


@dataclass(frozen=True)
class Served:
    """One answer of ``AssetProxy.serve``.

    A 200 has exactly one of ``path`` (the cached file) and ``body`` (pass-through
    bytes, when the cache cannot be written); the route sends whichever is set with
    ``content_type``, ``cache_control`` and ``etag``. A 404/503 has neither, and
    ``reason`` is a short plain-text line for its body.
    """

    status: int
    path: Path | None = None
    content_type: str = ERROR_CONTENT_TYPE
    cache_control: str = ERROR_CACHE_CONTROL
    etag: str | None = None
    body: bytes | None = field(default=None, repr=False)
    reason: str = ""


@dataclass(frozen=True)
class ViewerStatus:
    fetched_at: str | None  # ISO-8601 UTC from viewer.meta.json; None when nothing is pinned (or no meta)
    etag: str | None  # upstream's ETag of the pinned copy, sent back as If-None-Match by Update
    size: int  # bytes of the pinned script; 0 = not pinned
    has_prev: bool  # a previous copy is kept: Revert is possible


@dataclass(frozen=True)
class ProxyStatus:
    enabled: bool
    cache_bytes: int
    viewer: ViewerStatus
    unreachable_since: str | None  # ISO-8601 UTC: start of the earliest outage whose cool-down is active
    cache_writable: bool  # the last cache write worked (True before the first one)
    pass_through: bool  # fetched bytes are served without being stored (the same fact as not cache_writable)
    cache_error: str | None = None  # why, for the footer strip


class ViewerError(Exception):
    """An Update or Revert of the viewer script that did not happen; ``message`` is for the footer strip."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class _Unavailable(Exception):
    """No upstream answer for this call: switched off, host cooling down, no free slot, or upstream failed."""

    def __init__(self, kind: Literal["disabled", "cool-down", "busy", "upstream"], detail: str = "") -> None:
        super().__init__(kind)
        self.kind = kind
        self.detail = detail


_SERVE_REASONS = {
    "disabled": "3D viewer is switched off",
    "cool-down": "Wowhead unreachable; waiting a minute before asking again",
    "busy": "too many Wowhead requests at once; try again",
}
_VIEWER_REASONS = {
    "disabled": "The 3D viewer is switched off; switch it on to update the viewer script.",
    "cool-down": "Wowhead is unreachable right now; try the update again in a minute. The pinned copy is kept.",
    "busy": "Too many Wowhead requests at once; try the update again. The pinned copy is kept.",
}
_VIEWER_BUSY = "Another viewer script update is running."
_NOT_FOUND = Served(404, reason="not found")
_INTERNAL_ERROR = Served(503, reason="internal error")


def _unavailable(err: _Unavailable) -> Served:
    return Served(503, reason=_SERVE_REASONS.get(err.kind) or f"Wowhead unreachable ({err.detail})")


def _viewer_unavailable(err: _Unavailable) -> str:
    return _VIEWER_REASONS.get(err.kind) or (
        f"Could not fetch the viewer script from Wowhead ({err.detail}); the pinned copy is kept."
    )


def _cache_write_error(err: OSError) -> str:
    return f"Could not write to the model cache ({err.strerror or type(err).__name__})"


def _asset_etag(local_path: str, size: int) -> str:
    """Stable across hits: the path and the size, never the mtime (every hit touches it).

    The ids are content-stable (spec 8) and browsers hold these responses as immutable,
    so this only has to tell different files apart.
    """
    return f'"{size:x}-{_sha256(local_path.encode("ascii"))[:16]}"'


def _viewer_meta_json(body: bytes, *, etag: str | None, last_modified: str | None, fetched_at: str | None) -> bytes:
    return _json_bytes(
        {
            "etag": etag,
            "last_modified": last_modified,
            "fetched_at": fetched_at,
            "sha256": _sha256(body),
            "size": len(body),
        }
    )


def _meta_text(value: object) -> str | None:
    """A meta string fit to show and to send back as If-None-Match (visible ASCII and spaces), else None."""
    if isinstance(value, str) and 0 < len(value) <= _MAX_META_TEXT and _printable_ascii(value.replace(" ", "")):
        return value
    return None


@dataclass(frozen=True)
class _ViewerMeta:
    etag: str | None
    fetched_at: str | None


def _parse_viewer_meta(data: bytes) -> _ViewerMeta | None:
    try:
        raw = json.loads(data)
    except ValueError:
        return None
    if not isinstance(raw, dict):
        return None
    return _ViewerMeta(etag=_meta_text(raw.get("etag")), fetched_at=_meta_text(raw.get("fetched_at")))


@dataclass
class _Flight:
    """One upstream fetch in progress for a path; requests for the same path wait on ``done``."""

    done: threading.Event = field(default_factory=threading.Event)
    outcome: Served | None = None
    followers: int = 0


@dataclass(frozen=True)
class _HostState:
    until: float  # monotonic: no request goes to this host before then
    since: float  # wall clock: the first failure of the current outage


class AssetProxy:
    """The one door to Wowhead's CDN: allow-list, cache, then one guarded upstream GET (spec 8).

    Meant as one long-lived instance (``get_proxy()``): the cache's running byte total
    and the cool-down state live in it. Every method blocks; call them from a worker
    thread (``status()`` too, it takes the cache lock).

    Locks, in the only order they nest: ``_viewer_op`` (one Update/Revert at a time,
    never waited for) -> ``_viewer_lock`` (every write of the viewer files) -> the
    cache's own lock. ``_inflight_lock`` and ``_state_mu`` are leaves. Nothing is held
    while waiting for a slot or for upstream except ``_viewer_op`` during an Update.
    """

    def __init__(
        self,
        data_dir: Path,
        *,
        fetch: Callable[..., FetchResult] = fetch_upstream,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], float] = time.time,
        max_parallel: int = MAX_PARALLEL_FETCHES,
        cooldown_s: float = COOLDOWN_SECONDS,
        slot_timeout_s: float | None = None,
    ) -> None:
        if max_parallel < 1:
            raise ValueError("max_parallel must be at least 1")
        self.data_dir = Path(data_dir)
        self.cache = AssetCache(self.data_dir / "cache", clock=now)
        self.settings = ArmorySettings(self.data_dir)
        self._fetch = fetch
        self._clock = clock  # monotonic: cool-downs
        self._now = now  # wall clock: the times people see, and the negative cache's age
        self._cooldown_s = float(cooldown_s)
        if slot_timeout_s is None:
            slot_timeout_s = DEFAULT_TIMEOUT_SECONDS + SLOT_WAIT_MARGIN_SECONDS
        self._slot_timeout = float(slot_timeout_s)
        # A follower waits at most as long as its leader can take: the slot wait plus one fetch
        # (fetch_upstream's worst case is about its timeout plus one socket timeout).
        self._flight_timeout = self._slot_timeout + 2 * DEFAULT_TIMEOUT_SECONDS + SLOT_WAIT_MARGIN_SECONDS
        self._slots = threading.BoundedSemaphore(max_parallel)
        self._inflight: dict[str, _Flight] = {}
        self._inflight_lock = threading.Lock()
        self._state_mu = threading.Lock()  # _hosts, _cache_error, _viewer_digest_memo
        self._hosts: dict[str, _HostState] = {}
        self._cache_error: str | None = None
        self._viewer_digest_memo: tuple[tuple[int, int, int, int], str] | None = None
        self._viewer_lock = threading.Lock()
        self._viewer_op = threading.Lock()

    # serving

    def serve(self, local_path: str) -> Served:
        """Answer one ``/armory/assets/<local_path>`` request.

        Not allow-listed: 404. Cached: 200 (hits are served even while switched off or
        while Wowhead is down). Remembered 404: 404. Otherwise, unless switched off or
        the host is cooling down (503 at once), one fetch shared by every concurrent
        request for the path: 200 is stored and served, 404 remembered for an hour,
        anything else is a 503. The viewer script is fetched here only while nothing is
        pinned; afterwards only ``update_viewer`` fetches it.
        """
        upstream = resolve_upstream(local_path)
        if upstream is None:
            return _NOT_FOUND
        try:
            answer = self._cached_answer(local_path, upstream)
        except ValueError:  # a cache path that is a symlink or leaves the cache: never served, never fetched
            return _NOT_FOUND
        if answer is not None:
            return answer
        try:
            self._check_may_fetch(urlsplit(upstream.url).netloc)
        except _Unavailable as err:
            return _unavailable(err)
        with self._inflight_lock:
            flight = self._inflight.get(local_path)
            leading = flight is None
            if flight is None:
                # A fetch that finished a moment ago stored its answer before leaving the table.
                try:
                    answer = self._cached_answer(local_path, upstream)
                except ValueError:
                    return _NOT_FOUND
                if answer is not None:
                    return answer
                flight = self._inflight[local_path] = _Flight()
            else:
                flight.followers += 1
        if leading:
            return self._lead(local_path, upstream, flight)
        return self._follow(local_path, upstream, flight)

    def _cached_answer(self, local_path: str, upstream: Upstream) -> Served | None:
        """A hit, a remembered 404, or None for a miss. ``ValueError`` for an unusable cache path."""
        hit = self._viewer_hit() if upstream.kind == "viewer" else self._asset_hit(local_path, upstream)
        if hit is not None:
            return hit
        if self.cache.negative(local_path):
            return _NOT_FOUND
        return None

    def _asset_hit(self, local_path: str, upstream: Upstream) -> Served | None:
        path = self.cache.get(local_path)
        if path is None:
            return None
        try:
            size = os.lstat(path).st_size
        except OSError:
            return None  # evicted a moment ago
        return Served(200, path, upstream.content_type, IMMUTABLE_CACHE_CONTROL, _asset_etag(local_path, size))

    def _viewer_hit(self) -> Served | None:
        """The pinned viewer script, or None when nothing is pinned. Not touched: it is never evicted."""
        path = self.cache.path_for(VIEWER_PATH)
        try:
            st = os.lstat(path)
            if not stat.S_ISREG(st.st_mode):
                return None
            digest = self._viewer_digest(path, st)
        except (FileNotFoundError, NotADirectoryError):
            return None
        except OSError:
            # Pinned but unreadable: never silently replaced by whatever upstream serves today.
            return Served(503, reason="pinned viewer script unreadable")
        return Served(200, path, _VIEWER_UPSTREAM.content_type, VIEWER_CACHE_CONTROL, f'"{digest}"')

    def _viewer_digest(self, path: Path, st: os.stat_result) -> str:
        """sha256 of the pinned file (the value viewer.meta.json records), hashed once per file version.

        Taken from the file rather than from the meta, so a crash between writing the two
        can never pair one script with another's ETag.
        """
        key = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
        with self._state_mu:
            memo = self._viewer_digest_memo
        if memo is not None and memo[0] == key:
            return memo[1]
        digest = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(_READ_CHUNK), b""):
                digest.update(chunk)
        value = digest.hexdigest()
        with self._state_mu:
            self._viewer_digest_memo = (key, value)
        return value

    def _forget_viewer_digest(self) -> None:
        with self._state_mu:
            self._viewer_digest_memo = None

    def _lead(self, local_path: str, upstream: Upstream, flight: _Flight) -> Served:
        try:
            try:
                result = self._guarded_fetch(upstream.url)
            except _Unavailable as err:
                served = _unavailable(err)
            else:
                if result.status != 200:  # 404: the guard lets only 200 and 404 through here
                    served = self._remember_missing(local_path)
                elif upstream.kind == "viewer":
                    served = self._pin_first_use(result)
                else:
                    served = self._store(local_path, upstream, result.body)
            flight.outcome = served
            return served
        finally:
            if flight.outcome is None:  # an unexpected exception: followers must not wait for nothing
                flight.outcome = _INTERNAL_ERROR
            with self._inflight_lock:
                self._inflight.pop(local_path, None)
            flight.done.set()

    def _follow(self, local_path: str, upstream: Upstream, flight: _Flight) -> Served:
        """Take the leader's outcome without a second fetch; after a stored 200 re-read the cache."""
        if not flight.done.wait(self._flight_timeout):
            return _unavailable(_Unavailable("busy"))
        outcome = flight.outcome or _INTERNAL_ERROR
        if outcome.status != 200 or outcome.body is not None:  # a failure, or pass-through bytes
            return outcome
        try:
            again = self._cached_answer(local_path, upstream)
        except ValueError:
            again = None
        return again if again is not None else _unavailable(_Unavailable("busy"))

    # upstream

    def _check_may_fetch(self, host: str) -> None:
        if not self.settings.enabled():
            raise _Unavailable("disabled")
        with self._state_mu:
            state = self._hosts.get(host)
            cooling = state is not None and self._clock() < state.until
        if cooling:
            raise _Unavailable("cool-down")

    def _guarded_fetch(self, url: str, *, if_none_match: str | None = None) -> FetchResult:
        """One upstream GET under the On/Off switch, the host's cool-down and the shared slots.

        Returns a 200 or a 404 (and a 304 to a conditional request) and marks the host
        reachable. Otherwise raises ``_Unavailable`` and never retries: switched off,
        host cooling down, no free slot within the wait budget (R17: that starts no
        cool-down), or an upstream failure (``UpstreamError`` or any other status),
        which starts the host's cool-down (R16: per host). The display-ID resolver's
        item lookup comes through here as well (``fetch_item_xml``).
        """
        host = urlsplit(url).netloc
        self._check_may_fetch(host)
        if not self._slots.acquire(timeout=self._slot_timeout):
            raise _Unavailable("busy")
        try:
            self._check_may_fetch(host)  # the switch or a cool-down may have changed while waiting
            try:
                if if_none_match:
                    result = self._fetch(url, if_none_match=if_none_match)
                else:
                    result = self._fetch(url)
            except UpstreamError as err:
                self._host_failed(host)
                raise _Unavailable("upstream", err.reason) from None
        finally:
            self._slots.release()
        if result.status not in ((200, 304, 404) if if_none_match else (200, 404)):
            self._host_failed(host)
            raise _Unavailable("upstream", f"HTTP {result.status}")
        self._host_answered(host)
        return result

    def fetch_item_xml(self, entry: int) -> FetchResult | None:
        """Wowhead's item XML for ``entry`` (the display-ID resolver's fallback, spec 8.1), or None.

        One GET of ``item_xml_url(entry)`` under the same guard as an asset miss (the
        On/Off switch, www.wowhead.com's cool-down, the shared slots). The answer is
        returned, never cached, and no ``serve`` path reaches it. Returns the 200 or the
        404; None when nothing was asked (switched off, cooling down, no free slot) or
        upstream failed (network error, 429, 5xx …, which starts the host's cool-down).
        ``ValueError`` for an entry that is not a positive int, before anything is asked.
        """
        url = item_xml_url(entry)
        try:
            return self._guarded_fetch(url)
        except _Unavailable:
            return None

    def _host_failed(self, host: str) -> None:
        """(Re)start the host's cool-down.

        A failure during the cool-down, or within one cool-down after it ended (the next
        miss of someone still browsing), continues the same outage and keeps its ``since``.
        After a longer quiet gap it is a new outage, so a blip days ago never dates today's.
        """
        with self._state_mu:
            now = self._clock()
            previous = self._hosts.get(host)
            continues = previous is not None and now < previous.until + self._cooldown_s
            since = previous.since if continues else self._now()
            self._hosts[host] = _HostState(until=now + self._cooldown_s, since=since)
        if not continues:
            log.warning("armory: %s unreachable; misses answer 503 for %.0f s", host, self._cooldown_s)

    def _host_answered(self, host: str) -> None:
        with self._state_mu:
            previous = self._hosts.pop(host, None)
        if previous is not None:
            log.info("armory: %s answers again", host)

    # cache writes (the cache creates armory/ and its own tree with mode 0700 on the first write)

    def _store(self, local_path: str, upstream: Upstream, body: bytes) -> Served:
        etag = _asset_etag(local_path, len(body))
        try:
            path = self.cache.put(local_path, body)
        except OSError as err:
            self._cache_failed(err)
            return Served(200, None, upstream.content_type, IMMUTABLE_CACHE_CONTROL, etag, body)
        self._cache_ok()
        return Served(200, path, upstream.content_type, IMMUTABLE_CACHE_CONTROL, etag)

    def _remember_missing(self, local_path: str) -> Served:
        try:
            self.cache.mark_negative(local_path)
        except OSError as err:
            self._cache_failed(err)
        else:
            self._cache_ok()
        return _NOT_FOUND

    def _cache_failed(self, err: OSError) -> None:
        """Pass-through mode until a write works again; the reason goes to the footer strip."""
        message = f"{_cache_write_error(err)}; serving without storing. Check that the admin data folder is writable."
        with self._state_mu:
            first = self._cache_error is None
            self._cache_error = message
        if first:
            log.warning("armory: %s; pass-through mode", _cache_write_error(err))

    def _cache_ok(self) -> None:
        with self._state_mu:
            was_failing = self._cache_error is not None
            self._cache_error = None
        if was_failing:
            log.info("armory: the model cache is writable again")

    def _read_cache_file(self, local_path: str) -> bytes | None:
        """The bytes of a regular cache file, or None when there is none; other errors raise ``OSError``."""
        path = self.cache.path_for(local_path)
        try:
            st = os.lstat(path)
            if not stat.S_ISREG(st.st_mode):
                return None
            return path.read_bytes()
        except (FileNotFoundError, NotADirectoryError):
            return None

    def _cache_file_size(self, local_path: str) -> int | None:
        try:
            st = os.lstat(self.cache.path_for(local_path))
        except (FileNotFoundError, NotADirectoryError):
            return None
        return st.st_size if stat.S_ISREG(st.st_mode) else None

    # viewer script pinning

    def _pin_first_use(self, result: FetchResult) -> Served:
        with self._viewer_lock:
            pinned = self._viewer_hit()
            if pinned is not None:  # an Update pinned one meanwhile: the pinned copy wins
                return pinned
            try:
                self._write_viewer(result)
            except OSError as err:
                self._cache_failed(err)
                return self._viewer_pass_through(result.body)
            self._cache_ok()
            pinned = self._viewer_hit()
        return pinned if pinned is not None else self._viewer_pass_through(result.body)

    @staticmethod
    def _viewer_pass_through(body: bytes) -> Served:
        return Served(200, None, _VIEWER_UPSTREAM.content_type, VIEWER_CACHE_CONTROL, f'"{_sha256(body)}"', body)

    def _write_viewer(self, result: FetchResult) -> None:
        """Caller holds ``_viewer_lock``. The script first, then its meta."""
        body = result.body
        meta = _viewer_meta_json(
            body, etag=result.etag, last_modified=result.last_modified, fetched_at=_iso(self._now())
        )
        self.cache.put(VIEWER_PATH, body)
        self.cache.put(VIEWER_META_PATH, meta)
        self._forget_viewer_digest()

    def _swap_in_viewer(self, result: FetchResult) -> None:
        """Caller holds ``_viewer_lock``. Copies rather than renames, so a pinned script always exists."""
        current = self._read_cache_file(VIEWER_PATH)
        if current is not None and _sha256(current) == _sha256(result.body):
            # The same script under new validators: refresh the meta, keep .prev (still the real previous one).
            self.cache.put(
                VIEWER_META_PATH,
                _viewer_meta_json(
                    current, etag=result.etag, last_modified=result.last_modified, fetched_at=_iso(self._now())
                ),
            )
            return
        if current is not None:
            current_meta = self._read_cache_file(VIEWER_META_PATH)
            if current_meta is None:
                current_meta = _viewer_meta_json(current, etag=None, last_modified=None, fetched_at=None)
            self.cache.put(VIEWER_PREV_PATH, current)
            self.cache.put(VIEWER_PREV_META_PATH, current_meta)  # the exact bytes, for an exact Revert
        self._write_viewer(result)

    def _pinned_etag(self) -> str | None:
        """The pinned copy's upstream ETag when one is pinned (and it is safe to send back), else None."""
        try:
            if self._cache_file_size(VIEWER_PATH) is None:
                return None
            meta = self._read_cache_file(VIEWER_META_PATH)
        except (OSError, ValueError):
            return None
        parsed = _parse_viewer_meta(meta) if meta is not None else None
        return parsed.etag if parsed is not None else None

    def update_viewer(self) -> ViewerStatus:
        """Fetch the viewer script again (conditional GET) and pin the new copy, keeping the current one as ``.prev``.

        A 304 changes nothing; the same bytes under new validators refresh only the meta.
        Refused while switched off (R18), while the host cools down and while another
        Update/Revert runs; every failure keeps the pinned copy. Raises ``ViewerError``.
        """
        if not self._viewer_op.acquire(blocking=False):
            raise ViewerError(_VIEWER_BUSY)
        try:
            if not self.settings.enabled():
                raise ViewerError(_VIEWER_REASONS["disabled"])
            try:
                result = self._guarded_fetch(_VIEWER_UPSTREAM.url, if_none_match=self._pinned_etag())
            except _Unavailable as err:
                raise ViewerError(_viewer_unavailable(err)) from None
            if result.status == 304:
                return self.viewer_status()
            if result.status == 404:
                raise ViewerError("Wowhead no longer serves the viewer script (HTTP 404); the pinned copy is kept.")
            with self._viewer_lock:
                try:
                    self._swap_in_viewer(result)
                except OSError as err:
                    self._cache_failed(err)
                    raise ViewerError(
                        f"{_cache_write_error(err)}; check that the admin data folder is writable."
                    ) from None
                self._cache_ok()
            return self.viewer_status()
        finally:
            self._viewer_op.release()

    def revert_viewer(self) -> ViewerStatus:
        """Pin the previous copy again, bytes and meta exactly as they were; ``.prev`` is used up.

        Local only, so it also works while switched off or while Wowhead is down.
        Raises ``ViewerError`` when there is no previous copy or it cannot be written.
        """
        if not self._viewer_op.acquire(blocking=False):
            raise ViewerError(_VIEWER_BUSY)
        try:
            with self._viewer_lock:
                try:
                    previous = self._read_cache_file(VIEWER_PREV_PATH)
                    if previous is None:
                        raise ViewerError("There is no previous viewer script to revert to.")
                    previous_meta = self._read_cache_file(VIEWER_PREV_META_PATH)
                    if previous_meta is None:
                        previous_meta = _viewer_meta_json(previous, etag=None, last_modified=None, fetched_at=None)
                    self.cache.put(VIEWER_PATH, previous)
                    self.cache.put(VIEWER_META_PATH, previous_meta)
                except OSError as err:
                    self._cache_failed(err)
                    raise ViewerError(
                        f"{_cache_write_error(err)}; check that the admin data folder is writable."
                    ) from None
                self._forget_viewer_digest()
                self._cache_ok()
                for leftover in (VIEWER_PREV_PATH, VIEWER_PREV_META_PATH):
                    try:
                        self.cache.remove(leftover)
                    except OSError as err:  # reverted already; a leftover only keeps Revert on offer
                        log.warning("armory: could not remove %s (%s)", leftover, err.strerror)
            return self.viewer_status()
        finally:
            self._viewer_op.release()

    def viewer_status(self) -> ViewerStatus:
        try:
            size = self._cache_file_size(VIEWER_PATH)
            has_prev = self._cache_file_size(VIEWER_PREV_PATH) is not None
            meta = self._read_cache_file(VIEWER_META_PATH) if size is not None else None
        except (OSError, ValueError):
            return ViewerStatus(fetched_at=None, etag=None, size=0, has_prev=False)
        if size is None:
            return ViewerStatus(fetched_at=None, etag=None, size=0, has_prev=has_prev)
        parsed = _parse_viewer_meta(meta) if meta is not None else None
        return ViewerStatus(
            fetched_at=parsed.fetched_at if parsed is not None else None,
            etag=parsed.etag if parsed is not None else None,
            size=size,
            has_prev=has_prev,
        )

    # footer strip

    def status(self) -> ProxyStatus:
        """For the footer strip. Blocking (the cache lock, at most one walk): call it off the event loop."""
        with self._state_mu:
            now = self._clock()
            # R16b: only active cool-downs; an expired one is not news (its entry stays for since).
            since = min((state.since for state in self._hosts.values() if now < state.until), default=None)
            cache_error = self._cache_error
        return ProxyStatus(
            enabled=self.settings.enabled(),
            cache_bytes=self.cache.size_bytes(),
            viewer=self.viewer_status(),
            unreachable_since=_iso(since) if since is not None else None,
            cache_writable=cache_error is None,
            pass_through=cache_error is not None,
            cache_error=cache_error,
        )

    def clear_cache(self) -> int:
        """Delete every cached asset and remembered 404; the pinned script, its ``.prev`` and both metas stay."""
        return self.cache.clear()


# --- the process-wide instance ---

_proxy: AssetProxy | None = None
_proxy_lock = threading.Lock()


def get_proxy() -> AssetProxy:
    """The one ``AssetProxy`` of this process, under ``ADMIN_DATA_DIR/armory``; building it touches nothing."""
    global _proxy
    with _proxy_lock:
        if _proxy is None:
            _proxy = AssetProxy(Path(os.environ.get("ADMIN_DATA_DIR", "/admin-data")) / "armory")
        return _proxy


def reset_proxy() -> None:
    """Forget the instance (tests)."""
    global _proxy
    with _proxy_lock:
        _proxy = None
