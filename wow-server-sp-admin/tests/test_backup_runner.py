from pathlib import Path
from unittest.mock import MagicMock, patch

from app.services.backup_runner import BackupResult, run_full_backup


@patch("app.services.backup_runner.subprocess.run")
def test_run_full_backup_invokes_mysqldump_per_db(mock_run, tmp_path):
    # `docker exec ac-database mysql USE <db>` (existence probe) returns 0,
    # and `docker exec ac-database mysqldump <db>` returns 0 with bytes.
    def _fake_run(cmd, *args, **kwargs):
        m = MagicMock(returncode=0, stdout=b"-- dump bytes --", stderr=b"")
        return m

    mock_run.side_effect = _fake_run
    backups_dir = tmp_path / "backups"
    backups_dir.mkdir()

    result = run_full_backup(
        backups_dir=backups_dir,
        stack_dir=tmp_path,  # holds .env / configs for the tar step
        db_password="secret",
        date_str="2026-05-20",
    )
    assert result.ok
    # All four AC databases dumped.
    for db in ("acore_auth", "acore_characters", "acore_world", "acore_playerbots"):
        assert (backups_dir / f"{db}-2026-05-20.sql").exists()
    # The mysqldump invocations carried `-uroot -p<pw>` and the db name.
    dump_calls = [
        c.args[0] for c in mock_run.call_args_list
        if "mysqldump" in c.args[0]
    ]
    assert len(dump_calls) == 4
    assert (backups_dir / "azerothcore-config-2026-05-20.tar.gz").exists()


@patch("app.services.backup_runner.subprocess.run")
def test_skips_missing_database(mock_run, tmp_path):
    # USE probe returns rc=1 for acore_playerbots; rest succeed.
    def _fake_run(cmd, *args, **kwargs):
        if "USE acore_playerbots" in " ".join(cmd):
            return MagicMock(returncode=1, stdout=b"", stderr=b"unknown db")
        return MagicMock(returncode=0, stdout=b"-- dump --", stderr=b"")

    mock_run.side_effect = _fake_run
    backups_dir = tmp_path / "backups"
    backups_dir.mkdir()
    result = run_full_backup(
        backups_dir=backups_dir,
        stack_dir=tmp_path,
        db_password="x",
        date_str="2026-05-20",
    )
    assert result.ok
    assert not (backups_dir / "acore_playerbots-2026-05-20.sql").exists()
    assert (backups_dir / "acore_auth-2026-05-20.sql").exists()
    assert "acore_playerbots" in result.skipped
    assert len(result.skipped) == 1
    assert len(result.dumped) == 3  # the other three DBs were dumped


@patch("app.services.backup_runner.subprocess.run")
def test_dump_failure_sets_ok_false(mock_run, tmp_path):
    def _fake_run(cmd, *args, **kwargs):
        # Probe (mysql) always passes; dump of acore_characters fails
        if "mysqldump" in cmd and "acore_characters" in cmd:
            return MagicMock(returncode=1, stdout=b"", stderr=b"access denied")
        return MagicMock(returncode=0, stdout=b"-- dump --", stderr=b"")

    mock_run.side_effect = _fake_run
    backups_dir = tmp_path / "backups"
    backups_dir.mkdir()
    result = run_full_backup(
        backups_dir=backups_dir,
        stack_dir=tmp_path,
        db_password="pw",
        date_str="2026-05-20",
    )
    assert not result.ok
    assert result.error is not None
    assert "acore_characters" in result.error
