"""Lifecycle action state machines.

Each action accepts an `on_progress(step, msg)` callback so the calling
HTTP route can stream updates via SSE.
"""

from __future__ import annotations

import enum
import logging
import os
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from app.services.console import WorldserverConsole
from app.services.docker_client import WORLDSERVER, inspect_worldserver

ProgressCb = Callable[[str, str], None]
log = logging.getLogger(__name__)


class ActionResult(str, enum.Enum):
    OK = "ok"
    TIMEOUT = "timeout"
    ALREADY = "already"
    ERROR = "error"


def _wait_for_status(target: str, timeout: int, on_progress: ProgressCb) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        info = inspect_worldserver()
        if info.status == target:
            return True
        time.sleep(2)
    on_progress("wait_exit", f"timeout waiting for status={target}")
    return False


def _run_backup(on_progress: ProgressCb) -> bool:
    """Run the in-process backup. We do NOT shell out to /ac/backup.sh —
    that script hardcodes STACK_DIR=/opt/stacks/azerothcore (no env
    override) and writes via the host filesystem, neither of which work
    from inside the admin container. See app/services/backup_runner.py."""
    from app.services.backup_runner import run_full_backup
    from app.state import db_credentials

    on_progress("backup", "running in-process backup")
    ac_stack = Path(os.environ.get("AC_STACK_DIR", "/ac"))
    creds = db_credentials()
    result = run_full_backup(
        backups_dir=ac_stack / "backups",
        stack_dir=ac_stack,
        db_password=str(creds["password"]),
    )
    if not result.ok:
        on_progress("backup", f"backup FAILED: {result.error}")
        log.error("backup failed: %s", result.error)
        return False
    summary = f"dumped={result.dumped}; skipped={result.skipped}"
    on_progress("backup", f"backup OK ({summary})")
    return True


def run_stop(
    *,
    on_progress: ProgressCb,
    grace_seconds: int = 30,
    run_backup: bool = True,
) -> ActionResult:
    """Safe stop.

    Sequence (grace_seconds=30 default):
      t=0   announce + notify ("shutting down in 30s")
      t=20  announce + notify ("final 10 seconds")
      t=29  saveall (explicit save while AC is still healthy)
      t=30  detach, then `docker stop --time 60` (SIGTERM → AC's clean
            shutdown handler → World::StopNow(SHUTDOWN_EXIT_CODE) →
            final implicit saveall → exit code 0; Docker marks the
            container as user-stopped so `restart: unless-stopped`
            backs off). The `--time 60` window gives AC's final save
            headroom under load — on a quiet world it completes in
            5-15 s, but with ~2500 bot characters the save can stretch
            to 30-45 s; 60 s avoids a Docker-initiated SIGKILL while
            the save is mid-flight.
      then  in-process backup_runner (if run_backup=True)

    We do NOT use `server shutdown N` — its countdown is collapsed by
    the SIGTERM `docker stop` sends, defeating its purpose.
    """
    info = inspect_worldserver()
    if info.status in ("exited", "missing"):
        on_progress("inspect", f"already {info.status}")
        if run_backup:
            return ActionResult.OK if _run_backup(on_progress) else ActionResult.ERROR
        return ActionResult.OK

    # Compute the two sub-windows: most of the grace, then a final 10s.
    final_window = min(10, grace_seconds)
    early_window = max(0, grace_seconds - final_window - 1)

    on_progress("attach", "attaching to worldserver stdin")
    try:
        with WorldserverConsole(WORLDSERVER) as console:
            on_progress("notify", f"announcing {grace_seconds}s grace to players")
            console.send(
                f"announce Server shutting down in {grace_seconds} seconds "
                "for maintenance. Please log out safely."
            )
            console.send(f"notify Server shutting down in {grace_seconds}s.")

            if early_window > 0:
                on_progress("wait_grace", f"waiting {early_window}s")
                time.sleep(early_window)

            on_progress("notify_final", "final-10s warning")
            console.send("announce Final 10 seconds.")
            console.send("notify 10s remaining.")
            time.sleep(max(0, final_window - 1))

            on_progress("save", "saveall")
            console.send("saveall")
            time.sleep(1)
    except Exception as e:  # noqa: BLE001
        on_progress("attach", f"console error: {e}")
        return ActionResult.ERROR

    on_progress("docker_stop", "docker stop --time 60 ac-worldserver")
    subprocess.run(
        ["docker", "stop", "--time", "60", WORLDSERVER],
        check=False,
    )

    on_progress("wait_exit", "waiting for exited state")
    if not _wait_for_status("exited", timeout=120, on_progress=on_progress):
        return ActionResult.TIMEOUT

    if run_backup and not _run_backup(on_progress):
        return ActionResult.ERROR

    on_progress("done", "stopped + backup OK")
    return ActionResult.OK
