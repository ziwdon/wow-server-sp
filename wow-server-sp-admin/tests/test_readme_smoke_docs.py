from pathlib import Path


ADMIN_ROOT = Path(__file__).resolve().parents[1]
README = ADMIN_ROOT / "README.md"
INSTALLER = ADMIN_ROOT / "scripts" / "install-azerothcore-admin.sh"


def test_stop_smoke_checklist_matches_stop_progress_and_backup_safety():
    readme = README.read_text()
    installer = INSTALLER.read_text()
    normalized_readme = " ".join(readme.split())

    assert (
        "attach → notify → wait_grace → notify_final → save → docker_stop → "
        "wait_exit → done"
    ) in normalized_readme
    assert "acore_*-YYYY-MM-DD.sql" not in readme
    assert "Create backup" in readme
    assert "azerothcore-backup-manual-<timestamp>.tar.gz" in readme
    assert "pre-restore safety archive" in readme
    assert "Stop/Restart triggers a backup" not in installer
