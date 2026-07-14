from pathlib import Path


TEMPLATE = (
    Path(__file__).resolve().parents[1]
    / "app/templates/partials/progression_page.html"
)


def test_progression_targets_are_semantic_disabled_buttons_with_selection_state():
    template = TEMPLATE.read_text()

    assert 'role="group" aria-labelledby="exp-picker-label"' in template
    assert '<button type="button" class="exp-tile state-downgrade"' in template
    assert 'aria-pressed="false"' in template
    assert 'tile.disabled = !available;' in template
    assert "tile.setAttribute('aria-pressed', String(key === selectedExpansion));" in template


def test_progression_confirmation_dialog_is_labelled_and_restores_keyboard_focus():
    template = TEMPLATE.read_text()

    assert 'role="dialog" aria-modal="true"' in template
    assert 'aria-labelledby="prog-confirm-title"' in template
    assert 'aria-describedby="prog-confirm-body prog-confirm-notes"' in template
    assert "if (e.key === 'Tab') trapFocus(e);" in template
    assert "if (e.key === 'Escape') { e.preventDefault(); cleanup(false); }" in template
    assert 'cancelBtn.addEventListener(\'click\', onCancel);' in template
    assert 'if (!confirmed) { updateApply(); applyBtn.focus(); return; }' in template


def test_progression_apply_cannot_duplicate_or_bypass_confirmation():
    template = TEMPLATE.read_text()

    assert 'let confirmationOpen = false;' in template
    assert 'if (!overlay || !dialog || !body || !yesBtn || !cancelBtn) { resolve(false); return; }' in template
    assert 'if (!selectedChar || !selectedExpansion || confirmationOpen) return;' in template
    assert 'confirmationOpen = true;' in template
    assert 'const confirmed = await showConfirmModal(selectedChar.name, expLabel);' in template
    assert 'confirmationOpen = false;' in template
