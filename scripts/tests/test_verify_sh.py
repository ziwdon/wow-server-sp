import os
import shlex
import shutil
import stat
import subprocess
from pathlib import Path

import pytest


SCRIPTS_DIR = Path("/src") if Path("/src/verify-azerothcore.sh").is_file() else Path(__file__).resolve().parents[1]
VERIFY_SH = SCRIPTS_DIR / "verify-azerothcore.sh"


def _executable(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _stack(tmp_path: Path) -> Path:
    stack = tmp_path / "stack"
    (stack / "logs").mkdir(parents=True)
    (stack / "backups").mkdir()
    (stack / "configs" / "mysql").mkdir(parents=True)
    (stack / "configs" / "modules").mkdir()
    stack.joinpath(".env").write_text(
        "DOCKER_DB_ROOT_PASSWORD=test\n"
        "DOCKER_DB_EXTERNAL_PORT=127.0.0.1:3306\n"
        "DOCKER_SOAP_EXTERNAL_PORT=127.0.0.1:7878\n"
        "DOCKER_AUTH_EXTERNAL_PORT=100.64.0.5:3724\n"
        "DOCKER_WORLD_EXTERNAL_PORT=100.64.0.5:8085\n"
    )
    stack.joinpath("logs", "Server.log").write_text("WORLD: World Initialized\n")
    stack.joinpath("logs", "Errors.log").write_text("")
    stack.joinpath("configs", "mysql", "custom.cnf").write_text(
        "innodb_buffer_pool_size = 1G\ninnodb_buffer_pool_instances = 1\n"
    )
    stack.joinpath("configs", "modules", "mod_ahbot.conf").write_text("AuctionHouseBot.GUIDs = 1\n")
    stack.joinpath("configs", "modules", "playerbots.conf").write_text("# seeded\n")
    stack.joinpath("docker-compose.override.yml").write_text("services: {}\n")
    _executable(stack / "backup.sh", "#!/bin/sh\nexit 0\n")
    return stack


def _stubs(tmp_path: Path, *, ss_output: str = "", include_tailscale: bool = True) -> Path:
    bind = tmp_path / "bin"
    bind.mkdir(exist_ok=True)
    _executable(bind / "docker", """#!/bin/bash
printf 'docker %s\\n' "$*" >> "${VERIFY_CALL_LOG:-/dev/null}"
case "$1" in
  inspect)
    case "$*" in *ExitCode*) echo 0 ;; *StartedAt*) echo 2026-07-12T00:00:00Z ;; *) echo running ;; esac ;;
  exec) exit 0 ;;
  compose) exit 0 ;;
  images) exit 0 ;;
esac
""")
    if include_tailscale:
        _executable(bind / "tailscale", "#!/bin/sh\necho 100.64.0.5\n")
    _executable(bind / "crontab", "#!/bin/sh\necho '0 3 * * * /opt/stacks/azerothcore/backup.sh'\n")
    _executable(bind / "ss", f"#!/bin/sh\nprintf '%s\\n' {shlex.quote(ss_output)}\n")
    _executable(bind / "systemctl", "#!/bin/sh\nexit 0\n")
    for command in ("awk", "date", "find", "grep", "gzip", "head", "ls", "paste", "python3", "sed", "sort", "tail", "tar", "tr", "wc"):
        target = shutil.which(command)
        assert target, f"test prerequisite missing: {command}"
        if not (bind / command).exists():
            (bind / command).symlink_to(target)
    return bind


def _run(stack: Path, bind: Path, **extra_env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/bash", str(VERIFY_SH)],
        env={
            **os.environ,
            "STACK_DIR": str(stack),
            "PATH": str(bind),
            "VERIFY_CALL_LOG": str(bind.parent / "calls.log"),
            **extra_env,
        },
        capture_output=True,
        text=True,
    )


def _assert_complete_summary(result: subprocess.CompletedProcess[str]) -> None:
    assert "TOTAL:" in result.stdout
    counted = result.stdout.count("[OK]") + result.stdout.count("[FAIL]")
    reported = int(result.stdout.rsplit("TOTAL: ", 1)[1].split()[0])
    assert reported == counted


def test_missing_required_env_key_reports_failure_and_summary(tmp_path):
    stack = _stack(tmp_path)
    stack.joinpath(".env").write_text("DOCKER_DB_ROOT_PASSWORD=test\n")
    result = _run(stack, _stubs(tmp_path))

    assert result.returncode == 1
    assert ".env is missing required DOCKER_AUTH_EXTERNAL_PORT" in result.stdout
    assert "unbound variable" not in result.stderr
    _assert_complete_summary(result)


def test_failure_paths_accumulate_through_malformed_bindings_and_missing_tools(tmp_path):
    stack = _stack(tmp_path)
    stack.joinpath(".env").write_text(
        "DOCKER_DB_ROOT_PASSWORD=\n"
        "DOCKER_DB_EXTERNAL_PORT=127.0.0.1:3306\n"
        "DOCKER_SOAP_EXTERNAL_PORT=not-an-address\n"
        "DOCKER_AUTH_EXTERNAL_PORT=100.64.0.5:3724\n"
        "DOCKER_WORLD_EXTERNAL_PORT=100.64.0.5:8085\n"
    )
    bind = _stubs(
        tmp_path,
        ss_output="State Recv-Q Send-Q Local Address:Port Peer Address:Port\nLISTEN 0 0 0.0.0.0:3306 0.0.0.0:*",
        include_tailscale=False,
    )

    result = _run(stack, bind)

    assert result.returncode == 1
    assert ".env is missing required DOCKER_DB_ROOT_PASSWORD" in result.stdout
    assert "tailscale binary not installed" in result.stdout
    assert "SOAP .env value is malformed (no addr:port): 'not-an-address'" in result.stdout
    assert "Port 3306 (MySQL) listening on unexpected scope: 0.0.0.0" in result.stdout
    assert "RESULT: FAIL" in result.stdout
    _assert_complete_summary(result)
    assert "docker inspect --format={{.State.Status}} ac-database" in (tmp_path / "calls.log").read_text()


def test_running_worldserver_without_current_boot_readiness_fails(tmp_path):
    stack = _stack(tmp_path)
    stack.joinpath("logs", "Server.log").write_text("booting\n")
    result = _run(stack, _stubs(tmp_path))

    assert result.returncode == 1
    assert "ac-worldserver has not reached World Initialized" in result.stdout
    _assert_complete_summary(result)


@pytest.mark.parametrize("backup_setup", ["missing_dir", "empty", "corrupt"])
def test_general_verification_does_not_read_or_require_backup_archives(tmp_path, backup_setup):
    control_stack = _stack(tmp_path / "control")
    control = _run(control_stack, _stubs(tmp_path / "control"))

    stack = _stack(tmp_path / "case")
    if backup_setup == "missing_dir":
        shutil.rmtree(stack / "backups")
    elif backup_setup == "corrupt":
        (stack / "backups" / "azerothcore-backup-daily-corrupt.tar.gz").write_bytes(b"not gzip")

    bind = _stubs(tmp_path / "case")
    python_log = tmp_path / "python.log"
    real_python = shutil.which("python3")
    assert real_python
    (bind / "python3").unlink()
    _executable(
        bind / "python3",
        f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$VERIFY_PYTHON_LOG\"\nexec {real_python} \"$@\"\n",
    )

    result = _run(stack, bind, VERIFY_PYTHON_LOG=str(python_log))

    assert result.returncode == control.returncode
    assert result.stdout.count("[FAIL]") == control.stdout.count("[FAIL]")
    assert "fresh complete canonical archive" not in result.stdout
    assert not python_log.exists() or str(stack / "backups") not in python_log.read_text()
    _assert_complete_summary(result)


def test_actionable_errors_log_fails_but_known_graveyard_noise_is_advisory(tmp_path):
    stack = _stack(tmp_path)
    errors = stack / "logs" / "Errors.log"
    errors.write_text("[ERROR] database connection lost\n")
    failed = _run(stack, _stubs(tmp_path))
    assert "Errors.log has actionable runtime errors" in failed.stdout
    _assert_complete_summary(failed)

    errors.write_text(
        "Table `graveyard_zone` incomplete: Zone 2037 Team 0 does not have a linked graveyard\n"
    )
    advisory = _run(stack, _stubs(tmp_path))
    assert "known graveyard_zone data-gap warning" in advisory.stdout
    _assert_complete_summary(advisory)


def test_errors_log_advisory_flag_downgrades_actionable_errors(tmp_path):
    stack = _stack(tmp_path)
    errors = stack / "logs" / "Errors.log"
    # A graveyard-family line the single whitelist pattern does not match — would
    # fail by default, but the admin verify/redeploy path sets the advisory flag.
    errors.write_text(
        "GetClosestGraveyard: unable to find zoneId and areaId for map 1 coords (-26.6, 368.7, 98.4)\n"
    )

    strict = _run(stack, _stubs(tmp_path))
    assert "Errors.log has actionable runtime errors" in strict.stdout

    advisory = _run(stack, _stubs(tmp_path), VERIFY_ERRORS_LOG_ADVISORY="1")
    assert "Errors.log has actionable runtime errors" not in advisory.stdout
    assert "treated as advisory" in advisory.stdout
    _assert_complete_summary(advisory)
