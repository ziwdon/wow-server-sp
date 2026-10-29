#!/usr/bin/env python3
"""Armory browser check (spec 13, plan Task 13): real admin code, real proxy, real Wowhead.

NOT a unit test: pytest never collects it (no ``test_`` prefix) and it opens real sockets.
Run it by hand, once per viewer change, inside a throw-away sandbox container — see
``tests/browser/README.md`` for the exact container and the vendor files it needs.

What it does:

* builds ``/tmp/fx`` fixtures: an empty AC stack dir, ``ADMIN_DATA_DIR``, and a client-data
  dir whose DBCs are the synthetic set of ``tests/test_dbc.py`` plus a few rows for the
  three spike characters (talents, glyphs and item icons, some of them real Wowhead icon
  names so icon loading is exercised);
* replaces every DB read with the three spike characters of
  ``tests/data/armory/spike_characters.json`` (guid 2506 Vegivaca, 4957 Sariel, 7138
  Pitokax) and makes any real MySQL connection fail;
* runs the real app (uvicorn on 127.0.0.1) with the real ``AssetProxy`` and the real
  ``fetch_upstream``, wrapped only to record each upstream URL, status and size;
* drives Chromium (SwiftShader WebGL, every non-loopback request sent to a dead proxy) through the three character pages at 1400 px and
  390 px, the sheath and hide-tabard toggles, the glyph tab, a touch drag, a warm repeat load and
  the Players page, and checks what spec 13 and the Task 13 brief ask (printed as PASS/FAIL lines and
  written to ``<out>/armory-check.json`` with the screenshots).

The only outbound traffic is the admin's own proxy fetching allow-listed paths from
wow.zamimg.com / www.wowhead.com. The browser itself reaches nothing but the admin (its
proxy is a dead port), and every attempt to reach another origin is reported.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import struct
import sys
import threading
import time
import zlib
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[2]  # wow-server-sp-admin (or its copy)
HOST = "127.0.0.1"
GUIDS = (2506, 4957, 7138)
NAMES = {2506: "vegivaca", 4957: "sariel", 7138: "pitokax"}
RENDER_TIMEOUT_MS = 30_000  # spec 7.5 / the brief: rendered within 30 s
QUIET_SECONDS = 5.0  # "the viewer has finished loading": no request for this long
QUIET_MAX_SECONDS = 60.0
CHROMIUM_ARGS = [  # the spike's flags: software WebGL in a container without a GPU
    "--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist", "--enable-webgl",
]
REAL_ICON = "inv_chest_leather_04"  # exists upstream (the brief)
MISSING_ICON = "armorycheck_missing_icon"  # allow-listed shape, not on Wowhead

# Every upstream URL must have one of these shapes (spec 2 "Request shapes seen", 8 allow-list),
# written out here independently of armory_assets' own rules.
_N = r"[0-9]{1,8}"
UPSTREAM_SHAPES = [re.compile(p) for p in (
    r"https://wow\.zamimg\.com/modelviewer/live/viewer/viewer\.min\.js",
    rf"https://wow\.zamimg\.com/modelviewer/wrath/meta/character/{_N}\.json",
    rf"https://wow\.zamimg\.com/modelviewer/wrath/meta/charactercustomization/{_N}\.json",
    rf"https://wow\.zamimg\.com/modelviewer/wrath/meta/armor/(1|3|4|5|6|7|8|9|10|16|19|20)/{_N}\.json",
    rf"https://wow\.zamimg\.com/modelviewer/wrath/meta/item/{_N}\.json",
    rf"https://wow\.zamimg\.com/modelviewer/wrath/meta/itemvisual/{_N}\.json",
    rf"https://wow\.zamimg\.com/modelviewer/wrath/m2/{_N}\.m2",
    rf"https://wow\.zamimg\.com/modelviewer/wrath/skin/{_N}\.skin",
    rf"https://wow\.zamimg\.com/modelviewer/wrath/textures/{_N}\.webp",
    r"https://wow\.zamimg\.com/images/wow/icons/large/[a-z0-9_-]{1,64}\.jpg",
    r"https://www\.wowhead\.com/wotlk/item=[1-9][0-9]*&xml",
)]

# Runs in every page before its own scripts: CSP violations, model-panel state changes and
# every ZamModelViewer instance (wrapped right after the viewer script ran: a capture
# listener on document fires before the script tag's own load listener in armory.js).
INIT_JS = r"""
(() => {
  const check = window.__check = { csp: [], states: [], viewers: [], given: [], t0: performance.now() };
  document.addEventListener('securitypolicyviolation', (e) => {
    check.csp.push({ directive: e.effectiveDirective || e.violatedDirective, blocked: e.blockedURI,
                     disposition: e.disposition, sample: e.sample, source: e.sourceFile, line: e.lineNumber });
  });
  new MutationObserver((records) => {
    for (const r of records) {
      if (r.target.id === 'armory-model') check.states.push([r.target.dataset.state, performance.now()]);
    }
  }).observe(document, { subtree: true, attributes: true, attributeFilter: ['data-state'] });
  document.addEventListener('load', (e) => {
    const t = e.target;
    if (!(t instanceof HTMLScriptElement) || !/viewer\.min\.js/.test(t.src)) return;
    const Orig = window.ZamModelViewer;
    if (!Orig || Orig.__checkWrapped) return;
    const Wrapped = function (opts) {
      // What armory.js handed this viewer (copied before the viewer can touch it).
      const cc = (opts && opts.charCustomization) || {};
      check.given.push({ items: JSON.parse(JSON.stringify((opts && opts.items) || [])),
                         sheathMain: cc.sheathMain || 0 });
      const v = new Orig(opts);
      check.viewers.push(v);
      return v;
    };
    Object.assign(Wrapped, Orig);
    Wrapped.prototype = Orig.prototype;
    Wrapped.__checkWrapped = true;
    window.ZamModelViewer = Wrapped;
  }, true);
})();
"""

PANEL_JS = r"""
() => {
  const panel = document.getElementById('armory-model');
  const box = panel && panel.querySelector('.armory-model-canvas');
  const canvases = box ? Array.from(box.querySelectorAll('canvas')) : [];
  const rect = (el) => { if (!el) return null; const r = el.getBoundingClientRect();
                         return { x: r.x, y: r.y, w: r.width, h: r.height }; };
  const c = window.__check;
  const v = c.viewers[c.viewers.length - 1];
  return {
    state: panel && panel.dataset.state,
    text: panel && (panel.querySelector('.armory-model-text') || {}).textContent,
    note: panel && (panel.querySelector('.armory-model-note') || {}).textContent,
    canvases: canvases.length,
    canvasAttr: canvases.map((x) => [x.width, x.height]),
    canvasRect: rect(canvases[0]),
    boxRect: rect(box),
    panelRect: rect(panel),
    jquery: Boolean(window.jQuery),
    viewers: c.viewers.length,
    given: c.given.length ? c.given[c.given.length - 1] : null,
    hideTabard: (() => { const t = document.getElementById('armory-hide-tabard');
                         return t ? { disabled: t.disabled, checked: t.checked } : null; })(),
    hasDestroy: Boolean(v && typeof v.destroy === 'function'),
    azimuth: v && v.renderer ? v.renderer.azimuth : null,
    zenith: v && v.renderer ? v.renderer.zenith : null,
    states: c.states.slice(),
    csp: c.csp.slice(),
    no3d: document.querySelectorAll('.no-3d').length,
    stylesheets: Array.from(document.querySelectorAll('link[rel=stylesheet]')).map((l) => l.getAttribute('href')),
    m2: performance.getEntriesByType('resource').filter((e) => /\/wrath\/m2\//.test(e.name))
          .map((e) => [e.name.replace(/^.*\/wrath\//, ''), e.responseEnd, e.transferSize]),
  };
}
"""


# --- small helpers ---


def die(msg: str) -> None:
    print(f"armory_check: {msg}", file=sys.stderr)
    sys.exit(2)


def guard_sandbox(fx: Path) -> None:
    """Refuse to run anywhere near the live stack (CLAUDE.md "Mandatory live-stack test safety")."""
    if os.path.exists("/var/run/docker.sock"):
        die("refusing to run: /var/run/docker.sock exists (run inside the throw-away sandbox)")
    if os.path.exists("/opt/stacks"):
        die("refusing to run: /opt/stacks exists (run inside the throw-away sandbox)")
    if not str(fx.resolve()).startswith("/tmp/"):
        die(f"refusing to run: the fixture dir {fx} is not under /tmp")


def vendor_ok(path: Path) -> bool:
    return path.is_file() and path.stat().st_size > 200


def png_pixels(data: bytes) -> tuple[int, int, int, bytes]:
    """Decode an 8-bit, non-interlaced RGB/RGBA PNG (what Chromium screenshots are)."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    pos, idat = 8, []
    width = height = depth = color = interlace = 0
    while pos < len(data):
        length, kind = struct.unpack(">I4s", data[pos:pos + 8])
        chunk = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if kind == b"IHDR":
            width, height, depth, color, _c, _f, interlace = struct.unpack(">IIBBBBB", chunk)
        elif kind == b"IDAT":
            idat.append(chunk)
        elif kind == b"IEND":
            break
    if depth != 8 or interlace or color not in (2, 6):
        raise ValueError(f"unsupported PNG (depth {depth}, colour {color}, interlace {interlace})")
    bpp = 3 if color == 2 else 4
    raw = zlib.decompress(b"".join(idat))
    stride = width * bpp
    out = bytearray()
    prev = bytearray(stride)
    i = 0
    for _y in range(height):
        kind = raw[i]
        line = bytearray(raw[i + 1:i + 1 + stride])
        i += 1 + stride
        if kind == 1:
            for x in range(bpp, stride):
                line[x] = (line[x] + line[x - bpp]) & 255
        elif kind == 2:
            for x in range(stride):
                line[x] = (line[x] + prev[x]) & 255
        elif kind == 3:
            for x in range(stride):
                left = line[x - bpp] if x >= bpp else 0
                line[x] = (line[x] + ((left + prev[x]) >> 1)) & 255
        elif kind == 4:
            for x in range(stride):
                a = line[x - bpp] if x >= bpp else 0
                b = prev[x]
                c = prev[x - bpp] if x >= bpp else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if pa <= pb and pa <= pc else (b if pb <= pc else c)
                line[x] = (line[x] + pred) & 255
        out += line
        prev = line
    return width, height, bpp, bytes(out)


def picture_stats(png: bytes) -> dict:
    """How much is drawn: distinct colours (4 bits/channel) and the share of pixels that
    differ clearly from the most common colour (the cleared background)."""
    width, height, bpp, px = png_pixels(png)
    counts: dict[tuple[int, int, int], int] = {}
    for i in range(0, len(px), bpp):
        key = (px[i] >> 4, px[i + 1] >> 4, px[i + 2] >> 4)
        counts[key] = counts.get(key, 0) + 1
    bg = max(counts, key=counts.get)
    total = width * height
    off = sum(n for key, n in counts.items() if max(abs(key[j] - bg[j]) for j in range(3)) >= 2)
    return {"size": [width, height], "colours": len(counts), "drawn_share": round(off / total, 4)}


def not_blank(stats: dict) -> bool:
    return stats["colours"] >= 40 and stats["drawn_share"] >= 0.03


# Where the tabard sits on a front-facing model in the panel (fractions of the panel shot:
# x0, y0, x1, y1): the upper torso, measured on Vegivaca's renders.
TORSO = (0.38, 0.22, 0.62, 0.45)


def region_stats(png: bytes, box=TORSO) -> dict:
    """Mean colour of a region and the share of its pixels that are plain mid grey (the
    undyed Guild Tabard)."""
    width, height, bpp, px = png_pixels(png)
    x0, y0, x1, y1 = int(box[0] * width), int(box[1] * height), int(box[2] * width), int(box[3] * height)
    n = grey = 0
    sums = [0, 0, 0]
    for y in range(y0, y1):
        row = y * width * bpp
        for x in range(x0, x1):
            i = row + x * bpp
            r, g, b = px[i], px[i + 1], px[i + 2]
            sums[0] += r
            sums[1] += g
            sums[2] += b
            n += 1
            if max(r, g, b) - min(r, g, b) < 18 and 90 <= (r + g + b) / 3 <= 215:
                grey += 1
    return {"mean": [round(v / n, 1) for v in sums], "grey_share": round(grey / n, 3)}


def region_diff(a: bytes, b: bytes, box=TORSO) -> float:
    """Mean absolute channel difference of two same-size shots inside ``box``."""
    wa, ha, bpp_a, pa = png_pixels(a)
    wb, hb, bpp_b, pb = png_pixels(b)
    if (wa, ha) != (wb, hb):
        return float("inf")
    x0, y0, x1, y1 = int(box[0] * wa), int(box[1] * ha), int(box[2] * wa), int(box[3] * ha)
    total = n = 0
    for y in range(y0, y1):
        for x in range(x0, x1):
            i, j = (y * wa + x) * bpp_a, (y * wb + x) * bpp_b
            total += abs(pa[i] - pb[j]) + abs(pa[i + 1] - pb[j + 1]) + abs(pa[i + 2] - pb[j + 2])
            n += 3
    return round(total / n, 2)


def has_tabard(items) -> bool:
    return any(isinstance(i, list) and i and i[0] == 19 for i in items or [])


# --- fixtures ---


def load_characters() -> list[dict]:
    data = json.loads((APP_DIR / "tests" / "data" / "armory" / "spike_characters.json").read_text("utf-8"))
    return data["characters"]


# Icons per InventoryType for the paper-doll (Wowhead icon names; a few may not exist, which
# exercises the "failed icon -> silhouette" path on visible slots too).
TYPE_ICONS = {
    1: "inv_helmet_03", 2: "inv_jewelry_necklace_07", 3: "inv_shoulder_02", 4: "inv_shirt_01",
    5: REAL_ICON, 6: "inv_belt_03", 7: "inv_pants_03", 8: "inv_boots_01", 9: "inv_bracer_02",
    10: "inv_gauntlets_04", 11: "inv_jewelry_ring_03", 12: "inv_misc_gem_01", 13: "inv_sword_04",
    15: "inv_weapon_bow_02", 16: "inv_misc_cape_02", 17: "inv_axe_09", 19: "inv_shirt_guildtabard_01",
    25: "inv_axe_04",
}

# Talent tabs per class: (class id, [(tab id, name, order)]); three talents per tab.
CLASS_TABS = {
    1: [(90100, "Arms", 0), (90101, "Fury", 1), (90102, "Protection", 2)],
    11: [(90110, "Balance", 0), (90111, "Feral Combat", 1), (90112, "Restoration", 2)],
    4: [(90120, "Assassination", 0), (90121, "Combat", 1), (90122, "Subtlety", 2)],
}
# spell icon id -> texture (the DBC stores the Interface\Icons path; dbc.icon_basename lowercases)
SPELL_ICONS = {
    90001: "Interface\\Icons\\Ability_Warrior_Charge",
    90002: "Interface\\Icons\\Ability_Warrior_Cleave",
    90003: "Interface\\Icons\\Ability_Parry",
    90004: "Interface\\Icons\\INV_Chest_Leather_04",
    90005: "Interface\\Icons\\INV_Glyph_MajorWarrior",
    90006: "Interface\\Icons\\ArmoryCheck_Missing_Icon",
}


def write_dbc_fixture(dbc_dir: Path, characters: list[dict]) -> dict:
    """The synthetic DBC set plus rows for the three characters. Returns what it planted."""
    sys.path.insert(0, str(APP_DIR))
    from tests.test_dbc import FIELDS, record, synthetic_rows, write_dbc

    rows = synthetic_rows()
    f = FIELDS
    for icon_id, path in SPELL_ICONS.items():
        rows["SpellIcon.dbc"].append(record(f["SpellIcon.dbc"], {0: icon_id, 1: path}))
    talent_id, spell_id = 90300, 91000
    learned: dict[int, set[int]] = {}
    # the three talents of every tab: (tier, column, ranks, icon); the last one's icon is missing
    layout = [(0, 0, 3, 90001), (0, 1, 1, 90002), (1, 1, 2, 90006)]
    for class_id, tabs in CLASS_TABS.items():
        learned[class_id] = set()
        for tab_id, name, order in tabs:
            rows["TalentTab.dbc"].append(record(f["TalentTab.dbc"], {
                0: tab_id, 1: name, 18: 90003, 20: 1 << (class_id - 1), 22: order}))
            for tier, column, ranks, icon in layout:
                talent_id += 1
                rank_ids = []
                for rank in range(ranks):
                    spell_id += 1
                    rank_ids.append(spell_id)
                    rows["Spell.dbc"].append(record(f["Spell.dbc"], {
                        0: spell_id, 133: icon, 136: f"{name} talent {talent_id - 90300}",
                        153: f"Rank {rank + 1}", 170: f"Check talent {talent_id}, rank {rank + 1}."}))
                values = {0: talent_id, 1: tab_id, 2: tier, 3: column}
                for k, sid in enumerate(rank_ids):
                    values[4 + k] = sid
                rows["Talent.dbc"].append(record(f["Talent.dbc"], values))
                if order == 0 or (order == 1 and tier == 0):
                    learned[class_id].add(rank_ids[-1])
    # glyphs: a real major icon, the brief's real icon on a minor one, and a missing icon
    glyphs = [(90201, 92001, 0, 90005, "Glyph of Charging"), (90202, 92002, 1, 90004, "Glyph of Leather"),
              (90203, 92003, 0, 90006, "Glyph of Nothing")]
    for glyph_id, sid, flags, icon, name in glyphs:
        rows["Spell.dbc"].append(record(f["Spell.dbc"], {0: sid, 133: icon, 136: name}))
        rows["GlyphProperties.dbc"].append(record(f["GlyphProperties.dbc"], {0: glyph_id, 1: sid, 2: flags}))
    # item icons: one ItemDisplayInfo row per display id the characters wear
    seen: set[int] = set()
    for char in characters:
        for gear in char["gear"]:
            did = gear["display_id"]
            if did in seen:
                continue
            seen.add(did)
            icon = TYPE_ICONS.get(gear["inventory_type"], MISSING_ICON)
            rows["ItemDisplayInfo.dbc"].append(record(f["ItemDisplayInfo.dbc"], {0: did, 5: icon}))
    dbc_dir.mkdir(parents=True, exist_ok=True)
    for name, table in rows.items():
        write_dbc(dbc_dir / name, FIELDS[name], table)
    return {"learned": learned, "glyphs": [g[0] for g in glyphs]}


def prepare_fixtures(fx: Path, cache_mode: str) -> dict[str, Path]:
    paths = {
        "ac": fx / "ac",
        "admin_data": fx / "admin-data",
        "client": fx / "client-data",
        "dist": fx / "dist",
        "snap": fx / "snap",
        "out": fx / "out",
    }
    for key in ("ac", "client", "dist", "snap"):
        shutil.rmtree(paths[key], ignore_errors=True)
    for sub in ("backups", "logs", "configs"):
        (paths["ac"] / sub).mkdir(parents=True, exist_ok=True)
    paths["dist"].mkdir(parents=True)
    for name in ("worldserver.conf.dist", "playerbots.conf.dist", "mod_ahbot.conf.dist",
                 "individualProgression.conf.dist"):
        (paths["dist"] / name).write_text("")
    paths["snap"].mkdir(parents=True)
    paths["out"].mkdir(parents=True, exist_ok=True)
    armory = paths["admin_data"] / "armory"
    if cache_mode == "fresh":
        # Like the footer's Clear: every cached asset and the display-ID map go, the pinned
        # viewer script stays (so repeated runs do not re-fetch it).
        cache = armory / "cache"
        if cache.is_dir():
            for child in cache.iterdir():
                if child.name != "live":
                    shutil.rmtree(child) if child.is_dir() else child.unlink()
        for name in ("displayid-map.json", "armory.json"):
            (armory / name).unlink(missing_ok=True)
    elif cache_mode == "empty":
        shutil.rmtree(armory, ignore_errors=True)
    paths["admin_data"].mkdir(parents=True, exist_ok=True)
    return paths


# --- the app, with the DB layer replaced ---


class Recorder:
    """Wraps the real ``fetch_upstream``: every upstream URL, its status, size and time."""

    def __init__(self, fetch):
        self._fetch = fetch
        self._lock = threading.Lock()
        self.calls: list[dict] = []
        self.last = 0.0

    def __call__(self, url, **kw):
        started = time.monotonic()
        entry = {"url": url, "status": None, "bytes": 0}
        try:
            result = self._fetch(url, **kw)
            entry["status"], entry["bytes"] = result.status, len(result.body)
            return result
        except Exception as e:  # UpstreamError and anything unexpected: recorded, re-raised
            entry["status"] = f"error: {getattr(e, 'reason', type(e).__name__)}"
            raise
        finally:
            entry["ms"] = int((time.monotonic() - started) * 1000)
            with self._lock:
                self.calls.append(entry)
                self.last = time.monotonic()

    def count(self) -> int:
        with self._lock:
            return len(self.calls)

    def since(self, index: int) -> list[dict]:
        with self._lock:
            return list(self.calls[index:])


def build_app(paths: dict[str, Path], characters: list[dict], planted: dict, csp: str | None,
              report_only: str | None):
    os.environ["AC_STACK_DIR"] = str(paths["ac"])
    os.environ["ADMIN_DATA_DIR"] = str(paths["admin_data"])
    os.environ["CLIENT_DATA_DIR"] = str(paths["client"])
    os.environ["ADMIN_SNAPSHOTS_DIR"] = str(paths["snap"])
    sys.path.insert(0, str(APP_DIR))

    import mysql.connector

    def no_db(*_a, **_k):
        raise RuntimeError("no database in the armory browser check")

    mysql.connector.connect = no_db

    from app.state import init_state

    init_state(
        dist_dir=paths["dist"],
        admin_yml=paths["ac"] / "docker-compose.admin.yml",
        override_yml=paths["ac"] / "docker-compose.override.yml",
        configs_dir=paths["ac"] / "configs",
        snapshots_dir=paths["snap"],
    )
    import app.main as main
    from app.services import armory as armory_svc
    from app.services import armory_assets
    from app.services import players as players_svc
    from app.services.armory import CharacterView, EquippedItem, ModelInputs, SearchHit

    if csp is not None:
        main.ARMORY_CSP = csp

    recorder = Recorder(armory_assets.fetch_upstream)
    proxy = armory_assets.AssetProxy(paths["admin_data"] / "armory", fetch=recorder)
    armory_assets._proxy = proxy  # get_proxy() (routes and the resolver) returns this one

    stats_columns = ("maxhealth", "maxpower1", "maxpower2", "strength", "agility", "stamina",
                     "intellect", "spirit", "armor", "attackPower", "critPct", "dodgePct", "parryPct")
    views: dict[int, CharacterView] = {}
    for i, char in enumerate(characters):
        account, is_bot = ("CARLOS", False) if i == 0 else (f"RNDBOT{100 + i}", True)
        row = (char["guid"], char["name"], account, int(is_bot), char["race"], char["gender"], char["class"],
               char["level"], 0, 1_793_000_000 + i * 3600, char["skin"], char["face"], char["hairStyle"],
               char["hairColor"], char["facialStyle"])
        header = armory_svc._header(row)
        items = [
            EquippedItem(
                slot=g["slot"], entry=g["entry"], name=g["name"], display_id=g["display_id"],
                inventory_type=g["inventory_type"], item_class=g["item_class"], subclass=g["subclass"],
                quality=1 if g["inventory_type"] in (4, 19) else 4, item_level=66, required_level=60,
            )
            for g in char["gear"]
        ]
        learned = planted["learned"].get(char["class"], set())
        two_specs = i == 0  # Vegivaca: a hidden second spec panel (lazy talent icons)
        views[char["guid"]] = CharacterView(
            header=header,
            items=items,
            avg_ilvl=66,
            specs=(set(learned), set(list(learned)[:2]) if two_specs else set()),
            spec_count=2 if two_specs else 1,
            active_spec=0,
            glyphs=(tuple(planted["glyphs"]) + (0, 0, 0), (0,) * 6),
            stats_row=(5800, 0, 1000, 400, 120, 380, 30, 60, 6200, 1400, 18.5, 9.25, 11.0) if i == 0 else None,
            stats_columns=stats_columns if i == 0 else (),
        )

    def collect_character(guid, **_kw):
        return views.get(guid)

    def collect_model_inputs(guid, **_kw):
        view = views.get(guid)
        return None if view is None else ModelInputs(header=view.header, items=view.items)

    def search_characters(q, **_kw):
        prefix = (q or "").strip().lower()
        if len(prefix) < 2:
            return []
        return [
            SearchHit(guid=v.header.guid, name=v.header.name, level=v.header.level, race=v.header.race,
                      class_id=v.header.class_id, class_name=v.header.class_name,
                      class_color=v.header.class_color, race_name=v.header.race_name, is_bot=v.header.is_bot)
            for v in views.values() if v.header.name.lower().startswith(prefix)
        ]

    def collect_players(**_kw):
        rows = []
        for v in views.values():
            h = v.header
            rows.append(players_svc.CharRow(
                account=h.account, name=h.name, class_name=h.class_name, class_color=h.class_color,
                race_name=h.race_name, faction=h.faction, faction_color=h.faction_color, level=h.level,
                online=not h.is_bot, zone_name="Orgrimmar", latency=40, last_logout=h.logout_time,
                guid=h.guid, online_now=not h.is_bot))
        human = [r for r in rows if r.online_now]
        top_pve = tuple(
            players_svc.RankRow(n + 1, r.name, r.class_name, r.class_color, r.race_name, r.faction,
                                r.faction_color, r.level, 66 - n, r.guid) for n, r in enumerate(rows))
        top_pvp = tuple(
            players_svc.PvpRankRow(n + 1, r.name, r.class_name, r.class_color, r.race_name, r.faction,
                                   r.faction_color, 900 - 100 * n, 15000 - 1000 * n, r.guid)
            for n, r in enumerate(rows))
        return players_svc.PlayersSnapshot(
            fetched_at=time.time(), total_players=len(rows), online_players=len(human),
            cap_vanilla=0, cap_tbc=0, cap_wotlk=len(rows), online_now=tuple(human),
            all_groups=tuple(players_svc.AccountGroup(r.account, (r,)) for r in rows),
            top_pve=top_pve, top_pvp=top_pvp)

    # No Docker socket in the sandbox: docker.from_env() would raise out of /api/status (a 500
    # the nav pill turns into an "Internal error" toast over the screenshots). Report the
    # worldserver as "unknown", as the app does when the daemon does not answer.
    from app.services import docker_client

    docker_client.inspect_worldserver = lambda: docker_client.ContainerInfo(
        status="unknown", started_at=None, exit_code=None, image=None)

    armory_svc.collect_character = collect_character
    armory_svc.collect_model_inputs = collect_model_inputs
    armory_svc.search_characters = search_characters
    players_svc.collect_players = collect_players

    asgi = main.app
    if report_only:
        asgi = _ReportOnly(asgi, report_only)
    return asgi, proxy, recorder


class _ReportOnly:
    """Adds a Content-Security-Policy-Report-Only header to armory paths (discovery only)."""

    def __init__(self, app, policy: str):
        self._app, self._policy = app, policy.encode("latin-1")

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "") if scope["type"] == "http" else ""
        if not (path == "/armory" or path.startswith("/armory/")):
            await self._app(scope, receive, send)
            return

        async def wrapped(message):
            if message["type"] == "http.response.start":
                message = {**message, "headers": [*message.get("headers", []),
                                                   (b"content-security-policy-report-only", self._policy)]}
            await send(message)

        await self._app(scope, receive, wrapped)


def start_server(asgi, port: int):
    import uvicorn

    server = uvicorn.Server(uvicorn.Config(asgi, host=HOST, port=port, log_level="warning", lifespan="on"))
    thread = threading.Thread(target=server.run, name="uvicorn", daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            die("the admin app did not start")
        time.sleep(0.1)
    return server, thread


# --- the browser run ---


class Results:
    def __init__(self):
        self.checks: list[dict] = []
        self.data: dict = {}

    def check(self, name: str, ok: bool, detail="") -> bool:
        self.checks.append({"check": name, "ok": bool(ok), "detail": detail})
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""), flush=True)
        return ok

    def info(self, name: str, detail) -> None:
        self.checks.append({"check": name, "ok": None, "detail": detail})
        print(f"[INFO] {name} — {detail}", flush=True)


class PageWatch:
    """Everything one page asks for: requests (and any outside the admin), console, errors."""

    def __init__(self, page, base: str):
        self.base = base
        self.requests: list[str] = []
        self.outside: list[str] = []
        self.blobs = 0  # the viewer's own object URLs (blob:<admin origin>/…): not network
        self.failed: list[str] = []
        self.console: list[str] = []
        self.model_json = None
        self.last = time.monotonic()
        page.on("request", self._request)
        page.on("requestfailed", lambda r: self.failed.append(f"{r.url} {r.failure}"))
        page.on("console", self._console)
        page.on("pageerror", lambda e: self.console.append(f"pageerror: {e}"))
        page.on("response", self._response)

    def _console(self, message):
        text = message.text
        policy = "Content Security Policy" in text or "Content-Security-Policy" in text
        self.console.append(f"{message.type}: {text if policy else text[:300]}")

    def _request(self, request):
        self.last = time.monotonic()
        url = request.url
        if url.startswith("blob:" + self.base + "/"):
            self.blobs += 1
            return
        self.requests.append(url)
        if not url.startswith(self.base + "/"):
            self.outside.append(url)

    def _response(self, response):
        self.last = time.monotonic()
        if re.search(r"/api/armory/\d+/model$", response.url):
            try:
                self.model_json = response.json()
            except Exception:  # noqa: BLE001
                self.model_json = None

    def csp_console(self) -> list[str]:
        """CSP messages of the enforced policy (a --report-only policy's say so and are left out)."""
        return [c for c in self.console
                if ("Content Security Policy" in c or "Content-Security-Policy" in c)
                and "report-only" not in c.lower() and "[report only]" not in c.lower()]


def wait_quiet(watch: PageWatch, recorder: Recorder) -> float:
    start = time.monotonic()
    while time.monotonic() - start < QUIET_MAX_SECONDS:
        time.sleep(0.5)
        last = max(watch.last, recorder.last)
        if time.monotonic() - last >= QUIET_SECONDS:
            break
    return round(time.monotonic() - start, 1)


def wait_rendered(page) -> tuple[str, float]:
    started = time.monotonic()
    try:
        page.wait_for_function(
            "() => { const p = document.getElementById('armory-model');"
            " return p && ['rendered', 'unavailable', 'off', 'nowebgl'].includes(p.dataset.state); }",
            timeout=RENDER_TIMEOUT_MS + 15_000,  # model API + 30 s watchdog
            polling=100,
        )
    except Exception:  # noqa: BLE001 - reported as the state below
        pass
    state = page.evaluate("() => (document.getElementById('armory-model') || {dataset: {}}).dataset.state")
    return state, round(time.monotonic() - started, 2)


def model_shot(page) -> bytes:
    return page.locator("#armory-model").screenshot()


def run_character(browser, base, guid, recorder, proxy, res: Results, out: Path, expect_growth: bool) -> dict:
    name = NAMES[guid]
    ctx = browser.new_context(viewport={"width": 1400, "height": 1000})
    ctx.add_init_script(INIT_JS)
    page = ctx.new_page()
    watch = PageWatch(page, base)
    up_before = recorder.count()
    cache_before = proxy.status().cache_bytes
    t0 = time.monotonic()
    page.goto(f"{base}/armory/{guid}", wait_until="domcontentloaded")
    state, waited = wait_rendered(page)
    flip = model_shot(page)  # as close to the "rendered" flip as possible
    since_nav = round(time.monotonic() - t0, 2)
    res.check(f"{name}: model panel reaches 'rendered' within 30 s", state == "rendered",
              f"state {state!r} after {since_nav} s")
    quiet = wait_quiet(watch, recorder)
    final = model_shot(page)
    page.screenshot(path=str(out / f"impl-{name}-1400.png"), full_page=True)
    (out / f"impl-{name}-model-at-flip.png").write_bytes(flip)
    (out / f"impl-{name}-model-final.png").write_bytes(final)
    panel = page.evaluate(PANEL_JS)
    flip_stats, final_stats = picture_stats(flip), picture_stats(final)
    res.check(f"{name}: canvas not blank (final)", not_blank(final_stats), json.dumps(final_stats))
    res.check(f"{name}: canvas not blank at the 'rendered' flip (drawn check not early)",
              state == "rendered" and not_blank(flip_stats), json.dumps(flip_stats))
    rendered_at = next((t for s, t in panel["states"] if s == "rendered"), None)
    body = max(panel["m2"], key=lambda e: e[2] or 0, default=None)  # the body .m2 is by far the largest
    if rendered_at is not None and body is not None:
        res.check(f"{name}: 'rendered' only after the body model arrived",
                  rendered_at >= body[1], f"body {body[0]} done at {body[1]:.0f} ms, rendered at {rendered_at:.0f} ms")
    res.check(f"{name}: exactly one canvas in the panel", panel["canvases"] == 1, f"{panel['canvases']}")
    cr, br = panel["canvasRect"], panel["boxRect"]
    if cr and br:
        res.check(f"{name}: canvas fills its container at 1400 px",
                  abs(cr["w"] - br["w"]) <= 1 and abs(cr["h"] - br["h"]) <= 1 and abs(cr["x"] - br["x"]) <= 1,
                  f"canvas {cr}, box {br}, attr {panel['canvasAttr']}")
    res.check(f"{name}: no request outside the admin", not watch.outside, f"{watch.outside[:5]}")
    enforced = [v for v in panel["csp"] if v["disposition"] == "enforce"]
    report = [v for v in panel["csp"] if v["disposition"] != "enforce"]
    res.check(f"{name}: zero CSP violations (listener)", not enforced, json.dumps(enforced)[:600])
    res.check(f"{name}: zero CSP messages (console)", not watch.csp_console(), f"{watch.csp_console()[:3]}")
    if report:
        res.info(f"{name}: report-only violations", json.dumps(report)[:1500])
    model = watch.model_json or {}
    res.check(f"{name}: model API has no unresolved and no pending slot",
              model.get("unresolved") == [] and model.get("pending") == [],
              f"unresolved {model.get('unresolved')}, pending {model.get('pending')}, reason {model.get('reason')}")
    res.check(f"{name}: no 'no 3D' mark on the paper-doll", panel["no3d"] == 0, f"{panel['no3d']}")
    tabard = has_tabard(model.get("items"))
    toggle = panel["hideTabard"] or {}
    res.check(f"{name}: 'Hide tabard' is {'enabled and ticked' if tabard else 'disabled and unticked'} "
              f"({'a' if tabard else 'no'} tabard in the model answer)",
              toggle.get("disabled") is (not tabard) and toggle.get("checked") is tabard, json.dumps(toggle))
    first = (panel["given"] or {}).get("items")
    res.check(f"{name}: the first viewer was given no tabard (Hide tabard is ticked by default)",
              panel["given"] is not None and not has_tabard(first), json.dumps(first))
    upstream = recorder.since(up_before)
    bad = [c["url"] for c in upstream if not any(s.fullmatch(c["url"]) for s in UPSTREAM_SHAPES)]
    res.check(f"{name}: every upstream URL has an allow-listed shape", not bad, f"{bad[:5]}")
    growth = proxy.status().cache_bytes - cache_before
    up_bytes = sum(c["bytes"] for c in upstream)
    viewer_bytes = sum(c["bytes"] for c in upstream if c["url"].endswith("viewer.min.js"))
    if expect_growth:
        net = growth - viewer_bytes
        res.check(f"{name}: cache growth ≈ 3–4 MB for a new race/gender (viewer script excluded)",
                  2.5e6 <= net <= 5.0e6, f"{net / 1e6:.2f} MB (+{viewer_bytes / 1e6:.2f} MB viewer)")
    statuses: dict[str, int] = {}
    for c in upstream:
        statuses[str(c["status"])] = statuses.get(str(c["status"]), 0) + 1
    info = {
        "state": state, "seconds_to_rendered": since_nav, "quiet_after_s": quiet,
        "page_requests": len(watch.requests), "blob_urls": watch.blobs,
        "asset_requests": sum(1 for u in watch.requests if "/armory/assets/" in u),
        "upstream_fetches": len(upstream), "upstream_bytes": up_bytes, "upstream_statuses": statuses,
        "cache_growth_bytes": growth, "cache_total_bytes": proxy.status().cache_bytes,
        "model_api": model, "panel": {k: v for k, v in panel.items() if k not in ("states", "m2")},
        "states": panel["states"], "m2": panel["m2"], "flip": flip_stats, "final": final_stats,
        "console": watch.console[:40], "failed": watch.failed[:40],
        "upstream": upstream,
    }
    res.info(f"{name}: numbers", f"{len(upstream)} upstream fetches, {up_bytes / 1e6:.2f} MB upstream, "
             f"cache +{growth / 1e6:.2f} MB = {info['cache_total_bytes'] / 1e6:.2f} MB, "
             f"{info['page_requests']} page requests, rendered after {since_nav} s, statuses {statuses}")
    ctx.close()
    return info


def run_extras(browser, base, recorder, proxy, res: Results, out: Path) -> dict:
    """Vegivaca: sheath toggle (destroy + re-create), lazy icons in hidden panels, the spike compare."""
    data: dict = {}
    ctx = browser.new_context(viewport={"width": 1400, "height": 1000})
    ctx.add_init_script(INIT_JS)
    page = ctx.new_page()
    watch = PageWatch(page, base)
    page.goto(f"{base}/armory/2506", wait_until="domcontentloaded")
    state, _ = wait_rendered(page)
    wait_quiet(watch, recorder)

    # Lazy icons in the hidden Glyphs panel / the hidden second spec: still <img> before opening.
    # Only cells with an icon: an empty glyph socket is a silhouette by design (no <img>).
    icon_js = """(sel) => Array.from(document.querySelectorAll(
          sel + ' .glyph:not(.glyph--empty) .glyph-icon, ' + sel + ' .talent'))
        .map((cell) => { const img = cell.querySelector('img[data-armory-icon]');
          const empty = cell.querySelector('.armory-icon--empty');
          return img ? { src: img.getAttribute('src').replace(/^.*icons\\//, ''), complete: img.complete,
                         natural: img.naturalWidth } : { empty: Boolean(empty) }; })"""
    before_glyphs = page.evaluate(icon_js, "#armory-panel-glyphs")
    before_spec2 = page.evaluate(icon_js, "#armory-spec-1")
    page.click("#armory-tab-glyphs")
    time.sleep(3)
    after_glyphs = page.evaluate(icon_js, "#armory-panel-glyphs")
    page.screenshot(path=str(out / "impl-vegivaca-glyphs-1400.png"), full_page=True)
    page.click("#armory-tab-talents")
    page.click("#armory-spec-tab-1")
    time.sleep(3)
    after_spec2 = page.evaluate(icon_js, "#armory-spec-1")
    data["icons"] = {"glyphs_before": before_glyphs, "glyphs_after": after_glyphs,
                     "spec2_before": before_spec2, "spec2_after": after_spec2}
    hidden_ok = all("src" in e for e in before_glyphs + before_spec2 if e)
    res.check("lazy icons in hidden panels are still <img> before their tab opens", hidden_ok,
              json.dumps({"glyphs": before_glyphs, "spec2": before_spec2})[:600])
    real = [e for e in after_glyphs if e.get("src") == REAL_ICON + ".jpg"]
    res.check(f"glyph tab opened: the real icon ({REAL_ICON}) loaded and was kept",
              bool(real) and all(e["natural"] > 0 for e in real), json.dumps(after_glyphs)[:600])
    missing_left = [e for e in after_glyphs if e.get("src") == MISSING_ICON + ".jpg"]
    res.check("glyph tab opened: the missing icon became the empty silhouette", not missing_left,
              json.dumps(after_glyphs)[:600])
    spec2_missing = [e for e in after_spec2 if e.get("src") == MISSING_ICON + ".jpg"]
    spec2_real = [e for e in after_spec2 if "src" in e]
    res.check("second spec opened: missing talent icons swapped, existing ones kept",
              not spec2_missing and all(e["natural"] > 0 for e in spec2_real), json.dumps(after_spec2)[:600])

    # Sheath toggle: destroy() + a fresh viewer, still one canvas, still renders.
    before = page.evaluate(PANEL_JS)
    old_canvas = page.evaluate_handle("() => document.querySelector('#armory-model canvas')")
    page.click("#armory-sheath")
    time.sleep(0.3)
    state2, waited = wait_rendered(page)
    wait_quiet(watch, recorder)
    after = page.evaluate(PANEL_JS)
    old_attached = page.evaluate("(c) => Boolean(c && c.isConnected)", old_canvas)
    old_destroyed = page.evaluate("() => { const v = window.__check.viewers; return v.length > 1"
                                  " && v[v.length - 2].container === null && v[v.length - 2].options === null; }")
    sheathed = model_shot(page)
    (out / "impl-vegivaca-sheathed-model.png").write_bytes(sheathed)
    page.screenshot(path=str(out / "impl-vegivaca-sheathed-1400.png"), full_page=True)
    res.check("viewer.destroy() exists", before["hasDestroy"], "")
    res.check("sheath toggle: a new viewer, the old one destroyed and its canvas removed",
              after["viewers"] == before["viewers"] + 1 and old_destroyed and not old_attached,
              f"viewers {before['viewers']} -> {after['viewers']}, old destroyed {old_destroyed}, "
              f"old canvas attached {old_attached}")
    res.check("sheath toggle: exactly one canvas and rendered again", after["canvases"] == 1 and state2 == "rendered",
              f"canvases {after['canvases']}, state {state2} after {waited} s")
    res.check("sheath toggle: the re-created model is not blank", not_blank(picture_stats(sheathed)),
              json.dumps(picture_stats(sheathed)))
    enforced = [v for v in after["csp"] if v["disposition"] == "enforce"]
    res.check("sheath toggle: still zero CSP violations", not enforced, json.dumps(enforced)[:400])
    res.check("sheath toggle: no request outside the admin", not watch.outside, f"{watch.outside[:3]}")
    data["sheath"] = {"before": before, "after": after}

    page.click("#armory-sheath")  # back to the default
    wait_rendered(page)
    wait_quiet(watch, recorder)

    # Hide tabard (ticked by default): the first viewer was given the items minus the tabard
    # (19), drawn without the grey Guild Tabard; unticking re-creates it with the same items
    # plus the tabard. The paper-doll keeps its tabard slot. Then together with the sheath.
    hidden = page.evaluate(PANEL_JS)
    no_tabard = model_shot(page)
    time.sleep(1)
    no_tabard2 = model_shot(page)  # the idle animation's own change: the noise floor
    (out / "impl-vegivaca-hide-tabard-model.png").write_bytes(no_tabard)
    page.screenshot(path=str(out / "impl-vegivaca-hide-tabard-1400.png"), full_page=True)
    page.click("#armory-hide-tabard")  # untick: the tabard is drawn again
    time.sleep(0.3)
    state3, waited3 = wait_rendered(page)
    wait_quiet(watch, recorder)
    shown = page.evaluate(PANEL_JS)
    with_tabard = model_shot(page)
    (out / "impl-vegivaca-tabard-shown-model.png").write_bytes(with_tabard)
    given0, given1 = (hidden["given"] or {}).get("items", []), (shown["given"] or {}).get("items", [])
    res.check("hide tabard: ticked by default, Vegivaca's viewer was given no tabard",
              (hidden["hideTabard"] or {}).get("checked") is True and not has_tabard(given0),
              f"{json.dumps(hidden['hideTabard'])} {json.dumps(given0)}")
    res.check("hide tabard unticked: a new viewer, given the same items plus the tabard only",
              shown["viewers"] == hidden["viewers"] + 1 and has_tabard(given1)
              and given0 == [i for i in given1 if i[0] != 19],
              f"viewers {hidden['viewers']} -> {shown['viewers']}; {json.dumps(given1)}")
    res.check("hide tabard unticked: exactly one canvas and rendered again, not blank",
              shown["canvases"] == 1 and state3 == "rendered" and not_blank(picture_stats(with_tabard)),
              f"canvases {shown['canvases']}, state {state3} after {waited3} s, "
              f"{json.dumps(picture_stats(with_tabard))}")
    noise, change = region_diff(no_tabard, no_tabard2), region_diff(no_tabard, with_tabard)
    torso0, torso1 = region_stats(with_tabard), region_stats(no_tabard)
    res.check("hide tabard: the torso changed well beyond the idle animation's own change",
              change > max(3 * noise, 8.0), f"diff {change} vs noise {noise}; torso {torso0} -> {torso1}")
    res.check("hide tabard: less plain grey on the torso (the Guild Tabard is gone)",
              torso1["grey_share"] < torso0["grey_share"] * 0.6,
              f"{torso0['grey_share']} -> {torso1['grey_share']}")
    slot = page.evaluate("() => { const s = document.getElementById('slot-18');"
                         " return s ? [s.tagName, s.getAttribute('aria-label')] : null; }")
    res.check("hide tabard: the paper-doll still shows the tabard slot",
              bool(slot) and slot[0] == "BUTTON" and "Guild Tabard" in (slot[1] or ""), json.dumps(slot))
    page.click("#armory-hide-tabard")  # ticked again
    time.sleep(0.3)
    wait_rendered(page)
    page.click("#armory-sheath")  # both toggles together
    time.sleep(0.3)
    state4, _ = wait_rendered(page)
    both = page.evaluate(PANEL_JS)
    given2 = both["given"] or {}
    res.check("hide tabard + sheathe: both apply to the new viewer",
              state4 == "rendered" and given2.get("sheathMain") == 1 and not has_tabard(given2.get("items")),
              f"state {state4}, sheathMain {given2.get('sheathMain')}, items {json.dumps(given2.get('items'))}")
    enforced = [v for v in both["csp"] if v["disposition"] == "enforce"]
    res.check("hide tabard: still zero CSP violations, no request outside the admin",
              not enforced and not watch.outside, f"{json.dumps(enforced)[:300]} {watch.outside[:3]}")
    page.click("#armory-sheath")  # back to the defaults (tabard hidden, weapons drawn)
    time.sleep(0.3)
    wait_rendered(page)
    back = page.evaluate(PANEL_JS)
    given3 = back["given"] or {}
    res.check("back to the defaults: the tabard hidden again, the weapons unsheathed",
              given3.get("items") == given0 and given3.get("sheathMain") == 0, json.dumps(given3))
    data["hide_tabard"] = {"hidden": hidden["given"], "shown": shown["given"], "both": given2,
                           "back": given3, "noise": noise, "change": change,
                           "torso": [torso0, torso1], "slot18": slot}
    wait_quiet(watch, recorder)

    # Mouse drag rotates (desktop).
    box = page.locator("#armory-model .armory-model-canvas").bounding_box()
    az0 = page.evaluate(PANEL_JS)["azimuth"]
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.mouse.down()
    for i in range(1, 11):
        page.mouse.move(box["x"] + box["width"] / 2 + 15 * i, box["y"] + box["height"] / 2)
        time.sleep(0.03)
    page.mouse.up()
    time.sleep(1)
    az1 = page.evaluate(PANEL_JS)["azimuth"]
    res.check("mouse drag rotates the model", az0 is not None and az1 is not None and abs(az1 - az0) > 0.2,
              f"azimuth {az0} -> {az1}")
    ctx.close()
    return data


def run_mobile(browser, base, guid, recorder, res: Results, out: Path, touch_check: bool) -> dict:
    name = NAMES[guid]
    ctx = browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2,
                              is_mobile=True, has_touch=True)
    ctx.add_init_script(INIT_JS)
    page = ctx.new_page()
    watch = PageWatch(page, base)
    page.goto(f"{base}/armory/{guid}", wait_until="domcontentloaded")
    state, waited = wait_rendered(page)
    wait_quiet(watch, recorder)
    page.screenshot(path=str(out / f"impl-{name}-390.png"), full_page=True)
    panel = page.evaluate(PANEL_JS)
    res.check(f"{name} @390: rendered", state == "rendered", f"{state} after {waited} s")
    cr, br = panel["canvasRect"], panel["boxRect"]
    res.check(f"{name} @390: canvas fills its container",
              bool(cr and br) and abs(cr["w"] - br["w"]) <= 1 and abs(cr["h"] - br["h"]) <= 1,
              f"canvas {cr}, box {br}, panel {panel['panelRect']}")
    res.check(f"{name} @390: no request outside the admin", not watch.outside, f"{watch.outside[:3]}")
    enforced = [v for v in panel["csp"] if v["disposition"] == "enforce"]
    res.check(f"{name} @390: zero CSP violations", not enforced, json.dumps(enforced)[:400])
    data = {"panel": panel}
    if touch_check:
        page.evaluate("() => document.getElementById('armory-model').scrollIntoView({block: 'center'})")
        time.sleep(0.5)
        box = page.locator("#armory-model .armory-model-canvas").bounding_box()
        cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        scroll0 = page.evaluate("() => window.scrollY")
        az0, ze0 = panel["azimuth"], panel["zenith"]
        cdp = ctx.new_cdp_session(page)

        def touch(kind, x, y):
            points = [] if kind == "touchEnd" else [{"x": x, "y": y, "id": 1}]
            cdp.send("Input.dispatchTouchEvent", {"type": kind, "touchPoints": points})

        touch("touchStart", cx, cy)
        for i in range(1, 13):  # left and up: a page scroll would move scrollY
            touch("touchMove", cx - 8 * i, cy - 10 * i)
            time.sleep(0.03)
        touch("touchEnd", 0, 0)
        time.sleep(1)
        after = page.evaluate(PANEL_JS)
        scroll1 = page.evaluate("() => window.scrollY")
        touch_action = page.evaluate(
            "() => getComputedStyle(document.querySelector('#armory-model .armory-model-canvas')).touchAction")
        rotated = az0 is not None and after["azimuth"] is not None and abs(after["azimuth"] - az0) > 0.2
        res.check(f"{name} @390 touch: a drag on the canvas rotates the model", rotated,
                  f"azimuth {az0} -> {after['azimuth']}, zenith {ze0} -> {after['zenith']}")
        res.check(f"{name} @390 touch: the page does not scroll", scroll1 == scroll0,
                  f"scrollY {scroll0} -> {scroll1}; canvas box touch-action {touch_action!r}")
        data["touch"] = {"azimuth": [az0, after["azimuth"]], "zenith": [ze0, after["zenith"]],
                         "scroll": [scroll0, scroll1], "touch_action": touch_action}
    ctx.close()
    return data


def run_repeat(browser, base, recorder, res: Results, out: Path) -> dict:
    ctx = browser.new_context(viewport={"width": 1400, "height": 1000})  # cold browser cache
    ctx.add_init_script(INIT_JS)
    page = ctx.new_page()
    watch = PageWatch(page, base)
    before = recorder.count()
    page.goto(f"{base}/armory/2506", wait_until="domcontentloaded")
    state, waited = wait_rendered(page)
    wait_quiet(watch, recorder)
    page.screenshot(path=str(out / "impl-vegivaca-repeat-1400.png"), full_page=True)
    upstream = recorder.since(before)
    res.check("repeat load of /armory/2506 renders", state == "rendered", f"{state} after {waited} s")
    res.check("repeat load of /armory/2506: 0 upstream fetches", not upstream, f"{[c['url'] for c in upstream][:5]}")
    asset = sum(1 for u in watch.requests if "/armory/assets/" in u)
    res.info("repeat load numbers", f"{len(watch.requests)} page requests ({asset} assets, all served from "
             f"the admin's cache), {len(upstream)} upstream")
    ctx.close()
    return {"requests": len(watch.requests), "asset_requests": asset, "upstream": upstream}


ELLIPSIS_JS = r"""
() => Array.from(document.querySelectorAll('.player-row > .char-link')).map((a) => {
  const span = a.querySelector('span');
  const clip = (el) => el.scrollWidth > el.clientWidth;
  return { text: a.textContent.trim(), anchorClipped: clip(a), spanClipped: span ? clip(span) : null,
           anchorOverflow: getComputedStyle(a).textOverflow, spanOverflow: span ? getComputedStyle(span).textOverflow : null,
           spanDisplay: span ? getComputedStyle(span).display : null,
           anchorColor: getComputedStyle(a).color, spanColor: span ? getComputedStyle(span).color : null,
           row: a.parentElement.className };
})
"""


def run_players(browser, base, res: Results, out: Path) -> dict:
    data = {}
    for width in (1400, 390):
        mobile = width == 390
        ctx = browser.new_context(viewport={"width": width, "height": 1000 if not mobile else 844},
                                  device_scale_factor=2 if mobile else 1, is_mobile=mobile, has_touch=mobile)
        page = ctx.new_page()
        watch = PageWatch(page, base)
        page.goto(f"{base}/players", wait_until="domcontentloaded")
        try:
            page.wait_for_selector(".char-link", timeout=15_000)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(1)
        page.screenshot(path=str(out / f"impl-players-{width}.png"), full_page=True)
        cells = page.evaluate(ELLIPSIS_JS)
        data[width] = cells
        res.check(f"/players @{width}: names are armory links", len(cells) >= 3, f"{len(cells)} links")
        res.check(f"/players @{width}: no request outside the admin", not watch.outside, f"{watch.outside[:3]}")
        clipped = [c for c in cells if c["anchorClipped"] or c["spanClipped"]]
        # The ellipsis is drawn in the colour of the box that has text-overflow and clips.
        wrong = [c for c in clipped
                 if not (c["spanClipped"] and c["spanOverflow"] == "ellipsis" and c["spanDisplay"] == "block")
                 and c["anchorColor"] != c["spanColor"]]
        if clipped:
            res.check(f"/players @{width}: a clipped name's '…' is drawn in the name's class colour",
                      not wrong, json.dumps(wrong[:3]))
        else:
            res.info(f"/players @{width}: no clipped name", "")
        if mobile and clipped:
            first = page.locator(".player-row--rank > .char-link").first
            first.screenshot(path=str(out / "impl-players-390-rank-name.png"))
        if not mobile:  # the hover underline still shows on a linked name
            link = page.locator(".player-row--rank > .char-link").first
            link.hover()
            time.sleep(0.3)
            link.locator("xpath=..").screenshot(path=str(out / "impl-players-1400-hover.png"))
        ctx.close()
    return data


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--fx", default="/tmp/fx", help="fixture root (under /tmp)")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--cache", choices=("fresh", "keep", "empty"), default="fresh",
                        help="fresh: clear cached assets but keep the pinned viewer script (default); "
                             "keep: reuse the cache (skips the growth check); empty: delete everything")
    parser.add_argument("--csp", default=None, help="enforce this policy instead of ARMORY_CSP (experiments)")
    parser.add_argument("--report-only", default=None,
                        help="also send this policy as Content-Security-Policy-Report-Only (discovery; "
                             "'strict' = default-src 'self')")
    parser.add_argument("--only", default="", help="comma list of steps: chars,extras,mobile,repeat,players")
    parser.add_argument("--guids", default=",".join(map(str, GUIDS)), help="characters to check (default: all three)")
    parser.add_argument("--no-vendor-check", action="store_true",
                        help="run even when htmx/jquery are placeholders (to prove what breaks)")
    args = parser.parse_args()
    if args.report_only == "strict":
        args.report_only = "default-src 'self'"

    fx = Path(args.fx)
    guard_sandbox(fx)
    static = APP_DIR / "app" / "static"
    if not args.no_vendor_check:
        for name in ("htmx.min.js", "htmx-sse.js", "jquery.min.js"):
            if not vendor_ok(static / name):
                die(f"{static / name} is a placeholder or missing — fetch the real file (README)")
    steps = set(filter(None, args.only.split(","))) or {"chars", "extras", "mobile", "repeat", "players"}
    guids = [int(g) for g in args.guids.split(",") if g]
    if not set(guids) <= set(GUIDS):
        die(f"--guids: only {GUIDS} exist in the fixture")

    paths = prepare_fixtures(fx, args.cache)
    characters = load_characters()
    planted = write_dbc_fixture(paths["client"] / "dbc", characters)
    asgi, proxy, recorder = build_app(paths, characters, planted, args.csp, args.report_only)
    server, thread = start_server(asgi, args.port)
    base = f"http://{HOST}:{args.port}"
    out = paths["out"]
    res = Results()
    import app.main as main

    res.info("policy enforced", main.ARMORY_CSP)
    if args.report_only:
        res.info("policy report-only", args.report_only)
    from playwright.sync_api import sync_playwright

    started = time.monotonic()
    try:
        with sync_playwright() as pw:
            # A dead proxy for everything but loopback: should the CSP ever let a request to
            # another origin through, it is recorded (request event) and goes nowhere.
            browser = pw.chromium.launch(args=CHROMIUM_ARGS, proxy={"server": "http://127.0.0.1:9", "bypass": HOST})
            if "chars" in steps:
                fresh = args.cache != "keep"
                res.data["characters"] = {}
                for guid in guids:
                    res.data["characters"][guid] = run_character(
                        browser, base, guid, recorder, proxy, res, out, expect_growth=fresh)
            if "extras" in steps:
                res.data["extras"] = run_extras(browser, base, recorder, proxy, res, out)
            if "mobile" in steps:
                res.data["mobile"] = {guid: run_mobile(browser, base, guid, recorder, res, out, guid == 2506)
                                      for guid in guids}
            if "repeat" in steps:
                res.data["repeat"] = run_repeat(browser, base, recorder, res, out)
            if "players" in steps:
                res.data["players"] = run_players(browser, base, res, out)
            browser.close()
    finally:
        server.should_exit = True
        thread.join(10)
    upstream = recorder.since(0)
    bad = [c["url"] for c in upstream if not any(s.fullmatch(c["url"]) for s in UPSTREAM_SHAPES)]
    res.check("whole run: every upstream URL has an allow-listed shape", not bad, f"{len(upstream)} fetches")
    res.info("whole run", f"{len(upstream)} upstream fetches, {sum(c['bytes'] for c in upstream) / 1e6:.2f} MB, "
             f"cache {proxy.status().cache_bytes / 1e6:.2f} MB, {time.monotonic() - started:.0f} s")
    res.data["upstream_all"] = upstream
    res.data["checks"] = res.checks
    (out / "armory-check.json").write_text(json.dumps(res.data, indent=1, default=str))
    failed = [c for c in res.checks if c["ok"] is False]
    print(f"\n{len(failed)} failed, {sum(1 for c in res.checks if c['ok'])} passed; details in {out}/armory-check.json")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
