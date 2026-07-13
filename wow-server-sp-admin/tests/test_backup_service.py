import contextlib
import io
import os
import queue
import shlex
import signal
import time
from unittest.mock import MagicMock, patch

from app.services import backup
from app.services.backup import run_backup


def _write_hanging_backup_script(tmp_path, *, output: str | None) -> str:
    """Create a real child that ignores SIGTERM until the parent kills it."""
    pid_file = tmp_path / "child.pid"
    child = [
        "import os, signal, time",
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)",
        f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))",
    ]
    if output is not None:
        child.append(f"print({output!r}, flush=True)")
    child.append("while True: time.sleep(1)")
    script = tmp_path / "backup-hang.sh"
    script.write_text("#!/usr/bin/env bash\nexec python3 -c " + shlex.quote("\n".join(child)) + "\n")
    return str(script)


def _write_successful_backup_script(tmp_path) -> str:
    script = tmp_path / "backup-success.sh"
    script.write_text(
        "#!/usr/bin/env bash\n"
        "echo 'Wrote /ac/backups/azerothcore-backup-manual-success.tar.gz'\n"
    )
    return str(script)


def _write_wrapper_with_hanging_descendant(tmp_path) -> str:
    """Create a shell parent whose SIGTERM-ignoring child keeps stdout open."""
    pid_file = tmp_path / "descendant.pid"
    parent_pid_file = tmp_path / "parent.pid"
    child = "\n".join([
        "import os, signal, time",
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)",
        f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))",
        "while True: time.sleep(1)",
    ])
    script = tmp_path / "backup-wrapper.sh"
    script.write_text(
        "#!/usr/bin/env bash\n"
        f"echo $$ > {shlex.quote(str(parent_pid_file))}\n"
        "python3 -c " + shlex.quote(child) + " &\n"
        "wait $!\n"
    )
    return str(script)


def _kill_and_reap_if_needed(pid_file) -> None:
    if not pid_file.exists():
        return
    pid = int(pid_file.read_text())
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        os.waitpid(pid, 0)
    except ChildProcessError:
        pass


def _wait_until_gone(pid: int, timeout: float = 0.5) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.01)
    raise AssertionError(f"timed-out backup child {pid} is still alive")


def _assert_reaped(pid_file) -> None:
    pid = int(pid_file.read_text())
    _wait_until_gone(pid)


def _assert_not_running(pid_file) -> None:
    pid = int(pid_file.read_text())
    stat_file = backup.Path(f"/proc/{pid}/stat")
    if not stat_file.exists():
        return
    state = stat_file.read_text().rsplit(")", 1)[1].split()[0]
    assert state == "Z", f"timed-out backup child {pid} is still running ({state})"


@contextlib.contextmanager
def _test_deadline(seconds: float):
    def _expired(_signum, _frame):
        raise TimeoutError("test deadline expired while backup child was still running")

    previous = signal.signal(signal.SIGALRM, _expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


@patch("app.services.backup.subprocess.Popen")
def test_run_backup_invokes_bundled_script_with_label_and_stackdir(mock_popen):
    proc = MagicMock()
    proc.stdout = io.StringIO(
        "".join([
        "[2026-05-29 14:03:00] Starting backup (label=manual)...\n",
        "[2026-05-29 14:03:10] Wrote /ac/backups/azerothcore-backup-manual-2026-05-29T14-03-10.tar.gz\n",
        "[2026-05-29 14:03:10] Backup complete.\n",
        ])
    )
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
    proc.stdout = io.StringIO("boom\n")
    proc.wait.return_value = 1
    proc.returncode = 1
    mock_popen.return_value = proc
    result = run_backup("manual", stack_dir="/ac")
    assert not result.ok
    assert result.archive is None


def test_run_backup_overall_timeout_terminates_and_reaps_quiet_child(tmp_path, monkeypatch):
    script = _write_hanging_backup_script(tmp_path, output=None)
    monkeypatch.setattr(backup, "SCRIPT", backup.Path(script))
    monkeypatch.setattr(backup, "BACKUP_OVERALL_TIMEOUT", 0.10, raising=False)
    monkeypatch.setattr(backup, "BACKUP_NO_PROGRESS_TIMEOUT", 1.0, raising=False)
    monkeypatch.setattr(backup, "BACKUP_TERMINATE_GRACE", 0.05, raising=False)

    progress = []
    try:
        with _test_deadline(1.0):
            result = run_backup("manual", on_progress=lambda step, msg: progress.append((step, msg)))
    except BaseException:
        _kill_and_reap_if_needed(tmp_path / "child.pid")
        raise

    assert not result.ok
    assert result.timed_out is True
    assert result.archive is None
    assert any("overall deadline" in msg for _, msg in progress)
    try:
        _assert_reaped(tmp_path / "child.pid")
    except AssertionError:
        _kill_and_reap_if_needed(tmp_path / "child.pid")
        raise


def test_run_backup_no_progress_timeout_after_output_reaps_child_and_allows_later_run(
    tmp_path, monkeypatch,
):
    script = _write_hanging_backup_script(tmp_path, output="child made progress")
    monkeypatch.setattr(backup, "SCRIPT", backup.Path(script))
    monkeypatch.setattr(backup, "BACKUP_OVERALL_TIMEOUT", 1.0, raising=False)
    monkeypatch.setattr(backup, "BACKUP_NO_PROGRESS_TIMEOUT", 0.10, raising=False)
    monkeypatch.setattr(backup, "BACKUP_TERMINATE_GRACE", 0.05, raising=False)

    progress = []
    try:
        with _test_deadline(1.0):
            timed_out = run_backup(
                "manual",
                on_progress=lambda step, msg: progress.append((step, msg)),
            )
    except BaseException:
        _kill_and_reap_if_needed(tmp_path / "child.pid")
        raise

    assert not timed_out.ok
    assert timed_out.timed_out is True
    assert any(msg == "child made progress" for _, msg in progress)
    assert any("no-progress deadline" in msg for _, msg in progress)
    try:
        _assert_reaped(tmp_path / "child.pid")
    except AssertionError:
        _kill_and_reap_if_needed(tmp_path / "child.pid")
        raise

    monkeypatch.setattr(backup, "SCRIPT", backup.Path(_write_successful_backup_script(tmp_path)))
    later = run_backup("manual")
    assert later.ok
    assert later.archive == "/ac/backups/azerothcore-backup-manual-success.tar.gz"


def test_run_backup_timeout_terminates_wrapper_descendant_and_reaps_parent(tmp_path, monkeypatch):
    script = _write_wrapper_with_hanging_descendant(tmp_path)
    monkeypatch.setattr(backup, "SCRIPT", backup.Path(script))
    monkeypatch.setattr(backup, "BACKUP_OVERALL_TIMEOUT", 0.10, raising=False)
    monkeypatch.setattr(backup, "BACKUP_NO_PROGRESS_TIMEOUT", 1.0, raising=False)
    monkeypatch.setattr(backup, "BACKUP_TERMINATE_GRACE", 0.05, raising=False)

    pid_file = tmp_path / "descendant.pid"
    try:
        with _test_deadline(1.0):
            result = run_backup("manual")
        assert not result.ok
        assert result.timed_out is True
        _assert_not_running(pid_file)
        _assert_reaped(tmp_path / "parent.pid")
    except BaseException:
        _kill_and_reap_if_needed(pid_file)
        raise


def test_output_queue_discards_oldest_line_when_full():
    stream: queue.Queue[str | None] = queue.Queue(maxsize=2)

    backup._enqueue_output(stream, "oldest\n")
    backup._enqueue_output(stream, "recent\n")
    backup._enqueue_output(stream, "newest\n")

    assert [stream.get_nowait(), stream.get_nowait()] == ["recent\n", "newest\n"]
