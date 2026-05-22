from pathlib import Path


def test_settings_js_renders_read_only_keys_disabled_and_uneditable():
    script = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "static"
        / "settings.js"
    ).read_text()

    assert "k.read_only" in script
    assert "key-badge" in script
    assert "installer-managed" in script
    assert "disabled readonly" in script
    assert "if (k.read_only)" in script
