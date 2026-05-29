from unittest.mock import MagicMock, patch

from app.services.backup import run_backup


@patch("app.services.backup.subprocess.Popen")
def test_run_backup_invokes_bundled_script_with_label_and_stackdir(mock_popen):
    proc = MagicMock()
    proc.stdout = iter([
        "[2026-05-29 14:03:00] Starting backup (label=manual)...\n",
        "[2026-05-29 14:03:10] Wrote /ac/backups/azerothcore-backup-manual-2026-05-29T14-03-10.tar.gz\n",
        "[2026-05-29 14:03:10] Backup complete.\n",
    ])
    proc.wait.return_value = 0
    proc.returncode = 0
    mock_popen.return_value = proc

    progress = []
    result = run_backup("manual", on_progress=lambda s, m: progress.append((s, m)), stack_dir="/ac")

    assert result.ok
    assert result.archive == "/ac/backups/azerothcore-backup-manual-2026-05-29T14-03-10.tar.gz"
    # Invoked bash on the bundled script with the label.
    args, kwargs = mock_popen.call_args
    cmd = args[0]
    assert cmd[0] == "bash"
    assert cmd[-2:] == ["--label", "manual"]
    assert "backup.sh" in cmd[1]
    assert kwargs["env"]["STACK_DIR"] == "/ac"
    # Progress was streamed line-by-line.
    assert any("Starting backup" in m for _, m in progress)


@patch("app.services.backup.subprocess.Popen")
def test_run_backup_nonzero_exit_is_not_ok(mock_popen):
    proc = MagicMock()
    proc.stdout = iter(["boom\n"])
    proc.wait.return_value = 1
    proc.returncode = 1
    mock_popen.return_value = proc
    result = run_backup("manual", stack_dir="/ac")
    assert not result.ok
    assert result.archive is None
