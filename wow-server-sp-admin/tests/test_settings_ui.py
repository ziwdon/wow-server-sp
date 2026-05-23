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
