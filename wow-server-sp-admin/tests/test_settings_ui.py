from pathlib import Path


ADMIN_ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (ADMIN_ROOT / rel).read_text()


def test_settings_js_renders_read_only_keys_disabled_and_uneditable():
    script = _read("app/static/settings.js")

    assert "k.read_only" in script
    assert "key-badge" in script
    assert "installer-managed" in script
    assert "disabled readonly" in script
    assert "if (k.read_only)" in script


def test_settings_js_marks_applied_and_pending_states():
    script = _read("app/static/settings.js")

    assert "key-row-applied" in script
    assert "key-row-pending" in script
    assert "key-input-applied" in script
    assert "key-input-pending" in script
    assert "Object.prototype.hasOwnProperty.call(state.pending, k.key)" in script
    assert "pending, not applied" in script


def test_settings_css_widens_value_and_detail_columns():
    css = _read("app/static/app.css")

    assert "grid-template-columns: 270px minmax(0, 1fr) 525px" in css
    assert "minmax(240px, 300px)" in css
    assert ".key-input-pending" in css
    assert ".key-input-applied" in css


def test_css_stat_card_equal_height():
    css = _read("app/static/app.css")
    assert ".stat-row > div { min-width: 0; display: flex; flex-direction: column; }" in css
    assert ".stat-row .stat-card { flex: 1; }" in css


def test_css_btn_block_removed():
    css = _read("app/static/app.css")
    assert ".btn-block" not in css


def test_settings_html_has_only_pending_checkbox():
    html = _read("app/templates/settings.html")
    assert 'id="only-pending"' in html
    assert "Show pending changes" in html


def test_settings_js_handles_only_pending_filter():
    script = _read("app/static/settings.js")
    assert "only-pending" in script
    assert "pendingOnly" in script
    assert "No pending changes" in script


def test_settings_js_applies_selected_class_to_row():
    script = _read("app/static/settings.js")
    assert "classList.remove('selected')" in script
    assert "classList.add('selected')" in script
    assert "'selected'" in script  # pushed to rowClasses in _render


def test_settings_js_redirects_to_dashboard_on_success():
    script = _read("app/static/settings.js")
    assert "window.location.href = '/'" in script
    assert "hardError" in script
    assert "resolve(status !== 'ok')" in script


def test_settings_js_redirects_only_for_explicit_ok_action_status():
    script = _read("app/static/settings.js")

    assert "function actionStatusFromDone" in script
    assert "return match?.[1] || 'unknown'" in script
    assert "if (status !== 'ok')" in script
    assert "es.addEventListener('idle'" in script
    assert "es.addEventListener('error'" in script


def test_settings_rows_use_native_detail_buttons_separate_from_text_inputs():
    script = _read("app/static/settings.js")

    assert 'class="key-row-select"' in script
    assert "row.querySelector('.key-row-select').addEventListener('click'" in script
    assert "row.setAttribute('role', 'button');" not in script
