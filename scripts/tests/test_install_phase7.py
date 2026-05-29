from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALLER = (REPO_ROOT / "scripts" / "install-azerothcore.sh").read_text()


def test_phase7_copies_canonical_script_not_heredoc():
    # The old heredoc marker must be gone; the install script copies backup.sh.
    assert "cat > \"${STACK_DIR}/backup.sh\" <<'SCRIPT'" not in INSTALLER
    assert 'cp "${SCRIPT_SOURCE_DIR}/backup.sh" "${STACK_DIR}/backup.sh"' in INSTALLER


def test_phase7_cron_line_is_argument_free():
    # No-arg backup.sh = daily mode = prune. Crontab path/invocation unchanged.
    assert (
        'CRON_ENTRY="0 3 * * * /opt/stacks/azerothcore/backup.sh '
        '>> /opt/stacks/azerothcore/logs/backup.log 2>&1"'
    ) in INSTALLER
    # The script must NOT pass --prune on the cron line.
    assert "backup.sh --prune" not in INSTALLER
