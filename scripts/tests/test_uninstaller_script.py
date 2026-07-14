import os
import re
import stat
import subprocess
from pathlib import Path


SCRIPTS = Path("/src") if Path("/src/uninstall-azerothcore.sh").is_file() else Path(__file__).resolve().parents[1]


def _exe(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _isolated_script(tmp_path: Path, *, stack: Path, state: Path, config: Path, unit: Path) -> Path:
    replacements = {
        r'^(?:readonly )?STACK_DIR=.*$': f'readonly STACK_DIR="{stack}"',
        r'^(?:readonly )?STATE_FILE=.*$': f'readonly STATE_FILE="{state}"',
        r'^(?:readonly )?CONFIG_FILE=.*$': f'readonly CONFIG_FILE="{config}"',
        r'^(?:readonly )?SYSTEMD_UNIT=.*$': f'readonly SYSTEMD_UNIT="{unit}"',
    }
    source = (SCRIPTS / "uninstall-azerothcore.sh").read_text()
    for pattern, replacement in replacements.items():
        source, count = re.subn(pattern, replacement, source, count=1, flags=re.MULTILINE)
        assert count == 1, pattern
    script = tmp_path / "uninstall-azerothcore.sh"
    script.write_text(source)
    return script


def _dangerous_stubs(tmp_path: Path, *, fail_systemctl: bool = False) -> Path:
    bind = tmp_path / "bin"
    bind.mkdir()
    _exe(bind / "docker", """#!/bin/sh
echo "$@" >> "$TEST_ROOT/docker.calls"
[ "$1" = info ] && exit 0
if [ "$1" = compose ] && [ "$2" = version ]; then exit 0; fi
if [ "$1" = compose ]; then exit 42; fi
exit 0
""")
    _exe(bind / "systemctl", f"#!/bin/sh\necho \"$@\" >> \"$TEST_ROOT/systemctl.calls\"\nexit {42 if fail_systemctl else 0}\n")
    _exe(bind / "rm", """#!/bin/sh
echo "$@" >> "$TEST_ROOT/rm.calls"
for arg in "$@"; do
  case "$arg" in -*) continue ;; "$TEST_ROOT"/*) ;; *) echo "unsafe rm: $arg" >&2; exit 97 ;; esac
done
exec /bin/rm "$@"
""")
    _exe(bind / "crontab", "#!/bin/sh\necho \"$@\" >> \"$TEST_ROOT/crontab.calls\"\nexit 1\n")
    _exe(bind / "sudo", """#!/bin/sh
[ "$1" = -v ] && exit 0
case "$1" in systemctl|rm) exec "$@" ;; *) exit 98 ;; esac
""")
    return bind


def _run_isolated(script: Path, bind: Path, tmp_path: Path):
    for path in (tmp_path, bind):
        path.chmod(0o777)
    if os.geteuid() == 0:
        for parent in tmp_path.parents:
            if parent in (Path("/"), Path("/tmp")):
                break
            parent.chmod(parent.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        preexec = lambda: (os.setgid(65534), os.setuid(65534))
    else:
        preexec = None
    return subprocess.run(
        ["bash", str(script), "--yes"],
        env={
            "HOME": str(tmp_path),
            "PATH": f"{bind}:/usr/bin:/bin",
            "TEST_ROOT": str(tmp_path),
        },
        capture_output=True,
        text=True,
        preexec_fn=preexec,
    )


def test_uninstall_preserves_stack_and_state_when_compose_down_fails(tmp_path):
    stack = tmp_path / "stack"
    stack.mkdir()
    (stack / "docker-compose.yml").write_text("services: {}\n")
    state = tmp_path / "state"
    state.write_text("phase=4\n")
    config = tmp_path / "config"
    config.write_text("secret\n")
    unit = tmp_path / "azerothcore.service"
    unit.write_text("[Unit]\n")
    bind = _dangerous_stubs(tmp_path)
    script = _isolated_script(
        tmp_path, stack=stack, state=state, config=config, unit=unit,
    )

    result = _run_isolated(script, bind, tmp_path)

    assert result.returncode == 1
    assert unit.exists()
    assert stack.exists() and state.exists() and config.exists()
    assert "unit file was preserved" in result.stderr


def test_uninstall_aborts_before_docker_when_systemd_disable_fails(tmp_path):
    stack = tmp_path / "stack"
    stack.mkdir()
    (stack / "docker-compose.yml").write_text("services: {}\n")
    state = tmp_path / "state"
    state.write_text("phase=4\n")
    config = tmp_path / "config"
    config.write_text("secret\n")
    unit = tmp_path / "azerothcore.service"
    unit.write_text("[Unit]\n")
    bind = _dangerous_stubs(tmp_path, fail_systemctl=True)
    script = _isolated_script(
        tmp_path, stack=stack, state=state, config=config, unit=unit,
    )

    result = _run_isolated(script, bind, tmp_path)

    assert result.returncode == 1
    assert unit.exists() and stack.exists() and state.exists() and config.exists()
    docker_calls = (tmp_path / "docker.calls").read_text()
    assert "info" in docker_calls
    assert "compose" not in docker_calls
    assert " rm " not in docker_calls


def test_no_stack_dir_env_override_seam():
    assert 'STACK_DIR="${STACK_DIR:-' not in (SCRIPTS / "uninstall-azerothcore.sh").read_text()


def test_no_unconstrained_stack_removal_bypass():
    assert 'run sudo rm -rf "$STACK_DIR"' not in (SCRIPTS / "uninstall-azerothcore.sh").read_text()
