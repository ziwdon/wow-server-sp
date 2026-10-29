"""The Armory character page's markup (spec 5, 7): model panel, paper-doll, tabs, stats note,
footer strip, and that ``armory.js`` and the armory CSS stay on the Armory page.

The DB layer and the DBC store are monkeypatched exactly as in ``test_armory_routes.py`` (whose
helpers this module reuses); the rendered HTML is parsed with a small stdlib tree builder so
the assertions follow the document structure instead of string positions.
"""

from __future__ import annotations

import dataclasses
import json
import re
from html.parser import HTMLParser
from pathlib import Path

from app.services import dbc
from app.services.armory_assets import UpstreamError
from app.services.dbc import Glyph, Spell, Talent, TalentTab
from tests.test_armory_routes import (  # noqa: F401 - _no_network is an autouse fixture
    GUID,
    STATS_DIST,
    _client,
    _collect,
    _footer_text,
    _no_network,
    _proxy,
    _Store,
    _view,
)

ADMIN_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ADMIN_ROOT / "app" / "templates"
STYLESHEET = ADMIN_ROOT / "app" / "static" / "app.css"
SCRIPT = ADMIN_ROOT / "app" / "static" / "armory.js"
ARMORY_TEMPLATES = [
    TEMPLATES / "armory.html",
    TEMPLATES / "partials" / "armory_search.html",
    TEMPLATES / "partials" / "armory_character.html",
    TEMPLATES / "partials" / "armory_footer.html",
]

# Spec 5: left column head, neck, shoulder, back, chest, shirt, tabard, wrist; right column
# hands, waist, legs, feet, finger x2, trinket x2; bottom row main hand, off hand, ranged.
DOLL_LEFT = [0, 1, 2, 14, 4, 3, 18, 8]
DOLL_RIGHT = [9, 5, 6, 7, 10, 11, 12, 13]
DOLL_BOTTOM = [15, 16, 17]

STATS_NOTE = (
    "No stats saved for this character yet. Stats are written at logout and need "
    "PlayerSave.Stats.MinLevel ≥ 1 (Settings → search PlayerSave.Stats). Current value: {}."
)
STATS_COLUMNS = (
    "maxhealth", "maxpower1", "maxpower2", "maxpower3", "maxpower4", "maxpower5", "maxpower6",
    "maxpower7", "strength", "agility", "stamina", "intellect", "spirit", "armor", "resHoly",
    "resFire", "resNature", "resFrost", "resShadow", "resArcane", "blockPct", "dodgePct",
    "parryPct", "critPct", "rangedCritPct", "spellCritPct", "attackPower", "rangedAttackPower",
    "spellPower", "resilience",
)
STATS_ROW = (5000, 0, 1000, 0, 0, 0, 0, 0, 150, 80, 120, 30, 40, 4200, 0, 10, 0, 0, 0, 0,
             5.5, 7.25, 6.0, 12.5, 4.0, 0.0, 1800, 300, 0, 0)

HIS_DESC = "Reduces the cost of your Heroic Strike ability by $s1 rage points."


# --- a tiny DOM ---

_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}


class Node:
    def __init__(self, tag: str, attrs, parent: "Node | None"):
        self.tag = tag
        self.attrs = dict(attrs)
        self.parent = parent
        self.children: list = []  # Node or str

    def text(self) -> str:
        return "".join(c if isinstance(c, str) else c.text() for c in self.children)

    def norm(self) -> str:
        return re.sub(r"\s+", " ", self.text()).strip()

    def iter(self):
        for child in self.children:
            if isinstance(child, Node):
                yield child
                yield from child.iter()

    def classes(self) -> set[str]:
        return set((self.attrs.get("class") or "").split())

    def find_all(self, *, tag=None, cls=None, **attrs) -> list["Node"]:
        found = []
        for node in self.iter():
            if tag is not None and node.tag != tag:
                continue
            if cls is not None and cls not in node.classes():
                continue
            if any(node.attrs.get(k.replace("_", "-")) != v for k, v in attrs.items()):
                continue
            found.append(node)
        return found

    def find(self, **kw) -> "Node | None":
        found = self.find_all(**kw)
        return found[0] if found else None

    def by_id(self, id_: str) -> "Node":
        found = [n for n in self.iter() if n.attrs.get("id") == id_]
        assert len(found) == 1, f"expected exactly one #{id_}, found {len(found)}"
        return found[0]


class _Builder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("#root", {}, None)
        self.cur = self.root

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs, self.cur)
        self.cur.children.append(node)
        if tag not in _VOID:
            self.cur = node

    def handle_startendtag(self, tag, attrs):
        self.cur.children.append(Node(tag, attrs, self.cur))

    def handle_endtag(self, tag):
        node = self.cur
        while node is not self.root and node.tag != tag:
            node = node.parent
        if node is not self.root:
            self.cur = node.parent

    def handle_data(self, data):
        self.cur.children.append(data)


def parse(html: str) -> Node:
    builder = _Builder()
    builder.feed(html)
    builder.close()
    return builder.root


# --- fixtures ---


class _RichStore(_Store):
    """``_Store`` plus spell descriptions and icons, a third tree with an unlearned talent,
    and no icon for display id 300 (the main hand)."""

    def __init__(self, missing=()):
        super().__init__(missing)
        self._talents[3] = Talent(3, 163, 1, 2, (12298,))
        self._spells.update({
            12282: Spell(12282, "Improved Heroic Strike", "Rank 1", 7, HIS_DESC),
            12663: Spell(12663, "Improved Heroic Strike", "Rank 2", 7, HIS_DESC),
            12321: Spell(12321, "Booming Voice", "Rank 1", 8, "Increases the area of $s1."),
            12298: Spell(12298, "Shield Specialization", "Rank 1", 9, "Increases block."),
            58096: Spell(58096, "Glyph of Bloodthirst", "", 10, ""),
            58388: Spell(58388, "Glyph of Battle", "", 0, ""),
        })
        self._glyphs[490] = Glyph(490, 58388, False)
        self._icons = {7: "ability_rogue_ambush", 8: "spell_nature_purge", 9: "inv_shield_06",
                       10: "inv_glyph_majorwarrior"}

    def spell_icon(self, id_):
        return self._icons.get(id_, "")

    def item_icon(self, display_id):
        return "" if display_id == 300 else f"inv_{display_id}"


def _page(tmp_path, monkeypatch, view=None, *, store=None, dist: str = "") -> tuple[str, Node]:
    client = _client(tmp_path, monkeypatch, worldserver_dist=dist)
    monkeypatch.setattr(dbc, "store_from_env", lambda: store or _RichStore())
    _collect(monkeypatch, view if view is not None else _view())
    resp = client.get(f"/armory/{GUID}")
    assert resp.status_code == 200
    return resp.text, parse(resp.text)


def _two_specs(active: int = 1):
    return dataclasses.replace(
        _view(spec_count=2, specs=({12663}, {12321}), glyphs=((489, 0, 490, 0, 0, 0), (0,) * 6)),
        active_spec=active,
    )


def _armory_css() -> str:
    css = STYLESHEET.read_text()
    assert "/* Armory */" in css, "app.css needs its /* Armory */ section"
    return css[css.index("/* Armory */"):]


# --- the character page ---


def test_character_page_has_model_panel_paperdoll_tabs_and_blob(tmp_path, monkeypatch):
    html, doc = _page(tmp_path, monkeypatch)

    model = doc.by_id("armory-model")
    assert model.attrs["data-guid"] == str(GUID)
    assert model.attrs["data-model-api"] == f"/api/armory/{GUID}/model"
    assert model.attrs["data-state"] == "loading"  # armory.js moves it on
    assert "Fetching model…" in model.norm()
    assert model.find(cls="armory-model-canvas") is not None  # the viewer's container
    assert model.find(cls="armory-spinner") is not None
    sheath = doc.by_id("armory-sheath")
    assert sheath.tag == "input" and sheath.attrs["type"] == "checkbox"
    assert "Sheathe weapons" in sheath.parent.norm()

    doll = doc.find(cls="paperdoll")
    assert doll is not None
    head = doc.by_id("slot-0")
    assert head.tag == "button" and head.attrs["type"] == "button"
    assert head.attrs["data-tip-slot"] == "0"
    img = head.find(tag="img")
    assert img.attrs["src"] == "/armory/assets/icons/inv_100.jpg"
    assert "data-armory-icon" in img.attrs
    frame = head.find(cls="slot-icon")
    assert "border-color: #a335ee" in frame.attrs["style"]  # epic (quality 4)
    assert "Item 0" in head.norm()

    main_hand = doc.by_id("slot-15")  # filled, but ItemDisplayInfo has no icon for it
    assert main_hand.tag == "button"
    assert main_hand.find(tag="img") is None
    assert main_hand.find(cls="armory-icon--empty") is not None

    shoulder = doc.by_id("slot-2")  # nothing equipped: the silhouette
    assert shoulder.tag == "div" and "slot--empty" in shoulder.classes()
    assert shoulder.find(tag="img") is None
    assert "Shoulder" in shoulder.norm()

    assert "Average item level: 60" in doll.parent.norm()

    tooltip = doc.by_id("armory-tooltip")
    assert tooltip.attrs["role"] == "tooltip" and "hidden" in tooltip.attrs

    tabs = doc.find(role="tablist", cls="armory-tabs")
    assert [t.norm() for t in tabs.find_all(role="tab")] == ["Talents", "Glyphs", "Stats"]

    blob = json.loads(doc.by_id("armory-data").text())
    assert doc.by_id("armory-data").attrs["type"] == "application/json"
    assert blob["guid"] == GUID
    assert set(blob["tooltips"]) == {"0", "1", "3", "4", "15"}
    assert blob["itemsBySlot"]["0"]["icon"] == "inv_100"

    scripts = [s.attrs.get("src", "") for s in doc.find_all(tag="script")]
    armory = next(i for i, src in enumerate(scripts) if src.startswith("/static/armory.js?v="))
    # The viewer needs jQuery (Task 13's browser run); both deferred, so jQuery runs first.
    assert scripts.index("/static/jquery.min.js") < armory
    tags = {s.attrs.get("src"): s for s in doc.find_all(tag="script") if s.attrs.get("src")}
    assert "defer" in tags["/static/jquery.min.js"].attrs


def test_paperdoll_slot_order_matches_spec(tmp_path, monkeypatch):
    html, doc = _page(tmp_path, monkeypatch)

    order = [int(n) for n in re.findall(r'id="slot-(\d+)"', html)]
    assert order == DOLL_LEFT + DOLL_RIGHT + DOLL_BOTTOM
    assert sorted(order) == list(range(19))
    for cls, slots in (("paperdoll-left", DOLL_LEFT), ("paperdoll-right", DOLL_RIGHT),
                       ("paperdoll-bottom", DOLL_BOTTOM)):
        column = doc.find(cls=cls)
        ids = [n.attrs["id"] for n in column.iter() if n.attrs.get("id", "").startswith("slot-")]
        assert ids == [f"slot-{n}" for n in slots], cls


def test_icons_only_for_non_empty_icon_names(tmp_path, monkeypatch):
    html, doc = _page(tmp_path, monkeypatch)

    srcs = [img.attrs["src"] for img in doc.find_all(tag="img") if "data-armory-icon" in img.attrs]
    assert srcs  # paper-doll, talents, glyphs
    for src in srcs:
        assert re.fullmatch(r"/armory/assets/icons/[a-z0-9_-]{1,64}\.jpg", src), src
    assert "/armory/assets/icons/.jpg" not in html


def test_armory_templates_have_no_inline_script_or_handlers():
    for path in ARMORY_TEMPLATES:
        source = path.read_text()
        assert not re.search(r"\son[a-z]+\s*=", source), f"inline event handler in {path.name}"
        for tag in re.findall(r"<script\b[^>]*>", source):
            assert 'type="application/json"' in tag or "src=" in tag, f"inline script in {path.name}: {tag}"
        assert "javascript:" not in source


def test_stats_tab_note_when_row_missing_names_the_setting_and_current_value(tmp_path, monkeypatch):
    html, doc = _page(tmp_path, monkeypatch, dist=STATS_DIST)

    panel = doc.by_id("armory-panel-stats")
    assert panel.norm() == STATS_NOTE.format("0")
    codes = [c.norm() for c in panel.find_all(tag="code")]
    assert codes == ["PlayerSave.Stats.MinLevel", "PlayerSave.Stats"]
    assert panel.find(tag="dl") is None


def test_stats_note_says_unset_when_the_key_is_not_indexed(tmp_path, monkeypatch):
    html, doc = _page(tmp_path, monkeypatch)

    assert doc.by_id("armory-panel-stats").norm() == STATS_NOTE.format("unset")


def test_stats_tab_shows_as_of_logout_when_present(tmp_path, monkeypatch):
    view = _view(stats_row=STATS_ROW, stats_columns=STATS_COLUMNS)
    html, doc = _page(tmp_path, monkeypatch, view)

    panel = doc.by_id("armory-panel-stats")
    text = panel.norm()
    assert "as of last logout (2027-01-15 08:00 UTC)" in text
    assert "No stats saved" not in text
    rows = [(dt.norm(), dt.parent.find(tag="dd").norm()) for dt in panel.find_all(tag="dt")]
    assert rows[:3] == [("Health", "5000"), ("Rage", "100"), ("Strength", "150")]  # warrior: rage
    assert ("Dodge", "7.25%") in rows
    assert ("Attack power", "1800") in rows
    assert len(rows) == 24  # health, power, 22 saved stats


def test_talents_two_specs_mark_active(tmp_path, monkeypatch):
    html, doc = _page(tmp_path, monkeypatch, _two_specs(active=1))

    panel = doc.by_id("armory-panel-talents")
    sub = panel.find(role="tablist")
    tabs = sub.find_all(role="tab")
    assert [t.norm() for t in tabs] == ["Spec 1", "Spec 2 (active)"]
    assert [t.attrs["aria-selected"] for t in tabs] == ["false", "true"]
    spec1 = doc.by_id(tabs[0].attrs["aria-controls"])
    spec2 = doc.by_id(tabs[1].attrs["aria-controls"])
    assert "hidden" in spec1.attrs and "hidden" not in spec2.attrs  # the active spec shows first

    assert "2 / 0 / 0" in spec1.find(cls="talent-summary").norm()
    assert "0 / 1 / 0" in spec2.find(cls="talent-summary").norm()
    assert "Arms / Fury / Protection" in spec1.find(cls="talent-summary").norm()
    trees = spec1.find_all(cls="talent-tree")
    assert len(trees) == 3
    assert [t.norm() for t in spec1.find_all(cls="talent-tree-title")] == ["Arms 2", "Fury 0", "Protection 0"]

    his = trees[0].find(tag="button")
    assert his.attrs["data-tip-title"] == "Improved Heroic Strike"
    assert his.attrs["data-tip-rank"] == "Rank 2/2"
    assert his.attrs["data-tip-desc"] == HIS_DESC  # verbatim, $-placeholders kept
    assert "grid-row: 1" in his.attrs["style"] and "grid-column: 1" in his.attrs["style"]
    assert "talent--unlearned" not in his.classes() and "talent--maxed" in his.classes()
    assert his.find(tag="img").attrs["src"] == "/armory/assets/icons/ability_rogue_ambush.jpg"
    assert his.find(cls="talent-rank").norm() == "2/2"

    voice = trees[1].find(tag="button")  # Booming Voice: learned only in spec 2
    assert "talent--unlearned" in voice.classes()
    assert voice.find(cls="talent-rank").norm() == "0/1"
    shield = trees[2].find(tag="button")
    assert "grid-row: 2" in shield.attrs["style"] and "grid-column: 3" in shield.attrs["style"]

    voice2 = spec2.find_all(cls="talent-tree")[1].find(tag="button")
    assert "talent--unlearned" not in voice2.classes()


def test_talents_spec_one_active_and_single_spec_has_no_subtabs(tmp_path, monkeypatch):
    html, doc = _page(tmp_path, monkeypatch, _two_specs(active=0))
    tabs = doc.by_id("armory-panel-talents").find(role="tablist").find_all(role="tab")
    assert [t.norm() for t in tabs] == ["Spec 1 (active)", "Spec 2"]

    html, doc = _page(tmp_path, monkeypatch, _view(specs=({12282}, set())))
    panel = doc.by_id("armory-panel-talents")
    assert panel.find(role="tablist") is None
    assert len(panel.find_all(cls="talent-spec")) == 1
    assert "1 / 0 / 0" in panel.find(cls="talent-summary").norm()


def test_talents_without_reference_data_say_why(tmp_path, monkeypatch):
    class NoTalents(_RichStore):
        def talents(self):
            return {}

        def talent_tabs(self):
            return {}

    html, doc = _page(tmp_path, monkeypatch, store=NoTalents(missing=["Talent.dbc"]))

    panel = doc.by_id("armory-panel-talents")
    assert panel.find(cls="talent-tree") is None
    assert "Talent data is unavailable" in panel.norm()


def test_glyphs_tab_shows_three_major_three_minor_with_empty_sockets(tmp_path, monkeypatch):
    html, doc = _page(tmp_path, monkeypatch, _two_specs(active=1))

    panel = doc.by_id("armory-panel-glyphs")
    specs = panel.find_all(cls="glyph-spec")
    assert [s.find(cls="glyph-spec-title").norm() for s in specs] == ["Spec 1", "Spec 2 (active)"]
    major, minor = specs[0].find_all(cls="glyph-list")
    assert [g.norm() for g in major.find_all(tag="li")] == ["Glyph of Bloodthirst", "Empty socket", "Empty socket"]
    assert [g.norm() for g in minor.find_all(tag="li")] == ["Empty socket", "Glyph of Battle", "Empty socket"]
    filled = major.find_all(tag="li")[0]
    assert filled.find(tag="img").attrs["src"] == "/armory/assets/icons/inv_glyph_majorwarrior.jpg"
    assert "glyph--empty" in major.find_all(tag="li")[1].classes()
    assert minor.find_all(tag="li")[1].find(tag="img") is None  # its spell has no icon
    empty_major, empty_minor = specs[1].find_all(cls="glyph-list")
    assert len(empty_major.find_all(tag="li")) == 3 and len(empty_minor.find_all(tag="li")) == 3


def test_tabs_are_accessible_buttons_with_tabpanels(tmp_path, monkeypatch):
    html, doc = _page(tmp_path, monkeypatch, _two_specs(active=1))

    section = doc.find(tag="section", cls="armory-character")
    tablists = section.find_all(role="tablist")
    assert len(tablists) == 2  # the main strip and the spec sub-tabs
    for tablist in tablists:
        assert tablist.attrs.get("aria-label")
        tabs = tablist.find_all(role="tab")
        assert len(tabs) >= 2
        assert sum(t.attrs["aria-selected"] == "true" for t in tabs) == 1
        for tab in tabs:
            assert tab.tag == "button" and tab.attrs["type"] == "button"
            panel = doc.by_id(tab.attrs["aria-controls"])
            assert panel.attrs["role"] == "tabpanel"
            assert panel.attrs["aria-labelledby"] == tab.attrs["id"]
            selected = tab.attrs["aria-selected"] == "true"
            assert tab.attrs["tabindex"] == ("0" if selected else "-1")
            assert ("hidden" in panel.attrs) is not selected
    main = section.find(cls="armory-tabs").find_all(role="tab")
    assert main[0].attrs["aria-selected"] == "true"  # Talents first


def test_character_header_and_doll_carry_no_inline_handlers(tmp_path, monkeypatch):
    html, doc = _page(tmp_path, monkeypatch, _two_specs())

    section = doc.find(tag="section", cls="armory-character")
    for node in section.iter():
        assert not [a for a in node.attrs if a.startswith("on")], node.tag
    scripts = section.find_all(tag="script")
    assert [s.attrs.get("type") for s in scripts] == ["application/json"]


# --- assets stay on the Armory page ---


def test_armory_js_and_css_loaded_only_on_armory_pages(tmp_path, monkeypatch):
    html, doc = _page(tmp_path, monkeypatch)
    assert "/static/armory.js" in html and "/static/jquery.min.js" in html
    client = _client(tmp_path, monkeypatch)
    for page in ("/", "/players", "/stats", "/settings", "/backups", "/armory"):
        body = client.get(page).text
        assert "armory.js" not in body, page
        assert "jquery.min.js" not in body, page  # only with a character (and its viewer)

    resp = client.get("/static/armory.js")
    assert resp.status_code == 200
    assert "javascript" in resp.headers["content-type"]

    css = _armory_css()
    before = STYLESHEET.read_text().split("/* Armory */", 1)[0]
    for selector in (".armory-layout", ".paperdoll", ".slot", ".no-3d", ".talent-tree",
                     ".talent--unlearned", ".armory-tooltip", ".armory-footer", ".char-link"):
        assert selector in css, selector
        assert not re.search(re.escape(selector) + r"[\s,{:.\[]", before), f"{selector} outside the Armory section"
    assert re.search(r"\.talent-tree\s*\{[^}]*grid-template-columns:\s*repeat\(4,\s*1fr\)", css)
    assert re.search(r"\.char-link\s*\{\s*color:\s*inherit;\s*text-decoration:\s*none;?\s*\}", css)
    assert re.search(
        r"\.char-link:hover\s*\{\s*text-decoration:\s*underline;\s*cursor:\s*pointer;?\s*\}", css)
    for media in re.findall(r"@media[^{]*", css):
        assert media.strip() in ("@media (max-width: 768px)", "@media (prefers-reduced-motion: reduce)"), media


def test_armory_layout_is_two_columns_on_desktop_and_stacked_on_phone():
    css = _armory_css()

    desktop = css.split("@media", 1)[0]
    assert re.search(r"\.armory-layout\s*\{[^}]*grid-template-columns:\s*minmax\(", desktop)
    phone = css[css.index("@media (max-width: 768px)"):]
    assert re.search(r"\.armory-layout\s*\{[^}]*grid-template-columns:\s*1fr", phone)
    assert re.search(r"\.talent-trees\s*\{[^}]*grid-template-columns:\s*1fr", phone)


def test_armory_js_bootstraps_the_viewer_like_the_spike():
    script = SCRIPT.read_text()

    # Paths through the proxy only (spec 7), never Wowhead.
    assert "'/armory/assets/live/viewer/viewer.min.js'" in script
    assert "'/armory/assets/wrath/'" in script
    assert "zamimg" not in script and "wowhead.com" not in script
    # The viewer needs jQuery (Task 13): a jQuery container, no plain-element fallback, and a
    # clear "did not load" message instead of the viewer's own load-time error.
    assert "container: window.jQuery(box)" in script
    assert "window.jQuery ?" not in script
    assert "'/static/jquery.min.js'" in script and "if (!window.jQuery)" in script
    assert "htmlPrefilter" in script
    # The WH shim the viewer expects (spike page.html / the package's setup.js).
    for needle in ("defaultAnimation: 'Stand'", "getImageExtension", "INVENTORY_TYPE_HEAD: 1",
                   "INVENTORY_TYPE_SHOULDERS: 3", "INVENTORY_TYPE_CHEST: 5", "INVENTORY_TYPE_ROBE: 20",
                   "window.CONTENT_PATH"):
        assert needle in script, needle
    # The proven constructor options.
    for needle in ("new window.ZamModelViewer(", "type: 2", "hd: true", "type: 16",
                   "charCustomization", "sheathMain", "sheathOff", "window.jQuery"):
        assert needle in script, needle
    # States, watchdog and error capture.
    for state in ("loading", "rendered", "unavailable", "off", "nowebgl"):
        assert f"'{state}'" in script, state
    assert "30000" in script
    assert "console.error" in script
    assert "Fetching model…" in script
    assert "3D viewer is switched off" in script
    assert "no-3d" in script and "unresolved" in script and "pending" in script
    # No HTML injection from data: text goes through textContent.
    assert "innerHTML" not in script and "insertAdjacentHTML" not in script


def _foreign_link_rule() -> re.Pattern[str]:
    """armory.js's FOREIGN_LINK regex literal, as a Python pattern (same syntax subset)."""
    match = re.search(r"const FOREIGN_LINK = /(.+)/([a-z]*);", SCRIPT.read_text())
    assert match, "FOREIGN_LINK not found in armory.js"
    assert set(match.group(2)) == {"g", "i"}
    return re.compile(match.group(1).replace("\\/", "/"), re.I)


def test_armory_js_drops_the_viewers_foreign_stylesheet_link_only():
    rule = _foreign_link_rule()
    # The exact string the live viewer appends through $("head").append(...) (Task 13).
    viewer_link = '<link rel="stylesheet" href="//wow.zamimg.com/modelviewer/viewer/viewer.css" type="text/css" />'
    assert rule.sub("", viewer_link) == ""
    for foreign in ('<link rel="stylesheet" href="https://example.com/x.css">',
                    "<LINK href='http://example.com/a.css' rel=stylesheet>",
                    "<link rel=stylesheet href=//cdn.example/x.css>"):
        assert rule.sub("", foreign) == "", foreign
    kept = ['<link rel="stylesheet" href="/static/app.css">', '<div class="x"></div>',
            '<a href="//example.com/">x</a>', '<img src="//example.com/i.png">']
    for html in kept:
        assert rule.sub("", html) == html, html
    assert rule.sub("", "<div></div>" + viewer_link + "<span></span>") == "<div></div><span></span>"


# --- footer strip ---


def test_footer_partial_shows_degraded_sources(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    proxy, fetch = _proxy(tmp_path, monkeypatch)
    monkeypatch.setattr(dbc, "store_from_env", lambda: _Store(missing=["Talent.dbc", "Spell.dbc"]))
    fetch.answer("wrath/m2/1.m2", UpstreamError("timed out"))
    assert proxy.serve("wrath/m2/1.m2").status == 503  # starts the cool-down

    resp = client.get("/api/armory/footer")

    text = _footer_text(resp.text)
    assert "3D viewer: On" in text
    assert "Clear cached model data (keeps the viewer script)" in text
    assert "Viewer script: not fetched yet" in text
    assert "Reference data missing (Talent.dbc, Spell.dbc)" in text
    assert re.search(r"Wowhead unreachable since \d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC", text)
    doc = parse(resp.text)
    assert doc.find(cls="armory-footer-row").attrs["data-viewer-enabled"] == "1"
    warn = doc.find_all(cls="armory-footer-warn")
    assert len(warn) == 2

    off = parse(client.post("/api/armory/settings", data={"enabled": "off"}).text)
    assert off.find(cls="armory-footer-row").attrs["data-viewer-enabled"] == "0"


def test_footer_wrapper_loads_the_strip_on_both_views(tmp_path, monkeypatch):
    html, doc = _page(tmp_path, monkeypatch)
    footer = doc.by_id("armory-footer")
    assert footer.attrs["hx-get"] == "/api/armory/footer" and footer.attrs["hx-trigger"] == "load"

    search_page = parse(_client(tmp_path, monkeypatch).get("/armory").text)
    assert search_page.by_id("armory-footer").attrs["hx-get"] == "/api/armory/footer"
