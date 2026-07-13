import os
import stat
import subprocess
from pathlib import Path


SCRIPTS = Path("/src") if Path("/src/uninstall-azerothcore.sh").is_file() else Path(__file__).resolve().parents[1]


def _exe(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def test_uninstall_preserves_stack_and_state_when_compose_down_fails(tmp_path):
    stack = tmp_path / "stack"; stack.mkdir()
    stack.joinpath("docker-compose.yml").write_text("services: {}\n")
    state = tmp_path / "state"; state.write_text("phase=4\n")
    config = tmp_path / "config"; config.write_text("secret\n")
    bind = tmp_path / "bin"; bind.mkdir()
    _exe(bind / "docker", "#!/bin/sh\n[ \"$1\" = compose ] && { [ \"$2\" = version ] && exit 0; exit 42; }; exit 0\n")
    _exe(bind / "sudo", "#!/bin/sh\n[ \"$1\" = -v ] && exit 0\nexec \"$@\"\n")
    systemd_unit = tmp_path / "azerothcore.service"
    script = tmp_path / "uninstall-azerothcore.sh"
    script.write_text(
        (SCRIPTS / "uninstall-azerothcore.sh").read_text().replace(
            'SYSTEMD_UNIT="/etc/systemd/system/azerothcore.service"',
            f'SYSTEMD_UNIT="{systemd_unit}"',
            1,
        )
    )
    if os.geteuid() == 0:
        for p in tmp_path.parents:
            if p in (Path("/"), Path("/tmp")): break
            p.chmod(0o755)
        for p in (tmp_path, stack, bind): p.chmod(0o777)
        for p in (state, config, stack / "docker-compose.yml"): p.chmod(0o666)
        preexec = lambda: (os.setgid(65534), os.setuid(65534))
    else:
        preexec = None
    result = subprocess.run(
        ["bash", str(script), "--yes"],
        env={**os.environ, "PATH": f"{bind}:{os.environ['PATH']}", "STACK_DIR": str(stack), "STATE_FILE": str(state), "CONFIG_FILE": str(config)},
        capture_output=True, text=True, preexec_fn=preexec,
    )
    assert result.returncode == 1
    assert "Uninstall incomplete" in result.stderr
    assert stack.exists() and state.exists() and config.exists()
