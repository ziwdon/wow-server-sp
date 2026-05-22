import os
import shlex
import stat
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
UNINSTALLER = REPO_ROOT / "scripts/uninstall-azerothcore-admin.sh"


def _write_stub(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def test_uninstaller_removes_only_admin_compose_file_from_ac_env(tmp_path):
    ac_stack = tmp_path / "ac-stack"
    admin_stack = tmp_path / "admin-stack"
    ac_stack.mkdir()
    admin_stack.mkdir()
    env_file = ac_stack / ".env"
    env_file.write_text(
        "COMPOSE_FILE=docker-compose.yml:docker-compose.override.yml:docker-compose.admin.yml\n"
        "OTHER=value\n"
    )

    script = tmp_path / "uninstall-admin.sh"
    source = UNINSTALLER.read_text()
    source = source.replace(
        "STACK_DIR=/opt/stacks/azerothcore-admin",
        f"STACK_DIR={shlex.quote(str(admin_stack))}",
    )
    source = source.replace(
        "AC_STACK_DIR=/opt/stacks/azerothcore",
        f"AC_STACK_DIR={shlex.quote(str(ac_stack))}",
    )
    script.write_text(source)
    script.chmod(script.stat().st_mode | stat.S_IXUSR)

    stubs = tmp_path / "stubs"
    stubs.mkdir()
    _write_stub(stubs / "docker", "#!/bin/sh\nexit 0\n")
    _write_stub(stubs / "sudo", "#!/bin/sh\nexec \"$@\"\n")

    env = os.environ.copy()
    env["PATH"] = f"{stubs}:{env['PATH']}"
    result = subprocess.run(
        [str(script), "--yes"],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert env_file.read_text() == (
        "COMPOSE_FILE=docker-compose.yml:docker-compose.override.yml\n"
        "OTHER=value\n"
    )
