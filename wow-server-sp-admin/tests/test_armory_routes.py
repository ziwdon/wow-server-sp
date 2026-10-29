"""Armory routes: the page, search, the JSON model API, the asset proxy endpoint, the footer and the CSP.

The DB layer (``armory.search_characters`` / ``armory.collect_character``) is monkeypatched;
every upstream answer comes from a fake ``fetch`` handed to a real ``AssetProxy`` built on
``tmp_path`` (``armory_assets.get_proxy`` is patched to return it, so the resolver sits on it
too). An autouse fixture makes any real connection attempt fail the test.
"""

from __future__ import annotations

import json
import re
import socket
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.services import armory as armory_svc
from app.services import armory_assets, armory_resolver, cloud_backup, dbc
from app.services.armory import (
    Appearance,
    CharacterView,
    EquippedItem,
    Header,
    SearchHit,
    ViewerItem,
)
from app.services.armory_assets import AssetProxy, FetchResult, UpstreamError
from app.services.dbc import Glyph, Spell, Talent, TalentTab

MV = "https://wow.zamimg.com/modelviewer/"
WAIT = 5.0
GUID = 2506
CUSTOMIZATION_PATH = "wrath/meta/charactercustomization/11.json"  # Tauren male: 6 * 2 - 1 + 0

# The brief's initial policy (Task 13 trims it empirically); only these sources may appear.
ALLOWED_CSP_SOURCES = {"'self'", "'unsafe-inline'", "'unsafe-eval'", "data:", "blob:"}


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("a test tried to open a network connection")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)


# --- fixtures ---


def _client(tmp_path, monkeypatch, *, worldserver_dist: str = ""):
    monkeypatch.setenv("AC_STACK_DIR", str(tmp_path))
    monkeypatch.setenv("ADMIN_DATA_DIR", str(tmp_path / "admin-data"))
    monkeypatch.setenv("CLIENT_DATA_DIR", str(tmp_path / "client-data"))  # no dbc/: every file missing
    (tmp_path / "backups").mkdir(exist_ok=True)
    (tmp_path / "logs").mkdir(exist_ok=True)
    dist = tmp_path / "dist"
    dist.mkdir(exist_ok=True)
    for name in [
        "worldserver.conf.dist",
        "playerbots.conf.dist",
        "mod_ahbot.conf.dist",
        "individualProgression.conf.dist",
    ]:
        (dist / name).write_text(worldserver_dist if name == "worldserver.conf.dist" else "")
    from app.state import init_state

    init_state(
        dist_dir=dist,
        admin_yml=tmp_path / "docker-compose.admin.yml",
        override_yml=tmp_path / "docker-compose.override.yml",
        configs_dir=tmp_path / "configs",
        snapshots_dir=tmp_path / "snap",
    )
    from app.main import app

    return TestClient(app)


def _ok(body: bytes = b"{}") -> FetchResult:
    return FetchResult(200, body, None, None)


def _status(code: int) -> FetchResult:
    return FetchResult(code, b"", None, None)


class FakeFetch:
    """Records every call; answers from ``answers`` (url -> FetchResult or exception), else a 404."""

    def __init__(self, answers: dict | None = None, *, gate: str | None = None):
        self.answers = dict(answers or {})
        self.calls: list[str] = []
        self.lock = threading.Lock()
        self.gate = gate  # calls for this url block until ``release`` is set
        self.release = threading.Event()
        self.entered = threading.Event()

    def __call__(self, url: str, *, if_none_match: str | None = None) -> FetchResult:
        with self.lock:
            self.calls.append(url)
        if url == self.gate:
            self.entered.set()
            if not self.release.wait(WAIT):
                raise AssertionError("the test never opened the gate")
        answer = self.answers.get(url, _status(404))
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def answer(self, local_path_or_url: str, result) -> None:
        url = local_path_or_url if local_path_or_url.startswith("https://") else MV + local_path_or_url
        self.answers[url] = result

    def urls(self) -> list[str]:
        with self.lock:
            return list(self.calls)


def _proxy(tmp_path, monkeypatch, answers: dict | None = None, *, gate: str | None = None):
    fetch = FakeFetch(answers, gate=gate)
    proxy = AssetProxy(tmp_path / "admin-data" / "armory", fetch=fetch)
    monkeypatch.setattr(armory_assets, "get_proxy", lambda: proxy)
    return proxy, fetch


APPEARANCE = Appearance(skin=1, face=2, hair_style=3, hair_color=4, facial_style=0)


def _header(**kw) -> Header:
    values = dict(
        guid=GUID, name="Vegivaca", account="CARLOS", is_bot=False, race=6, race_name="Tauren",
        gender=0, class_id=1, class_name="Warrior", class_color="#C69B6D", faction="Horde",
        faction_color="#B30000", level=60, online=False, logout_time=1_800_000_000,
        appearance=APPEARANCE,
    )
    values.update(kw)
    return Header(**values)


def _item(slot, inventory_type, display_id, entry=None, **kw) -> EquippedItem:
    return EquippedItem(slot=slot, entry=entry or 50000 + slot, name=f"Item {slot}",
                        display_id=display_id, inventory_type=inventory_type, **kw)


# head 100 (meta ok), neck (never sent), shirt 6125 (DB 9995 renumbered to 9182 on Wowhead),
# chest 200 (meta ok), main hand 300 (no meta, Wowhead has no such item -> "no 3D")
ITEMS = [
    _item(0, 1, 100, quality=4, item_level=66),
    _item(1, 2, 150),
    _item(3, 4, 9995, entry=6125),
    _item(4, 5, 200, quality=3),
    _item(15, 17, 300, entry=12345),
]


def _view(*, items=None, header=None, spec_count=1, specs=(set(), set()), glyphs=None,
          stats_row=None, stats_columns=()) -> CharacterView:
    return CharacterView(
        header=header or _header(),
        items=list(ITEMS if items is None else items),
        avg_ilvl=60,
        specs=specs,
        spec_count=spec_count,
        active_spec=0,
        glyphs=glyphs or ((0,) * 6, (0,) * 6),
        stats_row=stats_row,
        stats_columns=stats_columns,
    )


def _collect(monkeypatch, result=None, *, error: Exception | None = None):
    calls = []

    def fake(guid, **kw):
        calls.append((guid, kw))
        if error is not None:
            raise error
        return result

    monkeypatch.setattr(armory_svc, "collect_character", fake)
    return calls


def _option(name, option_id, n_choices):
    return {"Name": name, "Id": option_id,
            "Choices": [{"Id": option_id * 100 + i, "Name": ""} for i in range(n_choices)]}


TAUREN_OPTIONS = [
    _option("Skin Color", 1, 5),
    _option("Horn Style", 2, 5),
    _option("Horn Color", 3, 5),
    _option("Hair", 4, 5),
    _option("Face", 5, 5),
    _option("Sparkles", 6, 2),
]


def _item_xml(display_id: int) -> bytes:
    return (b'<?xml version="1.0"?><wowhead><item id="6125"><icon displayId="'
            + str(display_id).encode() + b'">inv_shirt_06</icon></item></wowhead>')


def _model_answers(fetch: FakeFetch, *, customization=None) -> None:
    fetch.answer(CUSTOMIZATION_PATH, customization if customization is not None
                 else _ok(json.dumps({"data": {"Options": TAUREN_OPTIONS}}).encode()))
    fetch.answer("wrath/meta/armor/1/100.json", _ok())
    fetch.answer("wrath/meta/armor/4/9995.json", _status(404))
    fetch.answer("https://www.wowhead.com/wotlk/item=6125&xml", _ok(_item_xml(9182)))
    fetch.answer("wrath/meta/armor/4/9182.json", _ok())
    fetch.answer("wrath/meta/armor/5/200.json", _ok())
    fetch.answer("wrath/meta/item/300.json", _status(404))
    fetch.answer("https://www.wowhead.com/wotlk/item=12345&xml", _status(404))


def _stub_cloud(monkeypatch):
    """The lifespan starts the cloud backup service; these tests need none of it."""

    class Stub:
        async def start(self):
            pass

        async def stop(self):
            pass

    monkeypatch.setattr(cloud_backup, "get_service", lambda: Stub())


def _footer_text(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


# --- the page ---


def test_armory_page_renders_search_and_nav_link_active(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)

    resp = client.get("/armory")

    assert resp.status_code == 200
    body = resp.text
    assert "<title>AzerothCore Admin · Armory</title>" in body
    assert re.search(r'class="nav-link active" href="/armory"', body)
    assert 'class="nav-link " href="/players"' in body
    # Nav order: Players < Armory < Stats (spec 5)
    assert body.index('href="/players"') < body.index('href="/armory"') < body.index('href="/stats"')
    search = re.search(r"<input[^>]*id=\"armory-q\"[^>]*>", body).group(0)
    assert 'name="q"' in search
    assert 'hx-get="/api/armory/search"' in search
    assert 'hx-trigger="keyup changed delay:200ms, search"' in search
    assert 'hx-target="#armory-results"' in search
    assert "autofocus" in search
    assert 'id="armory-results"' in body
    assert 'id="armory-footer"' in body and 'hx-get="/api/armory/footer"' in body
    assert 'id="armory-model"' not in body  # no character loaded


def test_armory_page_prefills_and_runs_the_search_from_q(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)

    body = client.get("/armory", params={"q": 'Ve"gi<'}).text

    search = re.search(r"<input[^>]*id=\"armory-q\"[^>]*>", body).group(0)
    assert 'value="Ve&#34;gi&lt;"' in search  # escaped, never raw
    assert "load" in re.search(r'hx-trigger="([^"]*)"', search).group(1)


def test_armory_character_page_404_for_unknown_guid(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    calls = _collect(monkeypatch, None)

    resp = client.get("/armory/999")

    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("text/html")
    assert calls and calls[0][0] == 999
    assert 'id="armory-q"' in resp.text  # the search box, so the next search is one keystroke away
    assert "No character" in resp.text
    assert 'id="armory-model"' not in resp.text


@pytest.mark.parametrize("guid", ["0", "4294967296", "-1", "abc"])
def test_armory_character_page_rejects_bad_guids_before_the_db(tmp_path, monkeypatch, guid):
    client = _client(tmp_path, monkeypatch)
    calls = _collect(monkeypatch, _view())

    resp = client.get(f"/armory/{guid}")

    assert resp.status_code in (404, 422)
    assert calls == []


def test_armory_character_page_db_error_shows_banner_not_500(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    _collect(monkeypatch, error=RuntimeError("2003: Can't connect to MySQL server"))

    resp = client.get(f"/armory/{GUID}")

    assert resp.status_code == 200
    assert 'class="banner error"' in resp.text
    assert "Can&#39;t connect to MySQL server" in resp.text
    assert 'id="armory-q"' in resp.text
    assert 'id="armory-model"' not in resp.text


def test_armory_character_page_shows_header_model_placeholder_and_blob(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    _proxy_, fetch = _proxy(tmp_path, monkeypatch)
    _model_answers(fetch)
    calls = _collect(monkeypatch, _view())

    resp = client.get(f"/armory/{GUID}")

    assert resp.status_code == 200
    assert fetch.urls() == []  # the page never waits on Wowhead; the model panel asks later
    body = resp.text
    assert calls[0][0] == GUID and set(calls[0][1]) >= {"host", "port", "user", "password"}
    assert "<title>AzerothCore Admin · Armory</title>" in body
    assert re.search(r'class="nav-link active" href="/armory"', body)
    assert re.search(r'<[^>]*style="color: #C69B6D"[^>]*>\s*Vegivaca', body)
    text = _footer_text(body)
    assert "Level 60 Tauren Warrior" in text
    assert "CARLOS" in text
    assert "Last seen 2027-01-15 08:00 UTC" in text
    assert f'data-model-api="/api/armory/{GUID}/model"' in body
    blob = json.loads(re.search(
        r'<script type="application/json" id="armory-data">(.*?)</script>', body, re.S).group(1))
    assert blob["guid"] == GUID
    assert set(blob["tooltips"]) == {"0", "1", "3", "4", "15"}
    assert blob["tooltips"]["0"]["name"] == "Item 0"
    assert blob["tooltips"]["0"]["ilvl"] == 66
    assert blob["itemsBySlot"]["3"]["entry"] == 6125
    assert blob["itemsBySlot"]["15"]["inventoryType"] == 17


def test_armory_character_page_marks_bots_and_online(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    _collect(monkeypatch, _view(header=_header(account="RNDBOT42", is_bot=True, online=True)))

    text = _footer_text(client.get(f"/armory/{GUID}").text)

    assert "RNDBOT42" not in text
    assert " bot " in text
    assert "Online" in text and "Last seen" not in text


def test_armory_blob_cannot_close_its_script_tag(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    evil = _item(0, 1, 100, description="</script><script>alert(1)</script>")
    _collect(monkeypatch, _view(items=[evil]))

    body = client.get(f"/armory/{GUID}").text

    blob = re.search(r'<script type="application/json" id="armory-data">(.*?)</script>', body, re.S).group(1)
    assert json.loads(blob)["tooltips"]["0"]["description"] == "</script><script>alert(1)</script>"
    assert "<script>alert(1)" not in body


class _Store:
    """The DbcStore accessors the page context uses (ids are positional)."""

    def __init__(self, missing=()):
        self._missing = list(missing)
        self._tabs = {161: TalentTab(161, "Arms", 0, 1, 0), 164: TalentTab(164, "Fury", 0, 1, 1),
                      163: TalentTab(163, "Protection", 0, 1, 2)}
        self._talents = {1: Talent(1, 161, 0, 0, (12282, 12663)), 2: Talent(2, 164, 0, 1, (12321,))}
        self._spells = {12282: Spell(12282, "Improved Heroic Strike", "Rank 1", 0, ""),
                        12663: Spell(12663, "Improved Heroic Strike", "Rank 2", 0, ""),
                        12321: Spell(12321, "Booming Voice", "Rank 1", 0, ""),
                        58096: Spell(58096, "Glyph of Bloodthirst", "", 0, "")}
        self._glyphs = {489: Glyph(489, 58096, True)}

    def missing(self):
        return list(self._missing)

    def ok(self):
        return not self._missing

    def load(self):
        pass

    def talents(self):
        return dict(self._talents)

    def talent_tabs(self):
        return dict(self._tabs)

    def spell(self, id_):
        return self._spells.get(id_)

    def spell_icon(self, id_):
        return ""

    def glyph(self, id_):
        return self._glyphs.get(id_)

    def enchant(self, id_):
        return None

    def gem(self, item_id):
        return None

    def random_property(self, id_):
        return None

    def random_suffix(self, id_):
        return None

    def item_set(self, id_):
        return None

    def item_icon(self, display_id):
        return f"inv_{display_id}"

    def rand_prop_points(self, item_level):
        return None


STATS_DIST = (
    "#    PlayerSave.Stats.MinLevel\n"
    "#        Description: Minimum level for saving character stats in the database.\n"
    "#        Default:     0 - (Disabled)\n"
    "\n"
    "PlayerSave.Stats.MinLevel = 0\n"
)


def test_character_context_has_every_tab_per_spec(tmp_path, monkeypatch):
    _client(tmp_path, monkeypatch, worldserver_dist=STATS_DIST)
    from app import main

    store = _Store(missing=["Spell.dbc"])
    monkeypatch.setattr(dbc, "store_from_env", lambda: store)
    _collect(monkeypatch, _view(
        spec_count=2,
        specs=({12663}, {12321}),
        glyphs=((489, 0, 0, 0, 0, 0), (0,) * 6),
    ))

    ctx = main._armory_character_context(GUID)

    assert ctx["view"].header.guid == GUID
    assert set(ctx["tooltips"]) == {0, 1, 3, 4, 15}
    assert ctx["tooltips"][0].icon == "inv_100"
    assert len(ctx["trees"]) == 2  # spec 1 and spec 2
    arms_spec1 = ctx["trees"][0][0]
    assert [t.tab.name for t in ctx["trees"][0]] == ["Arms", "Fury", "Protection"]
    assert arms_spec1.points == 2
    assert ctx["trees"][1][1].points == 1  # Fury, spec 2
    assert len(ctx["glyphs"]) == 2
    assert ctx["glyphs"][0][0].name == "Glyph of Bloodthirst"
    assert ctx["glyphs"][1][0].name is None
    assert ctx["stats"] is None
    assert ctx["stats_note"] == {"min_level": "0"}
    assert ctx["dbc_missing"] == ["Spell.dbc"]
    json.dumps(ctx["armory_data"])  # serialisable as it is


def test_character_context_single_spec_with_stats_has_no_note(tmp_path, monkeypatch):
    _client(tmp_path, monkeypatch)
    from app import main

    monkeypatch.setattr(dbc, "store_from_env", lambda: _Store())
    _collect(monkeypatch, _view(stats_row=(5000, 0, 1000, 0, 0, 0, 0, 0), stats_columns=(
        "maxhealth", "maxpower1", "maxpower2", "maxpower3", "maxpower4", "maxpower5", "maxpower6", "maxpower7")))

    ctx = main._armory_character_context(GUID)

    assert len(ctx["trees"]) == 1 and len(ctx["glyphs"]) == 1
    assert ctx["stats"][:2] == [("Health", "5000"), ("Rage", "100")]  # rage is saved in tenths
    assert ctx["stats_note"] is None


def test_character_context_stats_note_unset_when_the_key_is_not_indexed(tmp_path, monkeypatch):
    _client(tmp_path, monkeypatch)
    from app import main

    monkeypatch.setattr(dbc, "store_from_env", lambda: _Store())
    _collect(monkeypatch, _view())

    assert main._armory_character_context(GUID)["stats_note"] == {"min_level": "unset"}


def test_character_context_none_for_unknown_guid(tmp_path, monkeypatch):
    _client(tmp_path, monkeypatch)
    from app import main

    _collect(monkeypatch, None)

    assert main._armory_character_context(GUID) is None


# --- search ---


def _hit(guid, name, *, is_bot=False, level=60):
    return SearchHit(guid=guid, name=name, level=level, race=6, class_id=1, class_name="Warrior",
                     class_color="#C69B6D", race_name="Tauren", is_bot=is_bot)


def test_search_endpoint_returns_rows_with_bot_tag(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    seen = []

    def fake(q, **kw):
        seen.append((q, kw))
        return [_hit(2506, "Vegivaca"), _hit(4957, "Vegibot", is_bot=True, level=12)]

    monkeypatch.setattr(armory_svc, "search_characters", fake)

    resp = client.get("/api/armory/search", params={"q": "veg"})

    assert resp.status_code == 200
    assert seen[0][0] == "veg" and set(seen[0][1]) >= {"host", "port", "user", "password"}
    rows = re.findall(r'<a[^>]*href="/armory/(\d+)"[^>]*>(.*?)</a>', resp.text, re.S)
    assert [guid for guid, _ in rows] == ["2506", "4957"]
    assert "bot" not in _footer_text(rows[0][1])
    assert "bot" in _footer_text(rows[1][1])
    assert "Lvl 12" in _footer_text(rows[1][1])
    assert 'style="color: #C69B6D"' in rows[0][1]


def test_search_endpoint_short_query_runs_no_sql(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    monkeypatch.setattr(armory_svc, "search_characters", lambda *a, **k: pytest.fail("no SQL for a short query"))

    for q in ("", "v", "  v  "):
        resp = client.get("/api/armory/search", params={"q": q})
        assert resp.status_code == 200
        assert "/armory/" not in resp.text


def test_search_endpoint_no_match_and_db_error_stay_200(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    monkeypatch.setattr(armory_svc, "search_characters", lambda q, **kw: [])
    resp = client.get("/api/armory/search", params={"q": "zzz"})
    assert resp.status_code == 200
    assert "No character" in resp.text

    def down(q, **kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(armory_svc, "search_characters", down)
    resp = client.get("/api/armory/search", params={"q": "zzz"})
    assert resp.status_code == 200
    assert "db down" in resp.text
    assert resp.text.count("<p") == 1  # one line


# --- model API ---


def test_model_api_returns_resolved_items_and_unresolved_slots(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    _proxy_, fetch = _proxy(tmp_path, monkeypatch)
    _model_answers(fetch)
    _collect(monkeypatch, _view())

    resp = client.get(f"/api/armory/{GUID}/model")

    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"
    assert resp.json() == {
        "modelId": 11,
        "options": [
            {"optionId": 1, "choiceId": 101},  # Skin Color = skin 1
            {"optionId": 2, "choiceId": 203},  # Horn Style = hair style 3 (Tauren)
            {"optionId": 3, "choiceId": 304},  # Horn Color = hair colour 4
            {"optionId": 4, "choiceId": 400},  # Hair = facial style 0
            {"optionId": 5, "choiceId": 502},  # Face = face 2
            {"optionId": 6, "choiceId": 600},  # unknown name: first choice
        ],
        "unresolvedOptions": ["Sparkles"],
        "items": [[1, 100], [4, 9182], [5, 200]],
        "unresolved": [15],
        "viewer": {"enabled": True, "scriptReady": False},
        "sheath": False,
    }
    # The neck never reaches the viewer, so nobody asks for its meta.
    assert not any("/150." in url for url in fetch.urls())


def test_model_api_reads_top_level_options_and_reports_a_pinned_viewer(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    proxy, fetch = _proxy(tmp_path, monkeypatch)
    _model_answers(fetch, customization=_ok(json.dumps({"Options": TAUREN_OPTIONS[:1]}).encode()))
    fetch.answer("live/viewer/viewer.min.js", FetchResult(200, b"/* viewer */", '"v1"', None))
    assert proxy.serve("live/viewer/viewer.min.js").status == 200  # pinned on first use
    _collect(monkeypatch, _view(items=[]))

    data = client.get(f"/api/armory/{GUID}/model").json()

    assert data["options"] == [{"optionId": 1, "choiceId": 101}]
    assert data["unresolvedOptions"] == []
    assert data["items"] == [] and data["unresolved"] == []
    assert data["viewer"] == {"enabled": True, "scriptReady": True}
    assert "reason" not in data


@pytest.mark.parametrize(
    "answer, words",
    [
        (_status(404), "not found"),
        (UpstreamError("timed out"), "Wowhead unreachable"),
        (_ok(b"not json"), "unreadable"),
        (_ok(b'{"data": {"Choices": []}}'), "unreadable"),
    ],
)
def test_model_api_without_customization_json_returns_null_options_with_reason(
    tmp_path, monkeypatch, answer, words
):
    client = _client(tmp_path, monkeypatch)
    _proxy_, fetch = _proxy(tmp_path, monkeypatch)
    _model_answers(fetch, customization=answer)
    _collect(monkeypatch, _view(items=[]))

    resp = client.get(f"/api/armory/{GUID}/model")

    assert resp.status_code == 200
    data = resp.json()
    assert data["options"] is None
    assert data["unresolvedOptions"] == []
    assert CUSTOMIZATION_PATH in data["reason"]
    assert words in data["reason"]
    assert data["modelId"] == 11 and data["sheath"] is False


def test_model_api_viewer_off_skips_resolver_and_upstream(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    proxy, fetch = _proxy(tmp_path, monkeypatch)
    _model_answers(fetch)
    proxy.settings.set_enabled(False)
    monkeypatch.setattr(armory_resolver, "get_resolver", lambda: pytest.fail("no resolver while switched off"))
    _collect(monkeypatch, _view())

    resp = client.get(f"/api/armory/{GUID}/model")

    assert resp.status_code == 200
    data = resp.json()
    assert data["viewer"] == {"enabled": False, "scriptReady": False}
    assert data["options"] is None and "switched off" in data["reason"]
    assert data["items"] == [] and data["unresolved"] == []
    assert fetch.urls() == []


def test_model_api_404_for_unknown_guid_and_503_for_db_error(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    _proxy(tmp_path, monkeypatch)
    _collect(monkeypatch, None)
    resp = client.get("/api/armory/77/model")
    assert resp.status_code == 404
    assert resp.json()["detail"]

    _collect(monkeypatch, error=RuntimeError("db down"))
    resp = client.get("/api/armory/77/model")
    assert resp.status_code == 503
    assert "db down" in resp.json()["detail"]

    assert client.get("/api/armory/0/model").status_code == 422


def test_model_api_logs_each_unknown_option_name_once(tmp_path, monkeypatch, caplog):
    client = _client(tmp_path, monkeypatch)
    from app import main

    monkeypatch.setattr(main, "_armory_unknown_options_logged", set())
    _proxy_, fetch = _proxy(tmp_path, monkeypatch)
    _model_answers(fetch)
    _collect(monkeypatch, _view(items=[]))

    with caplog.at_level("WARNING"):
        client.get(f"/api/armory/{GUID}/model")
        client.get(f"/api/armory/{GUID}/model")

    lines = [r.getMessage() for r in caplog.records if "Sparkles" in r.getMessage()]
    assert len(lines) == 1


def test_model_api_resolves_items_with_at_most_four_workers(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    _proxy_, fetch = _proxy(tmp_path, monkeypatch)
    _model_answers(fetch, customization=_status(404))
    items = [_item(slot, 1, 1000 + slot) for slot in (0, 2, 3, 4, 5, 6, 7, 8)]  # eight sent pieces
    _collect(monkeypatch, _view(items=items))
    barrier = threading.Barrier(4, timeout=WAIT)  # only passes when four resolve at once
    active = {"now": 0, "max": 0}
    mu = threading.Lock()

    class Resolver:
        def resolve_items(self, batch, entries):
            with mu:
                active["now"] += 1
                active["max"] = max(active["max"], active["now"])
            try:
                barrier.wait()
                time.sleep(0.02)
            finally:
                with mu:
                    active["now"] -= 1
            assert all(entries[v.slot] == 50000 + v.slot for v in batch)
            return [ViewerItem(v.slot, v.inventory_type, v.display_id + 1) for v in batch], []

    monkeypatch.setattr(armory_resolver, "get_resolver", lambda: Resolver())

    data = client.get(f"/api/armory/{GUID}/model").json()

    assert active["max"] == 4
    assert data["items"] == [[1, 1001 + slot] for slot in (0, 2, 3, 4, 5, 6, 7, 8)]  # slot order kept
    assert data["unresolved"] == []


# --- asset route ---


def test_assets_route_serves_cached_file_with_immutable_cache_control(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    proxy, fetch = _proxy(tmp_path, monkeypatch)
    body = bytes(range(256)) * 20
    fetch.answer("wrath/m2/123.m2", _ok(body))

    first = client.get("/armory/assets/wrath/m2/123.m2")

    assert first.status_code == 200
    assert first.content == body
    assert first.headers["content-type"] == "application/octet-stream"
    assert first.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert first.headers["x-content-type-options"] == "nosniff"
    etag = proxy.serve("wrath/m2/123.m2").etag
    assert first.headers["etag"] == etag  # the proxy's, not Starlette's mtime ETag

    again = client.get("/armory/assets/wrath/m2/123.m2")
    assert again.status_code == 200 and again.content == body
    assert again.headers["etag"] == etag

    revalidated = client.get("/armory/assets/wrath/m2/123.m2", headers={"If-None-Match": etag})
    assert revalidated.status_code == 304
    assert revalidated.content == b""
    assert revalidated.headers["etag"] == etag
    assert fetch.urls() == [MV + "wrath/m2/123.m2"]  # one upstream GET, ever


def test_assets_route_viewer_is_no_cache_and_revalidates(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    _proxy_, fetch = _proxy(tmp_path, monkeypatch)
    fetch.answer("live/viewer/viewer.min.js", FetchResult(200, b"window.ZamModelViewer=1;", '"v1"', None))

    first = client.get("/armory/assets/live/viewer/viewer.min.js")

    assert first.status_code == 200
    assert first.headers["content-type"] == "application/javascript"
    assert first.headers["cache-control"] == "no-cache"
    etag = first.headers["etag"]
    assert client.get("/armory/assets/live/viewer/viewer.min.js",
                      headers={"If-None-Match": f'W/"other", {etag}'}).status_code == 304
    assert client.get("/armory/assets/live/viewer/viewer.min.js",
                      headers={"If-None-Match": '"other"'}).status_code == 200


def test_assets_route_pass_through_when_the_cache_is_unwritable(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    proxy, fetch = _proxy(tmp_path, monkeypatch)
    fetch.answer("wrath/textures/77.webp", _ok(b"RIFFwebp"))

    def refuse(*_a, **_k):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(proxy.cache, "put", refuse)

    resp = client.get("/armory/assets/wrath/textures/77.webp")

    assert resp.status_code == 200
    assert resp.content == b"RIFFwebp"
    assert resp.headers["content-type"] == "image/webp"
    assert resp.headers["etag"]


@pytest.mark.parametrize(
    "path",
    [
        "wrath/m2/%2e%2e/%2e%2e/etc/passwd",
        "wrath/m2/..%2f..%2fetc/passwd",
        "item-displayid/6125",
        "https:/www.wowhead.com/wotlk/item=6125&xml",
        "wrath/meta/charactercustomization/99.json",
        "wrath/m2/abc.m2",
        "icons/INV_SWORD.jpg",
        "",
    ],
)
def test_assets_route_404s_crafted_path_without_fetch(tmp_path, monkeypatch, path):
    client = _client(tmp_path, monkeypatch)
    proxy, fetch = _proxy(tmp_path, monkeypatch)
    called = []
    monkeypatch.setattr(proxy, "fetch_item_xml", lambda entry: called.append(entry))

    resp = client.get("/armory/assets/" + path)

    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("text/plain")
    assert resp.headers["cache-control"] == "no-store"
    assert fetch.urls() == [] and called == []


def test_assets_route_503_when_disabled(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    proxy, fetch = _proxy(tmp_path, monkeypatch)
    fetch.answer("wrath/skin/5.skin", _ok(b"skin"))
    assert proxy.serve("wrath/skin/5.skin").status == 200  # cached while on
    proxy.settings.set_enabled(False)

    miss = client.get("/armory/assets/wrath/skin/6.skin")
    hit = client.get("/armory/assets/wrath/skin/5.skin")

    assert miss.status_code == 503
    assert miss.headers["content-type"].startswith("text/plain")
    assert miss.text == "3D viewer is switched off"
    assert hit.status_code == 200 and hit.content == b"skin"  # hits still serve
    assert fetch.urls() == [MV + "wrath/skin/5.skin"]


def test_assets_route_503_while_wowhead_cools_down(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    _proxy_, fetch = _proxy(tmp_path, monkeypatch)
    fetch.answer("wrath/m2/1.m2", UpstreamError("timed out"))

    first = client.get("/armory/assets/wrath/m2/1.m2")
    second = client.get("/armory/assets/wrath/m2/2.m2")

    assert first.status_code == 503 and "Wowhead unreachable" in first.text
    assert second.status_code == 503
    assert fetch.urls() == [MV + "wrath/m2/1.m2"]  # the cool-down answers without asking


def test_armory_blocking_work_runs_on_its_own_bounded_limiter(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    from app import main

    monkeypatch.setattr(main, "ARMORY_THREAD_TOKENS", 1)
    monkeypatch.setattr(main, "_armory_limiter_obj", None)
    _stub_cloud(monkeypatch)
    _proxy_, fetch = _proxy(tmp_path, monkeypatch, gate=MV + "wrath/m2/1.m2")
    fetch.answer("wrath/m2/1.m2", _ok(b"one"))
    fetch.answer("wrath/m2/2.m2", _ok(b"two"))
    results: dict[str, int] = {}

    def get(path):
        results[path] = client.get("/armory/assets/" + path).status_code

    with client:  # one event loop for every request, as in production
        first = threading.Thread(target=get, args=("wrath/m2/1.m2",))
        first.start()
        try:
            assert fetch.entered.wait(WAIT)
            second = threading.Thread(target=get, args=("wrath/m2/2.m2",))
            second.start()
            time.sleep(0.3)
            # The only token is held by the hung fetch: the second asset waits for it ...
            assert fetch.urls() == [MV + "wrath/m2/1.m2"]
            # ... while every other admin route still answers.
            assert client.get("/healthz").status_code == 200
            assert client.get("/api/armory/footer").status_code == 200
        finally:
            fetch.release.set()
        first.join(WAIT)
        second.join(WAIT)
    assert results == {"wrath/m2/1.m2": 200, "wrath/m2/2.m2": 200}


# --- CSP ---


def _csp_sources(policy: str) -> dict[str, list[str]]:
    out = {}
    for directive in filter(None, (part.strip() for part in policy.split(";"))):
        name, *sources = directive.split()
        out[name] = sources
    return out


def test_csp_header_on_armory_paths_only_and_names_no_third_party_origin(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    _proxy(tmp_path, monkeypatch)
    _collect(monkeypatch, _view())
    monkeypatch.setattr(armory_svc, "search_characters", lambda q, **kw: [])

    armory = [
        client.get("/armory"),
        client.get(f"/armory/{GUID}"),
        client.get("/armory/0"),  # a validation answer too
        client.get("/armory/assets/wrath/m2/abc.m2"),
    ]
    elsewhere = [
        client.get("/"),
        client.get("/players"),
        client.get("/api/armory/search", params={"q": "veg"}),
        client.get("/api/armory/footer"),
        client.get("/static/app.css"),
        client.get("/armoryx"),
    ]

    policies = {resp.headers.get("content-security-policy") for resp in armory}
    assert len(policies) == 1
    policy = policies.pop()
    assert policy
    for resp in elsewhere:
        assert "content-security-policy" not in resp.headers, resp.request.url
    sources = _csp_sources(policy)
    assert sources["default-src"] == ["'self'"]
    assert sources["connect-src"] == ["'self'"]
    for directive, values in sources.items():
        for value in values:
            assert value in ALLOWED_CSP_SOURCES, (directive, value)  # no host, no https:, no *
    assert "http" not in policy and "*" not in policy and "//" not in policy


def test_csp_header_is_not_duplicated_and_gzip_still_applies(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    _proxy_, fetch = _proxy(tmp_path, monkeypatch)
    fetch.answer("wrath/meta/item/5.json", _ok(b'{"a": "' + b"x" * 5000 + b'"}'))

    resp = client.get("/armory/assets/wrath/meta/item/5.json", headers={"Accept-Encoding": "gzip"})

    assert resp.status_code == 200
    assert resp.headers["content-encoding"] == "gzip"
    assert len([k for k in resp.headers.raw if k[0].lower() == b"content-security-policy"]) == 1


# --- footer ---


def test_footer_actions_rerender_partial_and_toggle_persists(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    proxy, fetch = _proxy(tmp_path, monkeypatch)
    fetch.answer("live/viewer/viewer.min.js", FetchResult(200, b"/* v1 */", '"v1"', None))

    resp = client.get("/api/armory/footer")
    assert resp.status_code == 200
    text = _footer_text(resp.text)
    assert "3D viewer: On" in text
    assert "Cached model data: 0 MB" in text
    assert "Viewer script: not fetched yet" in text
    assert 'hx-post="/api/armory/settings"' in resp.text
    assert 'hx-target="#armory-footer"' in resp.text
    assert "<html" not in resp.text  # a fragment

    resp = client.post("/api/armory/settings", data={"enabled": "off"})
    assert resp.status_code == 200
    assert "3D viewer: Off" in _footer_text(resp.text)
    assert json.loads((tmp_path / "admin-data" / "armory" / "armory.json").read_text()) == {"enabled": False}
    assert proxy.settings.enabled() is False
    assert "3D viewer: Off" in _footer_text(client.get("/api/armory/footer").text)

    # R18: no update while switched off; the reason is shown, still 200.
    resp = client.post("/api/armory/viewer/update")
    assert resp.status_code == 200
    assert "switched off" in _footer_text(resp.text)
    assert fetch.urls() == []

    resp = client.post("/api/armory/settings", data={"enabled": "on"})
    assert "3D viewer: On" in _footer_text(resp.text)

    resp = client.post("/api/armory/viewer/update")
    assert resp.status_code == 200
    text = _footer_text(resp.text)
    assert re.search(r"Viewer script: fetched \d{4}-\d{2}-\d{2}", text)
    assert "Revert" not in text  # nothing to revert to yet

    fetch.answer("live/viewer/viewer.min.js", FetchResult(200, b"/* v2 */", '"v2"', None))
    resp = client.post("/api/armory/viewer/update")
    assert 'hx-post="/api/armory/viewer/revert"' in resp.text

    resp = client.post("/api/armory/viewer/revert")
    assert resp.status_code == 200
    assert 'hx-post="/api/armory/viewer/revert"' not in resp.text
    assert proxy.serve("live/viewer/viewer.min.js").path.read_bytes() == b"/* v1 */"

    resp = client.post("/api/armory/viewer/revert")  # nothing left to revert to
    assert resp.status_code == 200
    assert "no previous viewer script" in _footer_text(resp.text)

    fetch.answer("wrath/m2/9.m2", _ok(b"m2"))
    assert proxy.serve("wrath/m2/9.m2").status == 200
    resp = client.post("/api/armory/cache/clear")
    assert resp.status_code == 200
    assert "Cleared 1 cached file" in _footer_text(resp.text)
    assert not (proxy.cache.root / "wrath" / "m2" / "9.m2").exists()
    assert proxy.serve("live/viewer/viewer.min.js").status == 200  # Clear keeps the pinned script


def test_footer_settings_refuses_odd_values_and_reports_unwritable_folder(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    proxy, _fetch = _proxy(tmp_path, monkeypatch)

    resp = client.post("/api/armory/settings", data={"enabled": "maybe"})
    assert resp.status_code == 200
    assert "on or off" in _footer_text(resp.text)
    assert not (tmp_path / "admin-data" / "armory" / "armory.json").exists()

    def refuse(_value):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(proxy.settings, "set_enabled", refuse)
    resp = client.post("/api/armory/settings", data={"enabled": "off"})
    assert resp.status_code == 200
    assert "not writable" in _footer_text(resp.text)
    assert "3D viewer: On" in _footer_text(resp.text)


def test_footer_shows_degraded_sources(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    proxy, fetch = _proxy(tmp_path, monkeypatch)
    monkeypatch.setattr(dbc, "store_from_env", lambda: _Store(missing=["Talent.dbc", "Spell.dbc"]))

    def refuse(*_a, **_k):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(proxy.cache, "put", refuse)
    fetch.answer("https://wow.zamimg.com/images/wow/icons/large/inv_a.jpg", _ok(b"jpg"))
    assert proxy.serve("icons/inv_a.jpg").body == b"jpg"  # pass-through: the cache write failed
    fetch.answer("wrath/m2/1.m2", UpstreamError("timed out"))
    assert proxy.serve("wrath/m2/1.m2").status == 503  # starts the cool-down

    text = _footer_text(client.get("/api/armory/footer").text)

    assert "Talent.dbc, Spell.dbc" in text
    assert re.search(r"Wowhead unreachable since \d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC", text)
    assert "Could not write to the model cache" in text


def test_footer_status_failure_is_a_line_not_a_500(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    proxy, _fetch = _proxy(tmp_path, monkeypatch)

    def boom():
        raise RuntimeError("boom")

    monkeypatch.setattr(proxy, "status", boom)

    resp = client.get("/api/armory/footer")

    assert resp.status_code == 200
    assert "boom" not in resp.text
    assert "admin log" in _footer_text(resp.text)


# --- nav elsewhere, lifespan warm-up ---


def test_players_nav_unchanged_elsewhere(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)

    body = client.get("/players").text

    assert 'class="nav-link active" href="/players"' in body
    assert 'class="nav-link " href="/armory"' in body
    links = re.findall(r'class="nav-link[^"]*" href="([^"]+)"', body)
    assert links == ["/", "/players", "/armory", "/stats", "/progression", "/settings",
                     "/maintenance", "/backups", "/customization"]
    for page in ("/", "/stats", "/settings", "/backups"):
        assert 'class="nav-link " href="/armory"' in client.get(page).text


def test_lifespan_warms_the_dbc_store_in_a_thread_without_blocking(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)

    _stub_cloud(monkeypatch)
    started = threading.Event()
    release = threading.Event()
    seen_threads = []

    class SlowStore:
        def load(self):
            seen_threads.append(threading.current_thread())
            started.set()
            release.wait(WAIT)

        def ok(self):
            self.load()
            return True

    monkeypatch.setattr(dbc, "store_from_env", lambda: SlowStore())

    try:
        with client:  # startup completes while the load is still running
            assert started.wait(WAIT)
            assert client.get("/healthz").status_code == 200
    finally:
        release.set()
    assert seen_threads and seen_threads[0] is not threading.main_thread()
    assert seen_threads[0].daemon


def test_lifespan_survives_a_failing_dbc_warm_up(tmp_path, monkeypatch, caplog):
    client = _client(tmp_path, monkeypatch)

    _stub_cloud(monkeypatch)
    done = threading.Event()

    class BrokenStore:
        def load(self):
            try:
                raise OSError("disk on fire")
            finally:
                done.set()

        def ok(self):
            self.load()

    monkeypatch.setattr(dbc, "store_from_env", lambda: BrokenStore())

    with caplog.at_level("WARNING"), client:
        assert done.wait(WAIT)
        assert client.get("/healthz").status_code == 200
        deadline = time.monotonic() + WAIT
        while not any("warm" in r.getMessage().lower() for r in caplog.records):
            assert time.monotonic() < deadline, "the failed warm-up was never logged"
            time.sleep(0.01)


def test_lifespan_survives_store_construction_failure(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)

    _stub_cloud(monkeypatch)

    def explode():
        raise RuntimeError("no store")

    monkeypatch.setattr(dbc, "store_from_env", explode)

    with client:
        assert client.get("/healthz").status_code == 200
