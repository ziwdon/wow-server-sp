"""Hermetic regression tests for root/admin lifecycle shell scripts.

Each test invokes a real script (or a precisely isolated production phase) with
all externally mutating commands replaced by tools in ``tmp_path``.  The
fixtures never point at /opt, /etc, the host Docker daemon, or a live crontab.
"""

from __future__ import annotations

import os
import shlex
import stat
import subprocess
from pathlib import Path


ROOT = Path("/src") if Path("/src/install-azerothcore.sh").is_file() else Path(__file__).resolve().parents[1]
ADMIN_ROOT = ROOT.parent / "wow-server-sp-admin"
INSTALLER = ROOT / "install-azerothcore.sh"
REDEPLOY = ROOT / "redeploy-azerothcore.sh"
UNINSTALL = ROOT / "uninstall-azerothcore.sh"
ADMIN_UNINSTALL = ADMIN_ROOT / "scripts" / "uninstall-azerothcore-admin.sh"


def _executable(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _drop_root_guard(source: str) -> str:
    guard = (
        'if [ "$EUID" -eq 0 ]; then\n'
        '    echo "ERROR: do not run as root; docker is invoked as your user (docker group)." >&2\n'
        '    exit 1\n'
        'fi\n\n'
    )
    assert guard in source
    return source.replace(guard, "", 1)


def _run(script: Path, *args: str, env: dict[str, str], input: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/bash", str(script), *args], input=input, text=True, capture_output=True,
        check=False, timeout=10, env={**os.environ, **env},
    )


def _root_redeploy_fixture(tmp_path: Path, *, initialized: bool = False) -> tuple[Path, Path, Path, Path]:
    stack = tmp_path / "stack"
    (stack / "logs").mkdir(parents=True)
    (stack / "docker-compose.yml").write_text("services: {}\n")
    (stack / ".env").write_text("COMPOSE_FILE=docker-compose.yml\n")
    (stack / "logs" / "Server.log").write_text("WORLD: World Initialized\n" if initialized else "old boot\n")
    bindir = tmp_path / "bin"; bindir.mkdir()
    calls = tmp_path / "calls.log"
    _executable(
        bindir / "docker",
        "#!/bin/sh\n"
        "printf '%s\\n' \"$*\" >> \"$LIFECYCLE_CALLS\"\n"
        "case \"$1 $2\" in\n"
        "  'compose config') echo ac-worldserver ;;\n"
        "  'compose ps') echo present ;;\n"
        "  'compose build') [ \"${REDEPLOY_CASE:-ok}\" != build-fail ] || exit 42 ;;\n"
        "  'compose stop') [ \"${REDEPLOY_CASE:-ok}\" != stop-fail ] || exit 42 ;;\n"
        "  'compose up')\n"
        "    [ \"${REDEPLOY_CASE:-ok}\" != recreate-fail ] || exit 42\n"
        "    if [ \"${REDEPLOY_WRITE_BOOT_LOG:-0}\" = 1 ]; then\n"
        "      : > \"$REDEPLOY_SERVER_LOG\"\n"
        "      printf '%b' \"${REDEPLOY_NEW_BOOT_LOG:-}\" > \"$REDEPLOY_SERVER_LOG\"\n"
        "    fi ;;\n"
        "  'inspect -f') echo running ;;\n"
        "esac\n",
    )
    _executable(bindir / "sleep", "#!/bin/sh\nexit 0\n")
    script = tmp_path / "redeploy.sh"
    _executable(script, _drop_root_guard(REDEPLOY.read_text()))
    return stack, bindir, calls, script


def test_root_redeploy_preflight_and_build_failures_preserve_running_service(tmp_path: Path) -> None:
    stack, bindir, calls, script = _root_redeploy_fixture(tmp_path)
    missing = _run(
        script, env={"PATH": f"{bindir}:{os.environ['PATH']}", "STACK_DIR": str(tmp_path / "missing"), "LIFECYCLE_CALLS": str(calls)},
    )
    assert missing.returncode == 1
    assert not calls.exists(), missing.stderr

    failed_build = _run(
        script,
        env={"PATH": f"{bindir}:{os.environ['PATH']}", "STACK_DIR": str(stack), "LIFECYCLE_CALLS": str(calls), "REDEPLOY_CASE": "build-fail"},
    )
    assert failed_build.returncode != 0
    assert calls.read_text().splitlines() == ["compose config --services", "compose build ac-worldserver"]


def test_root_redeploy_stops_with_timeout_and_never_recreates_after_stop_failure(tmp_path: Path) -> None:
    stack, bindir, calls, script = _root_redeploy_fixture(tmp_path)
    result = _run(
        script,
        env={"PATH": f"{bindir}:{os.environ['PATH']}", "STACK_DIR": str(stack), "STOP_TIMEOUT": "37", "LIFECYCLE_CALLS": str(calls), "REDEPLOY_CASE": "stop-fail"},
    )
    assert result.returncode != 0
    log = calls.read_text().splitlines()
    assert "compose stop -t 37 ac-worldserver" in log
    assert not any(line.startswith("compose up") for line in log)


def test_root_redeploy_recreate_failure_does_not_claim_current_boot_readiness(tmp_path: Path) -> None:
    stack, bindir, calls, script = _root_redeploy_fixture(tmp_path, initialized=True)
    result = _run(
        script,
        env={"PATH": f"{bindir}:{os.environ['PATH']}", "STACK_DIR": str(stack), "LIFECYCLE_CALLS": str(calls), "REDEPLOY_CASE": "recreate-fail"},
    )
    assert result.returncode != 0
    assert "World Initialized — worldserver is up." not in result.stdout
    assert calls.read_text().splitlines()[-1] == "compose up -d ac-worldserver"


def test_root_redeploy_accepts_only_a_marker_written_by_the_recreated_boot(tmp_path: Path) -> None:
    stack, bindir, calls, script = _root_redeploy_fixture(tmp_path, initialized=True)
    log = stack / "logs" / "Server.log"
    result = _run(
        script,
        env={
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "STACK_DIR": str(stack),
            "LIFECYCLE_CALLS": str(calls),
            "REDEPLOY_WRITE_BOOT_LOG": "1",
            "REDEPLOY_SERVER_LOG": str(log),
            "REDEPLOY_NEW_BOOT_LOG": "booting\\nWORLD: World Initialized\\n",
        },
    )
    assert result.returncode == 0, result.stderr
    assert log.read_text() == "booting\nWORLD: World Initialized\n"
    assert "World Initialized — worldserver is up." in result.stdout


def test_root_redeploy_rejects_a_stale_marker_after_recreate_truncates_the_log(tmp_path: Path) -> None:
    stack, bindir, calls, script = _root_redeploy_fixture(tmp_path, initialized=True)
    log = stack / "logs" / "Server.log"
    result = _run(
        script,
        env={
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "STACK_DIR": str(stack),
            "WORLD_INIT_TIMEOUT": "1",
            "LIFECYCLE_CALLS": str(calls),
            "REDEPLOY_WRITE_BOOT_LOG": "1",
            "REDEPLOY_SERVER_LOG": str(log),
            "REDEPLOY_NEW_BOOT_LOG": "booting only\\n",
        },
    )
    assert result.returncode == 1
    assert log.read_text() == "booting only\n"
    assert "did not observe 'World Initialized'" in result.stderr


def _installer_through_force_fresh(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    home = tmp_path / "home"; home.mkdir()
    stack = tmp_path / "stack"; stack.mkdir()
    (stack / "docker-compose.yml").write_text("services: {}\n")
    state = home / ".azerothcore-install-state"; state.write_text("4|keep|checkpoint\n")
    config = home / ".azerothcore-install-config"; config.write_text("DB_ROOT_PASSWORD=keep\n")
    source = INSTALLER.read_text()
    root_guard = (
        'if [ "${EUID}" -eq 0 ]; then\n'
        '    echo "ERROR: Do not run this installer with sudo or as root." >&2\n'
        '    echo "Run it as your normal user; the script will ask for sudo when needed." >&2\n'
        '    exit 2\n'
        'fi\n\n'
    )
    assert root_guard in source
    source = source.replace(root_guard, "", 1)
    source = source.replace('STACK_DIR="/opt/stacks/azerothcore"', f'STACK_DIR={shlex.quote(str(stack))}', 1)
    lock_call = 'acquire_installer_lock "$@"\n'
    assert lock_call in source
    source = source.replace(lock_call, ': # test fixture runs in-process so confirmation stdin remains available\n', 1)
    logging_call = 'start_logging_to "$LOG_FILE"\n'
    assert logging_call in source
    source = source.replace(logging_call, ': # test fixture keeps stdio attached\n', 1)
    script = tmp_path / "install-force-fresh.sh"; _executable(script, source)
    bindir = tmp_path / "bin"; bindir.mkdir()
    calls = tmp_path / "calls.log"
    _executable(bindir / "docker", "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$LIFECYCLE_CALLS\"\nexit 42\n")
    return script, home, bindir, calls


def test_force_fresh_compose_shutdown_failure_preserves_stack_checkpoint_and_config(tmp_path: Path) -> None:
    script, home, bindir, calls = _installer_through_force_fresh(tmp_path)
    stack = tmp_path / "stack"
    before_state = (home / ".azerothcore-install-state").read_text()
    before_config = (home / ".azerothcore-install-config").read_text()
    result = _run(
        script, "--force-fresh", input="WIPE\n",
        env={"HOME": str(home), "PATH": f"{bindir}:{os.environ['PATH']}", "LIFECYCLE_CALLS": str(calls)},
    )
    assert result.returncode == 1
    assert "Preserving" in result.stderr
    assert stack.is_dir()
    assert (home / ".azerothcore-install-state").read_text() == before_state
    assert (home / ".azerothcore-install-config").read_text() == before_config
    assert calls.read_text().splitlines() == ["compose down"]


def _root_uninstall_fixture(tmp_path: Path) -> tuple[Path, Path, Path, Path, Path]:
    stack = tmp_path / "stack"; stack.mkdir(); (stack / "docker-compose.yml").write_text("services: {}\n")
    state = tmp_path / "state"; state.write_text("phase=4\n")
    config = tmp_path / "config"; config.write_text("secret\n")
    bindir = tmp_path / "bin"; bindir.mkdir()
    root_guard = (
        'if [ "${EUID}" -eq 0 ]; then\n'
        '  echo "ERROR: Do not run this script with sudo/root."\n'
        '  echo "Run it as the same normal user that ran the installer, for example:"\n'
        '  echo "  ./uninstall-azerothcore.sh"\n'
        '  echo ""\n'
        '  echo "Reason: root changes HOME/crontab/state cleanup targets to /root."\n'
        '  exit 2\n'
        'fi\n\n'
    )
    source = UNINSTALL.read_text()
    assert root_guard in source
    systemd_assignment = 'SYSTEMD_UNIT="/etc/systemd/system/azerothcore.service"'
    assert systemd_assignment in source
    source = source.replace(
        systemd_assignment,
        f"SYSTEMD_UNIT={shlex.quote(str(tmp_path / 'azerothcore.service'))}",
        1,
    )
    original_safe_paths = (
        '    /opt/stacks/azerothcore|"${HOME}/.azerothcore-install-state"|"${HOME}/.azerothcore-install-config"|/tmp/ac-build.log)\n'
    )
    assert original_safe_paths in source
    source = source.replace(
        original_safe_paths,
        f"    {shlex.quote(str(stack))}|{shlex.quote(str(state))}|{shlex.quote(str(config))}|/tmp/ac-build.log)\n",
        1,
    )
    temp_root = tmp_path / "installer-tmp"; temp_root.mkdir()
    for original, replacement in (
        ("/tmp/azerothcore-install-*.log", str(temp_root / "azerothcore-install-*.log")),
        ("/tmp/ac-compose-effective.*.yml", str(temp_root / "ac-compose-effective.*.yml")),
        ("/tmp/ac-xp-rate-overrides.*", str(temp_root / "ac-xp-rate-overrides.*")),
        ("/tmp/ac-playerbots-schema-check.out", str(temp_root / "ac-playerbots-schema-check.out")),
        ("/opt/stacks/.azerothcore-clone-*", str(temp_root / ".azerothcore-clone-*")),
        ("/tmp/ac-build.log", str(temp_root / "ac-build.log")),
    ):
        source = source.replace(original, replacement)
    cleanup_step = "# 7) Remove known temporary files created by installer validation/build logging,\n"
    assert cleanup_step in source
    source = source.replace(cleanup_step, "exit 0 # fixture never touches host temporary cleanup globs\n", 1)
    script = tmp_path / "uninstall.sh"; _executable(script, source.replace(root_guard, "", 1))
    _executable(
        bindir / "sudo",
        "#!/bin/sh\n"
        "[ -z \"${SUDO_CALL_LOG:-}\" ] || printf '%s\\n' \"$*\" >> \"$SUDO_CALL_LOG\"\n"
        "[ \"${1:-}\" = -v ] && exit 0\n"
        "if [ \"${1:-}\" = rm ] && [ \"${2:-}\" = -rf ] && [ \"${3:-}\" = \"${SAFE_STACK:-}\" ]; then\n"
        "  exec /bin/rm -rf -- \"$SAFE_STACK\"\n"
        "fi\n"
        "echo \"fixture sudo refused unsafe command: $*\" >&2\nexit 99\n",
    )
    return script, stack, state, config, bindir


def test_root_uninstall_abort_and_dry_run_preserve_recovery_metadata(tmp_path: Path) -> None:
    script, stack, state, config, bindir = _root_uninstall_fixture(tmp_path)
    calls = tmp_path / "calls.log"
    _executable(bindir / "docker", "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$LIFECYCLE_CALLS\"\n[ \"$1 $2\" = 'compose version' ] && exit 0\nexit 1\n")
    _executable(bindir / "crontab", "#!/bin/sh\nexit 1\n")
    base = {"PATH": f"{bindir}:{os.environ['PATH']}", "STACK_DIR": str(stack), "STATE_FILE": str(state), "CONFIG_FILE": str(config), "LIFECYCLE_CALLS": str(calls)}
    aborted = _run(script, input="not-remove\n", env=base)
    assert aborted.returncode == 1 and "Aborted." in aborted.stdout
    assert not calls.exists()

    dry = _run(script, "--dry-run", env=base)
    assert dry.returncode == 0, dry.stderr
    assert stack.exists() and state.read_text() == "phase=4\n" and config.read_text() == "secret\n"
    assert "[dry-run] cd" in dry.stdout
    assert "sudo -v" not in dry.stdout


def test_root_uninstall_fixture_never_references_host_systemd_or_allows_host_removal(tmp_path: Path) -> None:
    script, stack, state, config, bindir = _root_uninstall_fixture(tmp_path)
    copied = script.read_text()
    assert "/etc/systemd/system/azerothcore.service" not in copied
    assert f"SYSTEMD_UNIT={shlex.quote(str(tmp_path / 'azerothcore.service'))}" in copied

    sudo_log = tmp_path / "sudo.log"
    _executable(bindir / "docker", "#!/bin/sh\nexit 1\n")
    _executable(bindir / "crontab", "#!/bin/sh\nexit 1\n")
    result = _run(
        script, "--yes",
        env={
            "PATH": str(bindir),
            "STACK_DIR": str(stack),
            "STATE_FILE": str(state),
            "CONFIG_FILE": str(config),
            "SUDO_CALL_LOG": str(sudo_log),
            "SAFE_STACK": str(stack),
        },
    )
    assert result.returncode == 1
    assert not sudo_log.exists()
    assert stack.exists() and state.exists() and config.exists()
    blocked = subprocess.run(
        [str(bindir / "sudo"), "rm", "-rf", "/etc/systemd/system/azerothcore.service"],
        text=True, capture_output=True, check=False,
        env={**os.environ, "SAFE_STACK": str(stack), "SUDO_CALL_LOG": str(sudo_log)},
    )
    assert blocked.returncode == 99
    assert "refused unsafe command" in blocked.stderr
    assert sudo_log.read_text().splitlines()[-1] == "rm -rf /etc/systemd/system/azerothcore.service"


def test_root_uninstall_reports_absent_docker_tool_and_preserves_recovery_context(tmp_path: Path) -> None:
    script, stack, state, config, bindir = _root_uninstall_fixture(tmp_path)
    _executable(bindir / "crontab", "#!/bin/sh\nexit 1\n")
    result = _run(
        script, "--yes",
        env={"PATH": str(bindir), "STACK_DIR": str(stack), "STATE_FILE": str(state), "CONFIG_FILE": str(config), "SAFE_STACK": str(stack)},
    )
    assert result.returncode == 1
    assert "Docker command is unavailable; preserving stack and installer state." in result.stderr
    assert "Uninstall incomplete. Recovery context was preserved" in result.stderr
    assert "[1/7]" not in result.stdout
    assert stack.exists() and state.read_text() == "phase=4\n" and config.read_text() == "secret\n"


def test_root_uninstall_preserves_damaged_stack_when_docker_daemon_is_down(tmp_path: Path) -> None:
    script, stack, state, config, bindir = _root_uninstall_fixture(tmp_path)
    (stack / "docker-compose.yml").unlink()
    unit = tmp_path / "azerothcore.service"; unit.write_text("[Service]\nExecStart=/bin/true\n")
    docker_calls = tmp_path / "docker.log"
    sudo_calls = tmp_path / "sudo.log"
    _executable(
        bindir / "docker",
        "#!/bin/sh\n"
        "printf '%s\\n' \"$*\" >> \"$DOCKER_CALLS\"\n"
        "echo 'Cannot connect to the Docker daemon' >&2\n"
        "exit 1\n",
    )
    _executable(bindir / "crontab", "#!/bin/sh\nexit 1\n")

    result = _run(
        script, "--yes",
        env={
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "STACK_DIR": str(stack),
            "STATE_FILE": str(state),
            "CONFIG_FILE": str(config),
            "SAFE_STACK": str(stack),
            "DOCKER_CALLS": str(docker_calls),
            "SUDO_CALL_LOG": str(sudo_calls),
        },
    )

    assert result.returncode == 1
    assert "Docker daemon is unavailable" in result.stderr
    assert "Uninstall incomplete. Recovery context was preserved" in result.stderr
    assert stack.exists() and state.read_text() == "phase=4\n" and config.read_text() == "secret\n"
    assert unit.read_text() == "[Service]\nExecStart=/bin/true\n"
    assert docker_calls.read_text().splitlines() == ["info"]
    assert not sudo_calls.exists()


def test_root_uninstall_allows_clean_state_cleanup_without_docker(tmp_path: Path) -> None:
    script, stack, state, config, bindir = _root_uninstall_fixture(tmp_path)
    (stack / "docker-compose.yml").unlink()
    stack.rmdir()
    state.unlink()
    config.unlink()
    _executable(bindir / "crontab", "#!/bin/sh\nexit 1\n")
    _executable(bindir / "rm", "#!/bin/sh\nexit 0\n")
    _executable(bindir / "cat", "#!/bin/sh\nexit 0\n")

    result = _run(
        script, "--yes",
        env={
            "PATH": str(bindir),
            "STACK_DIR": str(stack),
            "STATE_FILE": str(state),
            "CONFIG_FILE": str(config),
            "SAFE_STACK": str(stack),
        },
    )

    assert result.returncode == 0, result.stderr
    assert "No compose file found" in result.stdout
    assert "Docker command not found; skipping Docker fallback cleanup." in result.stdout
    assert "Uninstall incomplete" not in result.stderr


def test_root_uninstall_succeeds_without_unit_or_crontab_and_removes_only_sandbox_state(tmp_path: Path) -> None:
    script, stack, state, config, bindir = _root_uninstall_fixture(tmp_path)
    systemctl_calls = tmp_path / "systemctl.log"
    unrelated = tmp_path / "unrelated-metadata"; unrelated.write_text("keep\n")
    _executable(
        bindir / "docker",
        "#!/bin/sh\n"
        "[ \"$1\" = info ] && exit 0\n"
        "case \"$1 $2\" in\n"
        "  'compose version'|'compose -p') exit 0 ;;\n"
        "  'network ls'|'volume ls') exit 0 ;;\n"
        "  *) exit 1 ;;\n"
        "esac\n",
    )
    _executable(bindir / "crontab", "#!/bin/sh\nexit 1\n")
    _executable(bindir / "systemctl", "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$SYSTEMCTL_CALLS\"\nexit 99\n")
    result = _run(
        script, "--yes",
        env={
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "STACK_DIR": str(stack),
            "STATE_FILE": str(state),
            "CONFIG_FILE": str(config),
            "SYSTEMCTL_CALLS": str(systemctl_calls),
            "SAFE_STACK": str(stack),
        },
    )
    assert result.returncode == 0, result.stderr
    assert "No azerothcore.service unit found." in result.stdout
    assert "No crontab found for current user." in result.stdout
    assert not systemctl_calls.exists()
    assert not stack.exists() and not state.exists() and not config.exists()
    assert unrelated.read_text() == "keep\n"


def test_admin_uninstall_dry_run_preserves_ac_env_mode_and_admin_files(tmp_path: Path) -> None:
    ac_stack = tmp_path / "ac"; ac_stack.mkdir()
    admin_stack = tmp_path / "admin"; admin_stack.mkdir()
    env_file = ac_stack / ".env"; env_file.write_text("COMPOSE_FILE=docker-compose.yml:docker-compose.admin.yml\n"); env_file.chmod(0o640)
    admin_yml = ac_stack / "docker-compose.admin.yml"; admin_yml.write_text("services: {}\n")
    source = ADMIN_UNINSTALL.read_text().replace("STACK_DIR=/opt/stacks/azerothcore-admin", f"STACK_DIR={shlex.quote(str(admin_stack))}", 1).replace("AC_STACK_DIR=/opt/stacks/azerothcore", f"AC_STACK_DIR={shlex.quote(str(ac_stack))}", 1)
    script = tmp_path / "uninstall-admin.sh"; _executable(script, source)
    bindir = tmp_path / "bin"; bindir.mkdir()
    _executable(bindir / "docker", "#!/bin/sh\nexit 0\n")
    _executable(bindir / "sudo", "#!/bin/sh\necho unexpected-sudo >&2\nexit 99\n")
    before = env_file.stat()
    result = _run(script, "--dry-run", env={"PATH": f"{bindir}:{os.environ['PATH']}"})
    assert result.returncode == 0, result.stderr
    assert env_file.read_text() == "COMPOSE_FILE=docker-compose.yml:docker-compose.admin.yml\n"
    assert stat.S_IMODE(env_file.stat().st_mode) == stat.S_IMODE(before.st_mode)
    assert admin_yml.exists() and admin_stack.exists()


def test_root_systemd_heredoc_contains_daemon_restart_and_graceful_stop_contract(tmp_path: Path) -> None:
    source = INSTALLER.read_text()
    start = source.index("# PHASE 8 — Systemd auto-start")
    end = source.index("# ============================================================================\n# Finalisation", start)
    phase = source[start:end]
    script = tmp_path / "phase8.sh"
    _executable(
        script,
        "#!/bin/bash\nset -euo pipefail\n"
        f"ENABLE_SYSTEMD=y\nSTACK_DIR={shlex.quote(str(tmp_path / 'stack'))}\n"
        "should_run_phase() { [ \"$1\" = 8 ]; }\n"
        "banner() { :; }\nmark_phase_complete() { :; }\n"
        + phase,
    )
    (tmp_path / "stack").mkdir()
    bindir = tmp_path / "bin"; bindir.mkdir(); unit = tmp_path / "unit"; calls = tmp_path / "calls.log"
    _executable(
        bindir / "sudo",
        "#!/bin/sh\n"
        "if [ \"$1\" = tee ]; then cat > \"$UNIT_CAPTURE\"; exit 0; fi\n"
        "if [ \"$1\" = sed ]; then shift; [ \"$1\" = -i ] && { sed -i \"$2\" \"$UNIT_CAPTURE\"; exit; }; fi\n"
        "printf '%s\\n' \"$*\" >> \"$LIFECYCLE_CALLS\"\nexit 0\n",
    )
    _executable(bindir / "docker", "#!/bin/sh\n[ \"$1 $2\" = 'compose config' ] && { printf '%s\\n' phpmyadmin ac-eluna-ts-dev; exit 0; }\nexit 1\n")
    _executable(bindir / "whoami", "#!/bin/sh\necho tester\n")
    result = _run(script, env={"PATH": f"{bindir}:{os.environ['PATH']}", "UNIT_CAPTURE": str(unit), "LIFECYCLE_CALLS": str(calls)})
    assert result.returncode == 0, result.stderr
    rendered = unit.read_text()
    assert "PartOf=docker.service" in rendered
    assert "ExecStop=/usr/bin/docker compose down --timeout 60" in rendered
    assert "TimeoutStopSec=75" in rendered
    assert "--scale phpmyadmin=0 --scale ac-eluna-ts-dev=0" in rendered
    assert "REPLACE_WITH" not in rendered
