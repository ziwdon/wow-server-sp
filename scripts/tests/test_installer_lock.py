"""Executable regression coverage for the installer single-instance lock."""

import os
import re
import signal
import stat
import subprocess
import time
from pathlib import Path


SCRIPTS_DIR = (
    Path("/src")
    if Path("/src/install-azerothcore.sh").is_file()
    else Path(__file__).resolve().parents[1]
)
INSTALLER = SCRIPTS_DIR / "install-azerothcore.sh"


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _installer_fixture(tmp_path: Path) -> Path:
    """Copy the installer and add test-only hooks immediately after lock setup."""
    source = INSTALLER.read_text()
    stack_dir = 'STACK_DIR="/opt/stacks/azerothcore"'
    assert stack_dir in source
    source = source.replace(stack_dir, f'STACK_DIR="{tmp_path / "stack"}"', 1)
    root_guard = (
        'if [ "${EUID}" -eq 0 ]; then\n'
        '    echo "ERROR: Do not run this installer with sudo or as root." >&2\n'
        '    echo "Run it as your normal user; the script will ask for sudo when needed." >&2\n'
        '    exit 2\n'
        'fi\n\n'
    )
    assert root_guard in source
    source = source.replace(root_guard, "", 1)
    lock_call = '\nacquire_installer_lock "$@"\n'
    assert lock_call in source
    source = source.replace(
        lock_call,
        lock_call
        + 'if [ "${INSTALLER_LOCK_TEST_SPAWN_DESCENDANT:-0}" = 1 ]; then\n'
        + '    (\n'
        + '        if [ -n "${INSTALL_LOCK_FD:-}" ]; then\n'
        + '            eval "exec 9>&${INSTALL_LOCK_FD}"\n'
        + '        fi\n'
        + "        exec setsid sh -c 'while true; do sleep 1; done' </dev/null >/dev/null 2>&1\n"
        + '    ) &\n'
        + '    printf "%s\\n" "$!" > "$INSTALLER_LOCK_TEST_DESCENDANT_PID"\n'
        + 'fi\n\n',
        1,
    )
    logging_marker = "# ============================================================================\n# Logging\n"
    assert logging_marker in source
    source = source.replace(
        logging_marker,
        'if [ "${INSTALLER_LOCK_TEST_EXIT_AFTER_ACQUIRE:-0}" = 1 ]; then\n'
        '    exit 0\n'
        'fi\n\n'
        + logging_marker,
        1,
    )
    fixture = tmp_path / "install-azerothcore.sh"
    _write_executable(fixture, source)
    return fixture


def _wait_for(path: Path, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 5
    while not path.exists() and time.monotonic() < deadline:
        assert process.poll() is None, process.communicate()[1]
        time.sleep(0.01)
    assert path.exists(), "installer never reached the controlled sudo prompt"


def _wait_for_path(path: Path, description: str) -> None:
    deadline = time.monotonic() + 5
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert path.exists(), f"{description} was never created"


def _assert_process_is_running(pid: int) -> None:
    os.kill(pid, 0)
    state = Path(f"/proc/{pid}/stat").read_text().split()[2]
    assert state != "Z", f"process {pid} is a zombie"


def _assert_process_is_gone(pid: int) -> None:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return

    state = Path(f"/proc/{pid}/stat").read_text().split()[2]
    assert state == "Z", f"process {pid} is still running"


def test_lock_is_acquired_before_logging_starts():
    source = INSTALLER.read_text()

    lock_call = '\nacquire_installer_lock "$@"\n'
    assert lock_call in source
    assert source.index(lock_call) < source.index(
        'mkdir -p "$(dirname "$LOG_FILE")"'
    )


def test_outer_term_stops_locked_installer_before_releasing_lock(
    tmp_path: Path,
):
    installer = _installer_fixture(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    ready = tmp_path / "sudo-ready"
    sudo_calls = tmp_path / "sudo-calls"
    descendant_pid_file = tmp_path / "descendant-pid"
    _write_executable(
        bindir / "sudo",
        "#!/bin/sh\n"
        'printf "%s\\n" "$*" >> "$SUDO_CALLS"\n'
        'if [ "${1:-}" = -v ]; then : > "$SUDO_READY"; fi\n'
        "exit 0\n",
    )
    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "SUDO_READY": str(ready),
        "SUDO_CALLS": str(sudo_calls),
        "INSTALLER_LOCK_TEST_SPAWN_DESCENDANT": "1",
        "INSTALLER_LOCK_TEST_DESCENDANT_PID": str(descendant_pid_file),
    }
    first = subprocess.Popen(
        ["bash", str(installer)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    descendant_pid: int | None = None
    try:
        _wait_for(ready, first)
        _wait_for_path(descendant_pid_file, "lock descendant PID")
        descendant_pid = int(descendant_pid_file.read_text().strip())
        _assert_process_is_running(descendant_pid)
        lock_file = home / ".azerothcore-install.lock"
        assert lock_file.is_file()
        owner_before = lock_file.read_text()
        owner_pid = int(re.search(r"^pid=(\d+)$", owner_before, re.MULTILINE).group(1))
        assert first.pid != owner_pid
        calls_before = sudo_calls.read_text()

        contender = subprocess.run(
            ["bash", str(installer)],
            capture_output=True,
            text=True,
            env={**env, "INSTALLER_LOCK_TEST_EXIT_AFTER_ACQUIRE": "1"},
            check=False,
            timeout=5,
        )

        assert contender.returncode != 0
        assert "already running" in contender.stderr.lower()
        assert f"pid={owner_pid}" in contender.stderr
        assert lock_file.read_text() == owner_before
        assert sudo_calls.read_text() == calls_before
        assert not (home / ".azerothcore-install-state").exists()
        assert not (home / ".azerothcore-install-config").exists()

        os.kill(first.pid, signal.SIGTERM)
        first.wait(timeout=5)
        _assert_process_is_gone(owner_pid)
        _assert_process_is_running(descendant_pid)

        resumed = subprocess.run(
            ["bash", str(installer)],
            capture_output=True,
            text=True,
            env={**env, "INSTALLER_LOCK_TEST_EXIT_AFTER_ACQUIRE": "1"},
            check=False,
            timeout=5,
        )
        assert resumed.returncode == 0, resumed.stderr
    finally:
        if first.poll() is None:
            first.terminate()
            first.wait(timeout=5)
        if descendant_pid is not None:
            try:
                os.killpg(descendant_pid, 9)
            except ProcessLookupError:
                pass


def test_exported_lock_held_variable_cannot_bypass_active_lock(tmp_path: Path):
    installer = _installer_fixture(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    ready = tmp_path / "sudo-ready"
    sudo_calls = tmp_path / "sudo-calls"
    _write_executable(
        bindir / "sudo",
        "#!/bin/sh\n"
        'printf "%s\\n" "$*" >> "$SUDO_CALLS"\n'
        'if [ "${1:-}" = -v ]; then : > "$SUDO_READY"; fi\n'
        "exit 0\n",
    )
    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "SUDO_READY": str(ready),
        "SUDO_CALLS": str(sudo_calls),
    }
    first = subprocess.Popen(
        ["bash", str(installer)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        _wait_for(ready, first)

        bypass = subprocess.run(
            ["bash", str(installer)],
            capture_output=True,
            text=True,
            env={
                **env,
                "INSTALLER_LOCK_HELD": "1",
                "INSTALLER_LOCK_TEST_EXIT_AFTER_ACQUIRE": "1",
            },
            check=False,
            timeout=5,
        )

        assert bypass.returncode != 0
        assert "already running" in bypass.stderr.lower()
    finally:
        if first.poll() is None:
            first.terminate()
            first.wait(timeout=5)
