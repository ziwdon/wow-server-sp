import re
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app
from app.services import banish

PARTIAL = Path(__file__).resolve().parents[1] / "app/templates/partials/banish_page.html"
PAGE = Path(__file__).resolve().parents[1] / "app/templates/progression.html"


def test_progression_page_loads_banish_wrapper_after_raid_unlock():
    t = PAGE.read_text()
    assert 'id="banish-data"' in t and 'hx-get="/api/banish/panel"' in t and 'hx-trigger="load"' in t
    assert t.index('id="raid-unlock-data"') < t.index('id="banish-data"')


def test_partial_describes_intent_and_behaviour():
    t = PARTIAL.read_text()
    assert '<span class="panel-title">Banish</span>' in t
    for phrase in ("full raid", "instantly kills", "achievements", "players, bots or friendly NPCs", "global cooldown", "Blizzard"):
        assert phrase in t


def test_partial_has_status_block_and_install_button():
    t = PARTIAL.read_text()
    assert 'id="banish-install-btn"' in t and 'id="banish-remove-note"' in t
    assert "Install definition" in t and "Update definition" in t and "Reinstall definition" in t
    assert "character_spell WHERE spell = 35182" in t   # removal note: forget from characters first
    assert "no_spell_dbc_table" in t                     # unsupported build branch


def test_partial_has_accessible_modal_and_posts_guid():
    t = PARTIAL.read_text()
    assert 'role="dialog" aria-modal="true"' in t and 'aria-labelledby="banish-confirm-title"' in t
    assert "if (e.key === 'Tab') trapFocus(e);" in t
    assert "if (e.key === 'Escape') { e.preventDefault(); cleanup(false); }" in t
    assert "let confirmationOpen = false;" in t
    for url in ("'/api/banish/install'", "'/api/banish/teach'", "'/api/banish/forget'"):
        assert url in t
    assert "JSON.stringify({guid: selectedChar.guid})" in t
    assert "htmx.ajax('GET', '/api/banish/panel', {target: '#banish-data', swap: 'innerHTML'})" in t


def test_partial_disables_actions_for_online_characters():
    t = PARTIAL.read_text()
    assert "selectedChar.online" in t
    assert "Character must log out before Banish can be taught or removed." in t
    assert "status.definition === 'installed'" in t and "selectedChar.knows" in t


def test_partial_has_no_inline_handlers():
    t = PARTIAL.read_text()
    assert "onclick=" not in t and "hx-on" not in t


# --- the card as the panel route renders it ---------------------------------

CREDS = {"host": "h", "port": 3306, "user": "u", "password": "p"}
ROWS = (
    banish.BanishCharacterRow(101, "CARLOS", "Sariel", 80, False, True),
    banish.BanishCharacterRow(102, "CARLOS", "Thrall", 70, True, False),
)


def _render(monkeypatch, rows=ROWS, **status_kw) -> str:
    base = dict(support="ok", definition="installed", worldserver="active", installed_at=1793610000)
    base.update(status_kw)
    monkeypatch.setattr("app.main.db_credentials", lambda: CREDS)
    monkeypatch.setattr("app.main.banish_svc.collect_characters", lambda **kw: tuple(rows))
    monkeypatch.setattr("app.main.banish_svc.collect_status", lambda **kw: banish.BanishStatus(**base))
    r = TestClient(app).get("/api/banish/panel")
    assert r.status_code == 200
    return r.text


def _install_label(html: str) -> str | None:
    m = re.search(r'<button[^>]*id="banish-install-btn"[^>]*>\s*([^<]*?)\s*</button>', html)
    return m.group(1) if m else None


def test_rendered_definition_line_and_install_label(monkeypatch):
    for definition, line, label in (
        ("missing", "Not installed", "Install definition"),
        ("installed", "Installed (v1)", "Reinstall definition"),
        ("outdated", "Installed but differs from v1 — update it", "Update definition"),
    ):
        html = _render(monkeypatch, definition=definition)
        assert line in html, definition
        assert _install_label(html) == label, definition


def test_rendered_worldserver_line_only_once_something_is_installed(monkeypatch):
    for worldserver, line in (
        ("active", "Running with the definition loaded"),
        ("restart_required", "Restart required — the worldserver started before the definition was installed (Dashboard → Restart)"),
        ("unknown", "Unknown — restart the worldserver if the spell does not one-shot"),
        ("not_running", "Not running"),
    ):
        assert line in _render(monkeypatch, worldserver=worldserver), worldserver
    html = _render(monkeypatch, definition="missing", worldserver="restart_required")
    assert ">Worldserver<" not in html and "Restart required" not in html


def test_unsupported_core_explains_and_offers_no_install_or_teach(monkeypatch):
    html = _render(monkeypatch, support="no_spell_dbc_table", definition="missing", worldserver="active")
    assert "This AzerothCore build has no spell_dbc table; the Banish boss killer is unavailable." in html
    assert 'id="banish-install-btn"' not in html and 'id="banish-teach-btn"' not in html
    assert 'id="banish-forget-btn"' in html  # a character that still knows 35182 can be cleaned up


def test_status_db_error_shows_the_message_and_no_install_button(monkeypatch):
    html = _render(monkeypatch, support="db_error", definition="missing", worldserver="unknown",
                   installed_at=None, message="Access denied for user")
    assert '<div class="banner error">Could not read the Banish definition: Access denied for user</div>' in html
    assert 'id="banish-install-btn"' not in html


def test_no_characters_still_renders_status_and_install(monkeypatch):
    html = _render(monkeypatch, rows=(), definition="missing")
    assert '<p class="empty-state">No real-player characters found.</p>' in html
    assert _install_label(html) == "Install definition" and "Not installed" in html
    assert 'id="banish-account"' not in html and 'id="banish-confirm-overlay"' in html


def test_character_section_buttons_start_disabled(monkeypatch):
    html = _render(monkeypatch)
    for btn in ("banish-teach-btn", "banish-forget-btn"):
        assert re.search(r'<button[^>]*id="%s"[^>]*\sdisabled[\s>]' % btn, html), btn
    assert 'id="banish-account"' in html and 'id="banish-char-list"' in html and 'id="banish-result"' in html
