import json
import tarfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.services import actions
from app.services.actions import ActionResult


def _make_archive(backups: Path, name: str, dbs, with_admin_yml=True) -> Path:
    backups.mkdir(parents=True, exist_ok=True)
    stage = backups.parent / "stage"
    (stage / "sql").mkdir(parents=True, exist_ok=True)
    (stage / "config").mkdir(parents=True, exist_ok=True)
    for db in dbs:
        (stage / "sql" / f"{db}.sql").write_text("-- dump --")
    if with_admin_yml:
        (stage / "config" / "docker-compose.admin.yml").write_text("services: {admin: true}\n")
    (stage / "manifest.json").write_text(json.dumps({
        "format_version": 1, "label": "manual", "databases": list(dbs),
        "skipped_databases": [], "git_revisions": {}, "ac_image": "x", "stack_dir": "/x",
    }))
    archive = backups / name
    with tarfile.open(archive, "w:gz") as tf:
        tf.add(stage / "manifest.json", arcname="manifest.json")
        tf.add(stage / "sql", arcname="sql")
        tf.add(stage / "config", arcname="config")
    return archive


def test_run_restore_rejects_path_traversal(tmp_path, monkeypatch):
    monkeypatch.setenv("AC_STACK_DIR", str(tmp_path))
    r = actions.run_restore("../../etc/passwd", on_progress=lambda *a: None)
    assert r == ActionResult.ERROR


def test_run_restore_rejects_unknown_db_in_manifest(tmp_path, monkeypatch):
    monkeypatch.setenv("AC_STACK_DIR", str(tmp_path))
    _make_archive(tmp_path / "backups", "azerothcore-backup-manual-x.tar.gz", ["evil_db"])
    r = actions.run_restore("azerothcore-backup-manual-x.tar.gz", on_progress=lambda *a: None)
    assert r == ActionResult.ERROR


@patch("app.services.actions.run_start", return_value=ActionResult.OK)
@patch("app.services.actions.run_stop", return_value=ActionResult.OK)
@patch("app.services.backup.run_backup")
@patch("app.services.actions.db_credentials", return_value={"password": "pw"})
def test_run_restore_aborts_if_manifest_db_sql_is_missing(
    mock_creds, mock_backup, mock_stop, mock_start, tmp_path, monkeypatch
):
    monkeypatch.setenv("AC_STACK_DIR", str(tmp_path))
    mock_backup.return_value = type("R", (), {"ok": True, "archive": "safety", "output": ""})()
    archive = _make_archive(tmp_path / "backups", "azerothcore-backup-manual-x.tar.gz", ["acore_auth"])
    with tarfile.open(archive, "r:gz") as src:
        members = [
            (m, src.extractfile(m).read() if m.isfile() else None)
            for m in src.getmembers()
            if m.name != "sql/acore_auth.sql"
        ]
    with tarfile.open(archive, "w:gz") as dst:
        for member, data in members:
            if data is None:
                dst.addfile(member)
            else:
                import io
                dst.addfile(member, io.BytesIO(data))

    r = actions.run_restore("azerothcore-backup-manual-x.tar.gz", on_progress=lambda *a: None)

    assert r == ActionResult.ERROR
    mock_stop.assert_called_once()
    mock_start.assert_called_once()


@patch("app.services.actions.run_start", return_value=ActionResult.OK)
@patch("app.services.actions.run_stop", return_value=ActionResult.OK)
@patch("app.services.backup.run_backup")
@patch("app.services.actions.subprocess.run")
@patch("app.services.actions.db_credentials", return_value={"password": "pw"})
def test_run_restore_happy_path_imports_and_takes_safety_backup(
    mock_creds, mock_run, mock_backup, mock_stop, mock_start, tmp_path, monkeypatch
):
    monkeypatch.setenv("AC_STACK_DIR", str(tmp_path))
    mock_backup.return_value = type("R", (), {"ok": True, "archive": "safety", "output": ""})()
    mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
    dbs = ["acore_auth", "acore_characters", "acore_world", "acore_playerbots"]
    _make_archive(tmp_path / "backups", "azerothcore-backup-manual-x.tar.gz", dbs)

    r = actions.run_restore("azerothcore-backup-manual-x.tar.gz", on_progress=lambda *a: None)

    assert r == ActionResult.OK
    # Pre-restore safety backup taken with the prerestore label.
    assert mock_backup.call_args.args[0] == "prerestore"
    mock_stop.assert_called_once()
    mock_start.assert_called_once()
    # A drop/create + import happened for each DB (2 docker calls per DB).
    assert mock_run.call_count >= len(dbs)


@patch("app.services.actions.run_start", return_value=ActionResult.OK)
@patch("app.services.actions.run_stop", return_value=ActionResult.OK)
@patch("app.services.backup.run_backup")
@patch("app.services.actions.db_credentials", return_value={"password": "pw"})
def test_run_restore_aborts_if_safety_backup_fails(
    mock_creds, mock_backup, mock_stop, mock_start, tmp_path, monkeypatch
):
    monkeypatch.setenv("AC_STACK_DIR", str(tmp_path))
    mock_backup.return_value = type("R", (), {"ok": False, "archive": None, "output": ""})()
    _make_archive(tmp_path / "backups", "azerothcore-backup-manual-x.tar.gz", ["acore_auth"])
    r = actions.run_restore("azerothcore-backup-manual-x.tar.gz", on_progress=lambda *a: None)
    assert r == ActionResult.ERROR
    mock_start.assert_called_once()  # server brought back up after abort


@patch("app.services.actions.run_start", return_value=ActionResult.OK)
@patch("app.services.actions.run_stop", return_value=ActionResult.OK)
@patch("app.services.backup.run_backup")
@patch("app.services.actions._restore_admin_yml", side_effect=OSError("write failed"))
@patch("app.services.actions.db_credentials", return_value={"password": "pw"})
def test_run_restore_restarts_if_restore_step_raises_after_stop(
    mock_creds, mock_restore_admin, mock_backup, mock_stop, mock_start, tmp_path, monkeypatch
):
    monkeypatch.setenv("AC_STACK_DIR", str(tmp_path))
    mock_backup.return_value = type("R", (), {"ok": True, "archive": "safety", "output": ""})()
    _make_archive(tmp_path / "backups", "azerothcore-backup-manual-x.tar.gz", [])

    r = actions.run_restore("azerothcore-backup-manual-x.tar.gz", on_progress=lambda *a: None)

    assert r == ActionResult.ERROR
    mock_stop.assert_called_once()
    mock_restore_admin.assert_called_once()
    mock_start.assert_called_once()
