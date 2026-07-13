"""Hermetic lifecycle coverage for installer checkpoints, adoption, and init jobs.

The fixture copies the installer, redirects its stack directory to ``tmp_path``,
and stops before phase 0.2. Commands that could mutate the host or use Docker
or sudo are stubbed.
"""

import os
import re
import stat
import subprocess
from pathlib import Path


SCRIPTS_DIR = (
    Path("/src")
    if Path("/src/install-azerothcore.sh").is_file()
    else Path(__file__).resolve().parents[1]
)
INSTALLER = SCRIPTS_DIR / "install-azerothcore.sh"

ROOT_GUARD = (
    'if [ "${EUID}" -eq 0 ]; then\n'
    '    echo "ERROR: Do not run this installer with sudo or as root." >&2\n'
    '    echo "Run it as your normal user; the script will ask for sudo when needed." >&2\n'
    '    exit 2\n'
    'fi\n\n'
)

SAVED_CONFIG = """\
DB_ROOT_PASSWORD=DbSecret123
GM_USERNAME=GameMaster
GM_PASSWORD=GamePass123
AHBOT_PASSWORD=AhbotPass123
PLAYERBOT_COUNT=1
SERVER_XP_RATE=x5
SERVER_PVP=y
INNODB_BUFFER_POOL_SIZE=1G
MAP_UPDATE_THREADS=1
AHBOT_CHARACTER_COUNT=1
INSTALL_UFW=n
ENABLE_SYSTEMD=n
TAILSCALE_IP=100.64.0.5
"""

def _write_executable(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _installer_fixture(tmp_path: Path) -> Path:
    """Copy the production installer with test-only path and phase hooks."""
    source = INSTALLER.read_text()
    stack_dir = 'STACK_DIR="/opt/stacks/azerothcore"'
    assert stack_dir in source
    source = source.replace(stack_dir, f'STACK_DIR="{tmp_path / "stack"}"', 1)
    assert ROOT_GUARD in source
    source = source.replace(ROOT_GUARD, "", 1)
    initial_log = 'LOG_FILE="/tmp/azerothcore-install-${UNIX_TS}.log"'
    assert initial_log in source
    source = source.replace(
        initial_log, 'LOG_FILE="${TMPDIR}/azerothcore-install-${UNIX_TS}.log"', 1
    )
    logging_call = 'start_logging_to "$LOG_FILE"\n'
    assert logging_call in source
    source = source.replace(logging_call, ': # test fixture keeps stdout/stderr synchronous\n', 1)

    capacity_metrics_pattern = re.compile(
        r"opt_free_kib\(\) \{.*?\n\}\n\n"
        r"physical_ram_kib\(\) \{.*?\n\}\n\n",
        re.S,
    )
    test_capacity_metrics = """opt_free_kib() {
    printf '%s\\n' "${INSTALLER_TEST_OPT_FREE_KIB}"
}

physical_ram_kib() {
    printf '%s\\n' "${INSTALLER_TEST_MEM_TOTAL_KIB}"
}

"""
    source, metric_replacements = capacity_metrics_pattern.subn(test_capacity_metrics, source)
    assert metric_replacements == 1
    interactive_check = '    if [ -t 0 ]; then\n'
    assert interactive_check in source
    source = source.replace(
        interactive_check,
        '    if [ -t 0 ] || [ "${INSTALLER_TEST_ASSUME_INTERACTIVE:-0}" = "1" ]; then\n',
        1,
    )

    phase_01_banner = '    banner "0.1" "OS version check"\n'
    assert phase_01_banner in source
    source = source.replace(
        phase_01_banner,
        phase_01_banner
        + '    if [ "${INSTALLER_TEST_FAIL_PHASE:-}" = "0.1" ]; then false; fi\n',
        1,
    )
    phase_02 = 'if should_run_phase "0.2"; then\n'
    assert phase_02 in source
    source = source.replace(
        phase_02,
        'if [ "${INSTALLER_TEST_STOP_BEFORE_02:-0}" = 1 ]; then clean_exit 0; fi\n\n'
        + phase_02,
        1,
    )
    keepalive = (
        '( while true; do sudo -n true 2>/dev/null || exit; sleep 60; done ) &\n'
        'KEEPALIVE_PID=$!\n'
    )
    assert keepalive in source
    source = source.replace(
        keepalive,
        'KEEPALIVE_PID="" # test fixture never needs a sudo timestamp refresh\n',
        1,
    )
    adopt_prompt = (
        'else\n'
        '    echo ""\n'
        '    echo "════════════════════════════════════════════════════════════════"\n'
        '    echo "Interactive configuration"\n'
    )
    assert adopt_prompt in source
    source = source.replace(
        adopt_prompt,
        'elif [ "$ADOPT" = true ] && [ "${INSTALLER_TEST_ADOPT_VALUES:-0}" = 1 ]; then\n'
        '    DB_ROOT_PASSWORD=DbSecret123\n'
        '    GM_USERNAME=GameMaster\n'
        '    GM_PASSWORD=GamePass123\n'
        '    AHBOT_PASSWORD=AhbotPass123\n'
        '    PLAYERBOT_COUNT=1\n'
        '    SERVER_XP_RATE=x5\n'
        '    SERVER_PVP=y\n'
        '    INNODB_BUFFER_POOL_SIZE=1G\n'
        '    MAP_UPDATE_THREADS=1\n'
        '    AHBOT_CHARACTER_COUNT=1\n'
        '    INSTALL_UFW=n\n'
        '    ENABLE_SYSTEMD=n\n'
        '    TAILSCALE_IP=100.64.0.5\n'
        + adopt_prompt,
        1,
    )

    config_probe_marker = (
        '# ============================================================================\n'
        '# --force-fresh: wipe state and start over\n'
    )
    assert config_probe_marker in source
    config_probe = (
        'if [ "${INSTALLER_TEST_CONFIG_PROBE:-0}" = 1 ]; then\n'
        '    DB_ROOT_PASSWORD=DbSecret123\n'
        '    GM_USERNAME=GameMaster\n'
        '    GM_PASSWORD=GamePass123\n'
        '    AHBOT_PASSWORD=AhbotPass123\n'
        '    PLAYERBOT_COUNT=1\n'
        '    SERVER_XP_RATE=x5\n'
        '    SERVER_PVP=y\n'
        '    INNODB_BUFFER_POOL_SIZE=1G\n'
        '    MAP_UPDATE_THREADS=1\n'
        '    AHBOT_CHARACTER_COUNT=1\n'
        '    INSTALL_UFW=n\n'
        '    ENABLE_SYSTEMD=n\n'
        '    TAILSCALE_IP=100.64.0.5\n'
        '    save_config\n'
        '    unset DB_ROOT_PASSWORD GM_USERNAME GM_PASSWORD AHBOT_PASSWORD\n'
        '    load_config\n'
        '    [ "$GM_USERNAME" = GameMaster ] && [ "$TAILSCALE_IP" = 100.64.0.5 ]\n'
        '    clean_exit $?\n'
        'fi\n\n'
        + config_probe_marker
    )
    source = source.replace(config_probe_marker, config_probe, 1)

    init_job_end = (
        '        elapsed=$((elapsed + poll_interval))\n'
        '    done\n'
        '}\n\n'
        '# ============================================================================\n'
        '# Helper: compute "--scale svc=0" args for services we don\'t want running.\n'
    )
    assert init_job_end in source
    init_probe = (
        '        elapsed=$((elapsed + poll_interval))\n'
        '    done\n'
        '}\n\n'
        'if [ -n "${INSTALLER_TEST_INIT_PROBE:-}" ]; then\n'
        '    if wait_for_init_container "ac-init" 0 "Test init job"; then\n'
        '        clean_exit 0\n'
        '    fi\n'
        '    clean_exit 1\n'
        'fi\n\n'
        '# ============================================================================\n'
        '# Helper: compute "--scale svc=0" args for services we don\'t want running.\n'
    )
    source = source.replace(init_job_end, init_probe, 1)

    fixture = tmp_path / "install-azerothcore.sh"
    _write_executable(fixture, source)
    return fixture


def _make_stubs(bindir: Path) -> None:
    _write_executable(
        bindir / "sudo",
        "#!/bin/sh\n"
        "# The lifecycle harness must never invoke host sudo.\n"
        "exit 0\n",
    )
    _write_executable(
        bindir / "docker",
        """#!/bin/sh
printf '%s\\n' "$*" >> "$INSTALLER_TEST_DOCKER_LOG"
if [ "${INSTALLER_TEST_DOCKER_MODE:-ok}" = fail ]; then exit 1; fi
case "${1:-}" in
  --version) echo 'Docker version 25.0.0'; exit 0 ;;
  compose) echo 'Docker Compose version v2.0.0'; exit 0 ;;
  version) echo '25.0.0'; exit 0 ;;
  run) echo 'Hello from Docker!'; exit 0 ;;
  inspect)
    case "$*" in
      *ExitCode*) echo "${INSTALLER_TEST_INIT_EXIT_CODE:-0}" ;;
      *) echo "${INSTALLER_TEST_INIT_STATE:-running}" ;;
    esac
    exit 0 ;;
  logs) echo 'stub init-container log'; exit 0 ;;
esac
exit 0
""",
    )
    _write_executable(
        bindir / "tailscale",
        """#!/bin/sh
if [ "${INSTALLER_TEST_DOCKER_MODE:-ok}" = fail ]; then exit 1; fi
case "${1:-}" in
  version) echo '1.0.0' ;;
  ip) echo '100.64.0.5' ;;
esac
""",
    )
    _write_executable(
        bindir / "lsb_release",
        """#!/bin/sh
case "${1:-}" in
  -cs) echo jammy ;;
  -rs) echo 22.04 ;;
  *) echo 'Ubuntu 22.04' ;;
esac
""",
    )
    _write_executable(bindir / "groups", "#!/bin/sh\necho 'tester docker'\n")
    _write_executable(bindir / "crontab", "#!/bin/sh\nexit 1\n")


def _prepare(tmp_path: Path, *, config: str | None = SAVED_CONFIG) -> tuple[Path, Path, Path]:
    installer = _installer_fixture(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    if config is not None:
        home.joinpath(".azerothcore-install-config").write_text(config)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _make_stubs(bindir)
    return installer, home, bindir


def _run(
    installer: Path,
    home: Path,
    bindir: Path,
    *args: str,
    input_text: str | None = None,
    **extra_env: str,
) -> subprocess.CompletedProcess[str]:
    docker_log = home / "docker-calls.log"
    tmpdir = home / "tmp"
    tmpdir.mkdir(exist_ok=True)
    command = ["bash", str(installer), *args]
    run_kwargs = {
        "capture_output": True,
        "text": True,
        "timeout": 10,
        "env": {
            **os.environ,
            "HOME": str(home),
            "USER": "tester",
            "TMPDIR": str(tmpdir),
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "INSTALLER_TEST_DOCKER_LOG": str(docker_log),
            "INSTALLER_TEST_OPT_FREE_KIB": str(50 * 1024 * 1024),
            "INSTALLER_TEST_MEM_TOTAL_KIB": str(16 * 1024 * 1024),
            **extra_env,
        },
    }
    if input_text is None:
        run_kwargs["stdin"] = subprocess.DEVNULL
    else:
        run_kwargs["input"] = input_text
    return subprocess.run(command, **run_kwargs)


def _run_to_phase_02(
    tmp_path: Path, *, resume_from: str | None = None, state: str = ""
) -> tuple[subprocess.CompletedProcess[str], Path]:
    installer, home, bindir = _prepare(tmp_path)
    if state:
        home.joinpath(".azerothcore-install-state").write_text(state)
    args = () if resume_from is None else (f"--resume-from={resume_from}",)
    result = _run(
        installer, home, bindir, *args, INSTALLER_TEST_STOP_BEFORE_02="1"
    )
    return result, home


def test_fixture_does_not_create_installer_logs_under_host_tmp(tmp_path: Path):
    installer, home, bindir = _prepare(tmp_path)
    host_logs_before = set(Path("/tmp").glob("azerothcore-install-*"))

    result = _run(
        installer, home, bindir, INSTALLER_TEST_STOP_BEFORE_02="1"
    )

    assert result.returncode == 0, result.stderr
    assert set(Path("/tmp").glob("azerothcore-install-*")) == host_logs_before
    assert list((home / "tmp").glob("azerothcore-install-*.log"))


def test_resume_from_replaces_stale_checkpoint_and_ignores_malformed_lines(tmp_path: Path):
    result, home = _run_to_phase_02(
        tmp_path,
        resume_from="0.0",
        state="0.0|stale|old run\nnot-a-checkpoint\n0.0 stale\n",
    )

    assert result.returncode == 0, result.stderr
    state = (home / ".azerothcore-install-state").read_text()
    assert state.count("0.0|") == 1
    assert "0.1|" in state


def test_malformed_checkpoint_does_not_skip_its_phase(tmp_path: Path):
    result, home = _run_to_phase_02(tmp_path, state="0.0 stale\n")

    assert result.returncode == 0, result.stderr
    assert "[Phase 0.0] Pre-flight checks" in result.stdout
    assert "0.0|" in home.joinpath(".azerothcore-install-state").read_text()


def test_completed_checkpoint_skips_phase_without_resume_override(tmp_path: Path):
    result, home = _run_to_phase_02(tmp_path, state="0.0|seed|done\n")

    assert result.returncode == 0, result.stderr
    assert "[Phase 0.0] Already complete — skipping." in result.stdout
    assert "[Phase 0.1] OS version check" in result.stdout
    assert "0.1|" in home.joinpath(".azerothcore-install-state").read_text()


def test_failed_phase_keeps_checkpoint_incomplete_and_uses_err_trap(tmp_path: Path):
    installer, home, bindir = _prepare(tmp_path)
    result = _run(
        installer,
        home,
        bindir,
        "--resume-from=0.1",
        INSTALLER_TEST_FAIL_PHASE="0.1",
    )

    assert result.returncode != 0
    assert "✗ FAILED at 0.1" in result.stdout
    assert "Resume with:" in result.stdout
    state = home / ".azerothcore-install-state"
    assert not state.exists() or "0.1|" not in state.read_text()


def test_saved_prompt_config_is_written_then_loaded_for_recovery(tmp_path: Path):
    installer, home, bindir = _prepare(tmp_path, config=None)
    first = _run(
        installer,
        home,
        bindir,
        INSTALLER_TEST_CONFIG_PROBE="1",
    )

    config = home / ".azerothcore-install-config"
    assert first.returncode == 0, first.stderr
    assert "GM_USERNAME=GameMaster" in config.read_text()
    assert stat.S_IMODE(config.stat().st_mode) == 0o600

    resumed = _run(
        installer,
        home,
        bindir,
        "--resume-from=0.1",
        INSTALLER_TEST_STOP_BEFORE_02="1",
    )
    assert resumed.returncode == 0, resumed.stderr
    assert "Loading saved prompt answers" in resumed.stdout
    assert "0.1|" in home.joinpath(".azerothcore-install-state").read_text()


def test_adoption_failure_does_not_mark_state_and_uses_clean_exit(tmp_path: Path):
    installer, home, bindir = _prepare(tmp_path, config=None)
    (tmp_path / "stack").mkdir()
    state = home / ".azerothcore-install-state"
    state.write_text("0.0|seed|existing checkpoint\n")
    result = _run(
        installer,
        home,
        bindir,
        "--adopt",
        INSTALLER_TEST_DOCKER_MODE="fail",
        INSTALLER_TEST_ADOPT_VALUES="1",
    )

    assert result.returncode == 1
    assert "Adoption aborted" in result.stdout
    assert "✗ FAILED at" not in result.stdout
    assert state.read_text() == "0.0|seed|existing checkpoint\n"


def test_init_container_timeout_is_bounded_and_reports_logs(tmp_path: Path):
    installer, home, bindir = _prepare(tmp_path)
    result = _run(
        installer,
        home,
        bindir,
        INSTALLER_TEST_INIT_PROBE="1",
        INSTALLER_TEST_INIT_STATE="running",
    )

    assert result.returncode == 1
    assert "did not finish within 0s" in result.stdout
    assert "stub init-container log" in result.stdout


def test_init_container_nonzero_exit_is_reported_without_live_docker(tmp_path: Path):
    installer, home, bindir = _prepare(tmp_path)
    result = _run(
        installer,
        home,
        bindir,
        INSTALLER_TEST_INIT_PROBE="1",
        INSTALLER_TEST_INIT_STATE="exited",
        INSTALLER_TEST_INIT_EXIT_CODE="42",
    )

    assert result.returncode == 1
    assert "exited with code 42" in result.stdout
    assert "stub init-container log" in result.stdout
    assert "inspect" in home.joinpath("docker-calls.log").read_text()


GIB_IN_KIB = 1024 * 1024


def test_capacity_preflight_hard_fails_below_25_gib_free_on_opt(tmp_path: Path):
    installer, home, bindir = _prepare(tmp_path)

    result = _run(
        installer,
        home,
        bindir,
        INSTALLER_TEST_STOP_BEFORE_02="1",
        INSTALLER_TEST_OPT_FREE_KIB=str(25 * GIB_IN_KIB - 1),
        INSTALLER_TEST_MEM_TOTAL_KIB=str(16 * GIB_IN_KIB),
    )

    assert result.returncode == 1
    assert "ERROR: /opt has less than 25 GiB free" in result.stderr


def test_capacity_hard_minimum_cannot_be_overridden(tmp_path: Path):
    installer, home, bindir = _prepare(tmp_path)

    result = _run(
        installer,
        home,
        bindir,
        "--allow-capacity-warnings",
        INSTALLER_TEST_STOP_BEFORE_02="1",
        INSTALLER_TEST_OPT_FREE_KIB=str(25 * GIB_IN_KIB - 1),
        INSTALLER_TEST_MEM_TOTAL_KIB=str(16 * GIB_IN_KIB),
    )

    assert result.returncode == 1
    assert "ERROR: /opt has less than 25 GiB free" in result.stderr
    assert "Capacity warnings explicitly overridden." not in result.stdout


def test_capacity_preflight_warns_at_25_to_less_than_50_gib_and_accepts_confirmation(
    tmp_path: Path,
):
    installer, home, bindir = _prepare(tmp_path)

    result = _run(
        installer,
        home,
        bindir,
        input_text="yes\n",
        INSTALLER_TEST_STOP_BEFORE_02="1",
        INSTALLER_TEST_ASSUME_INTERACTIVE="1",
        INSTALLER_TEST_OPT_FREE_KIB=str(25 * GIB_IN_KIB),
        INSTALLER_TEST_MEM_TOTAL_KIB=str(16 * GIB_IN_KIB),
    )

    assert result.returncode == 0, result.stderr
    assert "WARNING: /opt has only 25 GiB free" in result.stdout
    assert "Capacity warning acknowledged." in result.stdout


def test_capacity_preflight_does_not_warn_at_50_gib_free_on_opt(tmp_path: Path):
    installer, home, bindir = _prepare(tmp_path)

    result = _run(
        installer,
        home,
        bindir,
        INSTALLER_TEST_STOP_BEFORE_02="1",
        INSTALLER_TEST_OPT_FREE_KIB=str(50 * GIB_IN_KIB),
        INSTALLER_TEST_MEM_TOTAL_KIB=str(16 * GIB_IN_KIB),
    )

    assert result.returncode == 0, result.stderr
    assert "Capacity warning" not in result.stdout


def test_capacity_warnings_fail_without_a_terminal_unless_explicitly_overridden(
    tmp_path: Path,
):
    installer, home, bindir = _prepare(tmp_path)
    warning_env = {
        "INSTALLER_TEST_STOP_BEFORE_02": "1",
        "INSTALLER_TEST_OPT_FREE_KIB": str(49 * GIB_IN_KIB),
        "INSTALLER_TEST_MEM_TOTAL_KIB": str(16 * GIB_IN_KIB),
    }

    blocked = _run(installer, home, bindir, **warning_env)
    allowed = _run(
        installer,
        home,
        bindir,
        "--resume-from=0.0",
        "--allow-capacity-warnings",
        **warning_env,
    )

    assert blocked.returncode == 1
    assert "requires interactive confirmation" in blocked.stderr
    assert allowed.returncode == 0, allowed.stderr
    assert "Capacity warnings explicitly overridden." in allowed.stdout


def test_buffer_pool_over_half_of_physical_ram_warns_and_requires_confirmation(
    tmp_path: Path,
):
    config = SAVED_CONFIG.replace("INNODB_BUFFER_POOL_SIZE=1G", "INNODB_BUFFER_POOL_SIZE=9G")
    installer, home, bindir = _prepare(tmp_path, config=config)
    warning_env = {
        "INSTALLER_TEST_STOP_BEFORE_02": "1",
        "INSTALLER_TEST_OPT_FREE_KIB": str(50 * GIB_IN_KIB),
        "INSTALLER_TEST_MEM_TOTAL_KIB": str(16 * GIB_IN_KIB),
    }

    blocked = _run(installer, home, bindir, **warning_env)
    confirmed = _run(
        installer,
        home,
        bindir,
        "--resume-from=0.0",
        input_text="yes\n",
        INSTALLER_TEST_ASSUME_INTERACTIVE="1",
        **warning_env,
    )
    allowed = _run(
        installer,
        home,
        bindir,
        "--resume-from=0.0",
        "--allow-capacity-warnings",
        **warning_env,
    )

    assert blocked.returncode == 1
    assert "InnoDB buffer pool size 9G exceeds 50% of physical RAM" in blocked.stdout
    assert "requires interactive confirmation" in blocked.stderr
    assert confirmed.returncode == 0, confirmed.stderr
    assert "Capacity warning acknowledged." in confirmed.stdout
    assert allowed.returncode == 0, allowed.stderr


def test_buffer_pool_at_half_of_physical_ram_does_not_warn(tmp_path: Path):
    config = SAVED_CONFIG.replace("INNODB_BUFFER_POOL_SIZE=1G", "INNODB_BUFFER_POOL_SIZE=8G")
    installer, home, bindir = _prepare(tmp_path, config=config)

    result = _run(
        installer,
        home,
        bindir,
        INSTALLER_TEST_STOP_BEFORE_02="1",
        INSTALLER_TEST_OPT_FREE_KIB=str(50 * GIB_IN_KIB),
        INSTALLER_TEST_MEM_TOTAL_KIB=str(16 * GIB_IN_KIB),
    )

    assert result.returncode == 0, result.stderr
    assert "Capacity warning" not in result.stdout
