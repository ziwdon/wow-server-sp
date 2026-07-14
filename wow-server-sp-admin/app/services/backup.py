"""Invoke the bundled canonical backup script to create one consolidated archive.

The admin runs the SAME script the host cron runs (bundled into the image at
/app/scripts/backup.sh), with STACK_DIR=/ac. We never reimplement backup logic
in Python - single source of truth. See the design spec.
"""
from __future__ import annotations

import logging
import os
import queue
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

SCRIPT = Path(os.environ.get("BACKUP_SCRIPT", "/app/scripts/backup.sh"))
BACKUP_OVERALL_TIMEOUT = float(os.environ.get("BACKUP_OVERALL_TIMEOUT_SECONDS", "3600"))
BACKUP_NO_PROGRESS_TIMEOUT = float(
    os.environ.get("BACKUP_NO_PROGRESS_TIMEOUT_SECONDS", "900")
)
BACKUP_TERMINATE_GRACE = float(
    os.environ.get("BACKUP_TERMINATE_GRACE_SECONDS", "10")
)
MAX_OUTPUT_CHARS = 64 * 1024
OUTPUT_QUEUE_MAX_ITEMS = 256
OUTPUT_READ_CHUNK_CHARS = 4096

ProgressCb = Callable[[str, str], None]


@dataclass
class BackupResult:
    ok: bool
    archive: str | None
    output: str
    timed_out: bool = False


def _process_group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_for_process_group(process_group: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while _process_group_exists(process_group):
        if time.monotonic() >= deadline:
            return False
        time.sleep(min(0.01, max(deadline - time.monotonic(), 0)))
    return True


def _terminate_and_reap(proc: subprocess.Popen) -> None:
    """Stop a timed-out process group, escalating once, before returning."""
    process_group = proc.pid
    try:
        os.killpg(process_group, signal.SIGTERM)
    except ProcessLookupError:
        pass

    # Do not let the shell parent's early exit shorten the descendant group's
    # TERM grace period: a docker/mysqldump child may still be running.
    group_exited = _wait_for_process_group(process_group, BACKUP_TERMINATE_GRACE)
    if not group_exited:
        try:
            os.killpg(process_group, signal.SIGKILL)
        except ProcessLookupError:
            pass
        if not _wait_for_process_group(process_group, BACKUP_TERMINATE_GRACE):
            log.error("backup process group did not exit within the post-KILL grace period")

    try:
        proc.wait(timeout=BACKUP_TERMINATE_GRACE)
    except subprocess.TimeoutExpired:
        # The direct child is part of the group, but retain a bounded wait in
        # case it is stuck in uninterruptible kernel I/O.
        log.error("backup shell did not exit within the post-KILL grace period")


def _enqueue_output(stream: queue.Queue[str | None], raw: str | None) -> None:
    """Keep recent output without ever blocking the stdout reader."""
    try:
        stream.put_nowait(raw)
        return
    except queue.Full:
        pass
    try:
        stream.get_nowait()
    except queue.Empty:
        pass
    try:
        stream.put_nowait(raw)
    except queue.Full:
        # The consumer won the race after eviction; it will read newer output.
        pass


def _append_output(current: str, line: str) -> str:
    """Keep a bounded tail suitable for diagnostics without retaining all logs."""
    if current:
        current += "\n"
    return (current + line)[-MAX_OUTPUT_CHARS:]


def run_backup(
    label: str,
    *,
    on_progress: ProgressCb | None = None,
    stack_dir: str | None = None,
) -> BackupResult:
    """Run `backup.sh --label <label>` with bounded execution and output."""
    stack = stack_dir or os.environ.get("AC_STACK_DIR", "/ac")
    proc = subprocess.Popen(
        ["bash", str(SCRIPT), "--label", label],
        env={**os.environ, "STACK_DIR": stack},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        start_new_session=True,
    )
    output = ""
    archive: str | None = None
    assert proc.stdout is not None

    # Reading stdout in this worker lets the deadline loop run even when the
    # child produces no newline (or no output at all).
    stream: queue.Queue[str | None] = queue.Queue(maxsize=OUTPUT_QUEUE_MAX_ITEMS)

    def _read_stdout() -> None:
        try:
            while raw := proc.stdout.readline(OUTPUT_READ_CHUNK_CHARS):
                _enqueue_output(stream, raw)
        finally:
            _enqueue_output(stream, None)

    reader = threading.Thread(target=_read_stdout, name="backup-output", daemon=True)
    reader.start()

    started = last_progress = time.monotonic()
    timeout_reason: str | None = None

    def _record(raw: str) -> None:
        nonlocal archive, last_progress, output
        line = raw.rstrip("\n")
        output = _append_output(output, line)
        last_progress = time.monotonic()
        if on_progress:
            on_progress("backup", line)
        if "Wrote " in line:
            archive = line.split("Wrote ", 1)[1].strip()

    while proc.poll() is None:
        now = time.monotonic()
        overall_remaining = BACKUP_OVERALL_TIMEOUT - (now - started)
        progress_remaining = BACKUP_NO_PROGRESS_TIMEOUT - (now - last_progress)
        if overall_remaining <= 0:
            timeout_reason = f"overall deadline ({BACKUP_OVERALL_TIMEOUT:g}s)"
            break
        if progress_remaining <= 0:
            timeout_reason = f"no-progress deadline ({BACKUP_NO_PROGRESS_TIMEOUT:g}s)"
            break
        try:
            raw = stream.get(timeout=min(overall_remaining, progress_remaining, 0.1))
        except queue.Empty:
            continue
        if raw is not None:
            _record(raw)

    if timeout_reason is not None:
        message = f"backup timed out: {timeout_reason}; terminating child"
        log.error(message)
        if on_progress:
            on_progress("backup", message)
        _terminate_and_reap(proc)
    else:
        proc.wait()

    # A descendant could have inherited stdout after the direct child exits;
    # never let that keep this action runner slot occupied indefinitely.
    reader.join(timeout=max(BACKUP_TERMINATE_GRACE, 0.1))
    while True:
        try:
            raw = stream.get_nowait()
        except queue.Empty:
            break
        if raw is not None:
            _record(raw)

    ok = proc.returncode == 0
    if not ok:
        log.error("backup.sh failed (rc=%s): %s", proc.returncode, output[-MAX_OUTPUT_CHARS:])
    return BackupResult(
        ok=ok,
        archive=archive if ok else None,
        output=output,
        timed_out=timeout_reason is not None,
    )
