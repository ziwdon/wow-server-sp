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


def test_uninstaller_uses_same_dir_temp_file_for_ac_env_rewrite(tmp_path):
    ac_stack = tmp_path / "ac-stack"
    admin_stack = tmp_path / "admin-stack"
    ac_stack.mkdir()
    admin_stack.mkdir()
    env_file = ac_stack / ".env"
    env_file.write_text(
        "COMPOSE_FILE=docker-compose.yml:docker-compose.override.yml:docker-compose.admin.yml\n"
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
    sudo_log = tmp_path / "sudo.log"
    _write_stub(stubs / "docker", "#!/bin/sh\nexit 0\n")
    _write_stub(
        stubs / "sudo",
        "#!/bin/sh\n"
        "printf '%s\\n' \"$*\" >> \"$SUDO_LOG\"\n"
        "exec \"$@\"\n",
    )

    env = os.environ.copy()
    env["PATH"] = f"{stubs}:{env['PATH']}"
    env["SUDO_LOG"] = str(sudo_log)
    result = subprocess.run(
        [str(script), "--yes"],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert f"mktemp {ac_stack}/.env.tmp." in sudo_log.read_text()
    assert list(ac_stack.glob(".env.tmp.*")) == []


def test_uninstaller_removes_admin_compose_file_but_not_directory(tmp_path):
    ac_stack = tmp_path / "ac-stack"
    admin_stack = tmp_path / "admin-stack"
    ac_stack.mkdir()
    admin_stack.mkdir()
    (ac_stack / ".env").write_text("COMPOSE_FILE=docker-compose.yml\n")
    admin_yml = ac_stack / "docker-compose.admin.yml"
    admin_yml.write_text("services:\n  ac-worldserver:\n    environment:\n      AC_FOO: '1'\n")

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
    assert not admin_yml.exists()

    admin_yml.mkdir()
    result = subprocess.run(
        [str(script), "--yes"],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert admin_yml.is_dir()


def test_uninstaller_disables_and_removes_systemd_unit(tmp_path):
    ac_stack = tmp_path / "ac-stack"
    admin_stack = tmp_path / "admin-stack"
    systemd_unit = tmp_path / "azerothcore-admin.service"
    ac_stack.mkdir()
    admin_stack.mkdir()
    (ac_stack / ".env").write_text("COMPOSE_FILE=docker-compose.yml\n")
    systemd_unit.write_text("[Service]\nExecStart=/usr/bin/docker compose up -d\n")

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
    source = source.replace(
        "SYSTEMD_UNIT=/etc/systemd/system/$SYSTEMD_SERVICE",
        f"SYSTEMD_UNIT={shlex.quote(str(systemd_unit))}",
    )
    script.write_text(source)
    script.chmod(script.stat().st_mode | stat.S_IXUSR)

    stubs = tmp_path / "stubs"
    stubs.mkdir()
    systemctl_log = tmp_path / "systemctl.log"
    _write_stub(stubs / "docker", "#!/bin/sh\nexit 0\n")
    _write_stub(stubs / "sudo", "#!/bin/sh\nexec \"$@\"\n")
    _write_stub(
        stubs / "systemctl",
        "#!/bin/sh\n"
        "printf '%s\\n' \"$*\" >> \"$SYSTEMCTL_LOG\"\n"
        "exit 0\n",
    )

    env = os.environ.copy()
    env["PATH"] = f"{stubs}:{env['PATH']}"
    env["SYSTEMCTL_LOG"] = str(systemctl_log)
    result = subprocess.run(
        [str(script), "--yes"],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert not systemd_unit.exists()
    assert systemctl_log.read_text().splitlines() == [
        "disable --now azerothcore-admin.service",
        "daemon-reload",
    ]
