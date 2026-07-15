from pathlib import Path

PARTIAL = Path(__file__).resolve().parents[1] / "app/templates/partials/raid_unlock_page.html"
PAGE = Path(__file__).resolve().parents[1] / "app/templates/progression.html"


def test_partial_has_picker_and_accessible_modal():
    t = PARTIAL.read_text()
    assert 'id="raid-picker"' in t
    assert 'role="group" aria-labelledby="raid-picker-label"' in t
    assert 'class="raid-tile"' in t
    assert 'role="dialog" aria-modal="true"' in t
    assert "if (e.key === 'Tab') trapFocus(e);" in t
    assert "if (e.key === 'Escape') { e.preventDefault(); cleanup(false); }" in t


def test_partial_posts_guid_and_raid_key():
    t = PARTIAL.read_text()
    assert "'/api/raid-unlock/apply'" in t
    assert "guid: selectedChar.guid, raid_key: selectedRaid" in t
    assert "let confirmationOpen = false;" in t


def test_progression_page_loads_raid_unlock_wrapper():
    t = PAGE.read_text()
    assert 'id="raid-unlock-data"' in t
    assert 'hx-get="/api/raid-unlock/characters"' in t
