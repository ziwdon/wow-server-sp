"""Lifecycle action state machines.

Each action accepts an `on_progress(step, msg)` callback so the calling
HTTP route can stream updates via SSE.
"""

from __future__ import annotations

import enum
import logging
import os
import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from app.services.console import WorldserverConsole
from app.services.docker_client import WORLDSERVER, inspect_worldserver
from app.services.env_var import config_key_to_ac_env_var

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


WORLD_INIT_RE = re.compile(r"World\s+Initialized\s+In", re.IGNORECASE)


def _wait_for_world_init(timeout: int, on_progress: ProgressCb) -> bool:
    """Tail /ac/logs/Server.log for the world-init line. Emitted exactly
    once at end of boot, then Server.log goes quiet (per CLAUDE.md).

    Baselines `last_size` to the file's *current* size at entry so a stale
    "World Initialized" line from the previous boot does NOT trigger a
    false positive on Restart. AC's Server.log appender uses mode `w`
    (truncate-on-open), so once the new worldserver opens the file the
    size drops below `last_size`; we reset to 0 and read everything the
    new boot writes from then on.
    """
    log_path = Path(os.environ.get("AC_STACK_DIR", "/ac")) / "logs" / "Server.log"
    deadline = time.monotonic() + timeout
    last_size = log_path.stat().st_size if log_path.exists() else 0
    while time.monotonic() < deadline:
        if log_path.exists():
            size = log_path.stat().st_size
            if size < last_size:
                # AC re-opened Server.log in mode `w` -> file truncated.
                last_size = 0
            if size > last_size:
                with log_path.open("r", errors="replace") as f:
                    f.seek(last_size)
                    chunk = f.read()
                last_size = size
                if WORLD_INIT_RE.search(chunk):
                    return True
        time.sleep(2)
    on_progress("wait_init", "timeout waiting for World Initialized")
    return False


def run_start(*, on_progress: ProgressCb) -> ActionResult:
    info = inspect_worldserver()
    if info.status == "running":
        on_progress("inspect", "already running")
        return ActionResult.ALREADY

    on_progress("compose_up", "docker compose up -d ac-worldserver ac-database")
    result = subprocess.run(
        [
            "docker", "compose",
            "--project-directory", "/ac",
            "--env-file", "/ac/.env",
            "up", "-d", "ac-worldserver", "ac-database",
        ],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        on_progress("compose_up", f"compose up FAILED: {result.stderr}")
        return ActionResult.ERROR

    on_progress("wait_init", "waiting for World initialized line")
    if not _wait_for_world_init(timeout=300, on_progress=on_progress):
        return ActionResult.TIMEOUT

    on_progress("done", "running")
    return ActionResult.OK


def run_restart(
    *,
    on_progress: ProgressCb,
    grace_seconds: int = 30,
) -> ActionResult:
    stop_result = run_stop(
        on_progress=on_progress,
        grace_seconds=grace_seconds,
        run_backup=True,
    )
    if stop_result not in (ActionResult.OK, ActionResult.ALREADY):
        return stop_result
    return run_start(on_progress=on_progress)


def run_force_stop(*, on_progress: ProgressCb) -> ActionResult:
    on_progress("force_stop", "docker stop --time 60 ac-worldserver")
    result = subprocess.run(
        ["docker", "stop", "--time", "60", WORLDSERVER],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        on_progress("force_stop", f"docker stop failed: {result.stderr}; escalating to kill")
        subprocess.run(["docker", "kill", WORLDSERVER], check=False)

    if not _wait_for_status("exited", timeout=120, on_progress=on_progress):
        return ActionResult.TIMEOUT

    on_progress("done", "force-stopped (no backup taken)")
    return ActionResult.OK


# Matches a `Key = Value` line in a loaded .conf file. Same shape as
# the parser in services/config_index.py — both must agree on what
# counts as a key, otherwise the verifier reports false negatives for
# legitimately-loaded keys.
_CONF_KV_RE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9_.]*)\s*=")


@dataclass(frozen=True)
class VerifyFailure:
    env_var: str
    config_key: str | None  # None when the failure is "not present in env"
    reason: str  # short human-readable cause


def _read_loaded_config(on_progress: ProgressCb) -> set[str] | None:
    """Read every .conf actually loaded by the running worldserver and
    return the set of derived AC_* env-var names. Returns None on read
    error so callers can fail loudly."""
    result = subprocess.run(
        [
            "docker", "exec", WORLDSERVER, "sh", "-c",
            'cat /azerothcore/env/dist/etc/worldserver.conf 2>/dev/null; '
            'for f in /azerothcore/env/dist/etc/modules/*.conf; do '
            '[ -f "$f" ] && cat "$f"; '
            'done',
        ],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        on_progress("verify", f"could not read loaded configs: {result.stderr.strip()}")
        return None
    derived: set[str] = set()
    for line in result.stdout.splitlines():
        m = _CONF_KV_RE.match(line)
        if m:
            derived.add(config_key_to_ac_env_var(m.group(1)))
    return derived


def _read_live_env(on_progress: ProgressCb) -> dict[str, str] | None:
    result = subprocess.run(
        ["docker", "exec", WORLDSERVER, "env"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        on_progress("verify", f"docker exec env failed: {result.stderr.strip()}")
        return None
    out: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            out[k] = v
    return out


def verify_env_vars_bound(
    expected: dict[str, str],
    *,
    env_var_to_key: dict[str, str],
    on_progress: ProgressCb,
) -> list[VerifyFailure]:
    """Two-part check (mirrors the install script):

    (a) Presence — every expected env var is in the container's
        environment with the expected value.
    (b) Reverse-mapping — the env var name resolves back to a key in
        the .conf files actually loaded by worldserver. This is the
        only way to catch AC's silent-drop trap (env vars whose names
        don't match any loaded key are silently ignored).

    `env_var_to_key` is `{AC_FOO_BAR: "Foo.Bar"}` for every admin-set
    env var, supplied by the caller so the failure list can name the
    user-facing key alongside the env var.
    """
    on_progress("verify", f"checking {len(expected)} env vars in ac-worldserver")

    live = _read_live_env(on_progress)
    if live is None:
        return [
            VerifyFailure(v, env_var_to_key.get(v), "could not read container env")
            for v in expected
        ]

    loaded_envs = _read_loaded_config(on_progress)
    if loaded_envs is None:
        return [
            VerifyFailure(
                v, env_var_to_key.get(v),
                "could not read loaded .conf files (silent-drop check skipped)",
            )
            for v in expected
        ]

    failures: list[VerifyFailure] = []
    for var, want in expected.items():
        key = env_var_to_key.get(var)
        # (a) Presence.
        got = live.get(var)
        if got != str(want):
            on_progress("verify", f"{var}: expected {want!r}, got {got!r}")
            failures.append(VerifyFailure(var, key, f"value mismatch: got {got!r}"))
            continue
        # (b) Reverse-mapping — silent-drop trap.
        if var not in loaded_envs:
            on_progress(
                "verify",
                f"{var}: present in env but no loaded .conf key derives this "
                f"name — AzerothCore is silently dropping it",
            )
            failures.append(VerifyFailure(
                var, key,
                "silently dropped by AC (no loaded .conf key maps to this env var)",
            ))

    if not failures:
        on_progress("verify", "all env vars bound correctly")
    return failures
