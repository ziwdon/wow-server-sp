import asyncio
import datetime as dt
import os
import shlex
import signal

import pytest

from app.services import backup
from app.services.actions import run_backup_manual
from app.services.actions import ActionResult
from app.services.runner import ActionRunner


def _write_output_then_hang_script(tmp_path) -> None:
    pid_file = tmp_path / "child.pid"
    child = "\n".join([
        "import os, signal, time",
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)",
        f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))",
        "print('child made progress', flush=True)",
        "while True: time.sleep(1)",
    ])
    script = tmp_path / "backup-output-hang.sh"
    script.write_text("#!/usr/bin/env bash\nexec python3 -c " + shlex.quote(child) + "\n")


def _assert_reaped(pid_file) -> None:
    pid = int(pid_file.read_text())
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return
    raise AssertionError(f"timed-out backup child {pid} is still alive")


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


@pytest.mark.asyncio
async def test_action_record_replays_recorded_progress_timestamp(monkeypatch):
    first_event_at = dt.datetime(2026, 6, 19, 5, 0, tzinfo=dt.timezone.utc)
    replay_at = dt.datetime(2026, 6, 19, 11, 1, tzinfo=dt.timezone.utc)
    clock_values = [first_event_at, replay_at]

    monkeypatch.setattr(
        "app.services.runner._utcnow",
        lambda: clock_values.pop(0),
        raising=False,
    )
    runner = ActionRunner()

    record = runner.start(
        "restart",
        lambda on_progress: (
            on_progress("wait_init", "waiting for World initialized line"),
            ActionResult.OK,
        )[1],
    )

    while runner.current() is not None:
        await asyncio.sleep(0)

    q = record.subscribe()

    assert q.get_nowait() == (
        "progress",
        first_event_at,
        "wait_init",
        "waiting for World initialized line",
    )


@pytest.mark.asyncio
async def test_runner_releases_single_flight_after_action_completes():
    runner = ActionRunner()
    release = __import__("threading").Event()

    first = runner.start(
        "slow", lambda _progress: (release.wait(), ActionResult.OK)[1]
    )
    with pytest.raises(RuntimeError, match="another action"):
        runner.start("second", lambda _progress: ActionResult.OK)

    release.set()
    await first.wait()
    third = runner.start("third", lambda _progress: ActionResult.OK)
    await third.wait()
    assert runner.current() is None


@pytest.mark.asyncio
async def test_runner_serializes_external_mutation_with_actions():
    runner = ActionRunner()

    assert runner.try_acquire_mutation() is True
    with pytest.raises(RuntimeError, match="another action"):
        runner.start("restore", lambda _progress: ActionResult.OK)
    runner.release_mutation()

    release = __import__("threading").Event()
    action = runner.start(
        "restore", lambda _progress: (release.wait(), ActionResult.OK)[1]
    )
    assert runner.try_acquire_mutation() is False
    release.set()
    await action.wait()
    assert runner.try_acquire_mutation() is True
    runner.release_mutation()


@pytest.mark.asyncio
async def test_runner_releases_after_output_then_hanging_manual_backup(tmp_path, monkeypatch):
    _write_output_then_hang_script(tmp_path)
    monkeypatch.setattr(backup, "SCRIPT", tmp_path / "backup-output-hang.sh")
    monkeypatch.setattr(backup, "BACKUP_OVERALL_TIMEOUT", 1.0, raising=False)
    monkeypatch.setattr(backup, "BACKUP_NO_PROGRESS_TIMEOUT", 0.10, raising=False)
    monkeypatch.setattr(backup, "BACKUP_TERMINATE_GRACE", 0.05, raising=False)

    runner = ActionRunner()
    first = runner.start("backup", lambda on_progress: run_backup_manual(on_progress=on_progress))
    try:
        await asyncio.wait_for(first.wait(), timeout=1.0)
    except TimeoutError:
        _kill_and_reap_if_needed(tmp_path / "child.pid")
        await asyncio.wait_for(first.wait(), timeout=1.0)
        raise

    assert first.status == ActionResult.TIMEOUT.value
    assert any(msg == "child made progress" for _, _, msg in first.steps)
    assert any("no-progress deadline" in msg for _, _, msg in first.steps)
    _assert_reaped(tmp_path / "child.pid")

    second = runner.start("later", lambda _on_progress: ActionResult.OK)
    await second.wait()
    assert second.status == ActionResult.OK.value
