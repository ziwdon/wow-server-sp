"""Static and server-level accessibility contracts for the admin UI.

Browser/axe verification is deliberately owned by T-10.  These tests protect
the semantic structure that the server and client scripts are responsible for.
"""

import datetime as dt
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import _render_progress, app


ADMIN_ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (ADMIN_ROOT / rel).read_text()


def test_settings_page_keeps_the_base_main_as_its_only_main_landmark():
    response = TestClient(app).get("/settings")

    assert response.status_code == 200
    assert response.text.count("<main") == 1
    assert '<div class="settings-list" id="key-list">' in response.text


def test_settings_rows_use_a_native_detail_button_separate_from_value_input():
    script = _read("app/static/settings.js")

    assert 'class="key-row-select"' in script
    assert 'aria-controls="key-detail"' in script
    assert "row.setAttribute('role', 'button');" not in script
    assert "row.tabIndex = 0;" not in script
    assert "event.target !== row" not in script


def test_mobile_detail_close_restores_focus_to_the_native_detail_button():
    script = _read("app/static/settings.js")

    assert "const detailButton = newRow?.querySelector('.key-row-select');" in script
    assert "mobileDetailTrigger = detailButton;" in script
    assert "mobileDetailTrigger = newRow;" not in script


def test_mobile_filter_toggle_exposes_and_synchronizes_its_expanded_state():
    html = _read("app/templates/settings.html")
    script = _read("app/static/settings.js")

    assert 'aria-expanded="false"' in html
    assert 'aria-controls="sidebar-extra"' in html
    assert "mobileFilterToggle.setAttribute('aria-expanded', String(isOpen));" in script


def test_backups_sse_swaps_progress_fragments_directly_into_the_activity_list():
    response = TestClient(app).get("/backups")

    assert response.status_code == 200
    assert '<ul id="action-log" class="action-log" sse-swap="progress,done" hx-swap="beforeend">' in response.text
    assert '<li sse-swap="progress,done"' not in response.text


def test_sse_progress_payload_is_a_single_list_item_for_the_activity_list():
    payload = _render_progress(
        "wait_init", "waiting", dt.datetime(2026, 6, 19, 5, tzinfo=dt.timezone.utc)
    )

    assert payload.startswith('<li class="step step-wait_init">')
    assert payload.endswith("</li>")
    assert "<ul" not in payload
