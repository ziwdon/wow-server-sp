import os
import stat
import subprocess
from pathlib import Path


SCRIPTS_DIR = Path("/src") if Path("/src/redeploy-azerothcore.sh").is_file() else Path(__file__).resolve().parents[1]
REDEPLOY_SH = SCRIPTS_DIR / "redeploy-azerothcore.sh"


def _executable(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _stack(tmp_path: Path, initialized: bool) -> Path:
    stack = tmp_path / "stack"
    (stack / "logs").mkdir(parents=True)
    stack.joinpath("docker-compose.yml").write_text("services: {}\n")
    stack.joinpath(".env").write_text("COMPOSE_FILE=docker-compose.yml\n")
    stack.joinpath("logs", "Server.log").write_text(
        "WORLD: World Initialized\n" if initialized else "starting\n"
    )
    return stack


def _stubs(tmp_path: Path) -> Path:
    bind = tmp_path / "bin"
    bind.mkdir(mode=0o777)
    bind.chmod(0o777)
    _executable(bind / "docker", """#!/bin/bash
if [ "$1" = compose ]; then
    case "$2" in
        config) echo ac-worldserver ;;
        ps) echo abc123 ;;
    esac
    exit 0
fi
if [ "$1" = inspect ]; then
    if [ "${REDEPLOY_TEST_STATUS:-running}" = crash ]; then
        if [ -f "$REDEPLOY_TEST_STATE" ]; then echo exited; else : > "$REDEPLOY_TEST_STATE"; echo running; fi
    else
        echo "${REDEPLOY_TEST_STATUS:-running}"
    fi
fi
""")
    _executable(bind / "sleep", "#!/bin/sh\nexit 0\n")
    return bind


def _run(stack: Path, bind: Path, *, status="running") -> subprocess.CompletedProcess[str]:
    if os.geteuid() == 0:
        for p in stack.parents:
            if p == Path("/") or p == Path("/tmp"):
                break
            p.chmod(0o755)
        for p in (stack, stack / "logs"):
            p.chmod(0o755)
        for p in stack.rglob("*"):
            if p.is_file():
                p.chmod(0o644)
        preexec_fn = lambda: (os.setgid(65534), os.setuid(65534))
    else:
        preexec_fn = None
    return subprocess.run(
        ["bash", str(REDEPLOY_SH)],
        env={
            **os.environ,
            "PATH": f"{bind}:{os.environ['PATH']}",
            "STACK_DIR": str(stack),
            "SKIP_BUILD": "1",
            "WORLD_INIT_TIMEOUT": "1",
            "REDEPLOY_TEST_STATUS": status,
            "REDEPLOY_TEST_STATE": str(bind / "inspect-state"),
        },
        capture_output=True,
        text=True,
        preexec_fn=preexec_fn,
    )


def test_redeploy_succeeds_only_after_current_boot_initialization(tmp_path):
    result = _run(_stack(tmp_path, initialized=True), _stubs(tmp_path))

    assert result.returncode == 0, result.stderr
    assert "World Initialized — worldserver is up." in result.stdout


def test_redeploy_fails_when_running_worldserver_never_initializes(tmp_path):
    result = _run(_stack(tmp_path, initialized=False), _stubs(tmp_path))

    assert result.returncode == 1
    assert "did not observe 'World Initialized'" in result.stderr
    assert "Redeploy FAILED" not in result.stderr


def test_redeploy_fails_when_worldserver_exits_while_waiting(tmp_path):
    result = _run(_stack(tmp_path, initialized=False), _stubs(tmp_path), status="crash")

    assert result.returncode == 1
    assert "entered state 'exited' before initialization completed" in result.stderr
