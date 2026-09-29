import os
import shlex
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


ADMIN_ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ADMIN_ROOT / "scripts/install-azerothcore-admin.sh"
ADMIN_COMPOSE = ADMIN_ROOT / "docker-compose.yml"


def _write_stub(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _installer_copy_through_admin_yml(tmp_path: Path) -> tuple[Path, Path]:
    """Create a test copy that stops after the admin.yml creation step."""
    ac_stack = tmp_path / "ac-stack"
    admin_stack = tmp_path / "admin-stack"
    script = tmp_path / "install-through-admin-yml.sh"

    source = INSTALLER.read_text()
    source = source.replace(
        'if [ "$EUID" -eq 0 ]; then\n'
        '    echo "ERROR: do not run as root; sudo is invoked internally where needed." >&2\n'
        "    exit 1\n"
        "fi\n\n",
        "",
    )
    source = source.replace(
        "STACK_DIR=/opt/stacks/azerothcore-admin",
        f"STACK_DIR={shlex.quote(str(admin_stack))}",
    )
    source = source.replace(
        "AC_STACK_DIR=/opt/stacks/azerothcore",
        f"AC_STACK_DIR={shlex.quote(str(ac_stack))}",
    )
    source = source.replace(
        "# --- Step 4b: backups dir",
        "exit 0\n\n# --- Step 4b: backups dir",
    )
    script.write_text(source)
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script, ac_stack


def _systemd_prompt_script(tmp_path: Path) -> Path:
    """Create a test copy that only runs the optional systemd prompt block."""
    marker = "# --- Step 9: optional systemd unit ---\n"
    source = INSTALLER.read_text()
    _, systemd_prompt_block = source.split(marker, maxsplit=1)

    script = tmp_path / "install-systemd-prompt.sh"
    script.write_text(f"#!/bin/bash\nset -euo pipefail\n{marker}{systemd_prompt_block}")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def _lan_ip_step_run(
    tmp_path: Path,
    *,
    lan_ip: str | None,
    existing_env: str | None = None,
    nonlocal_bind: str = "1",
) -> subprocess.CompletedProcess[str]:
    """Run a test copy of the installer that stops after the LAN_IP step."""
    admin_stack = tmp_path / "admin-stack"
    ac_stack = tmp_path / "ac-stack"
    admin_stack.mkdir()
    ac_stack.mkdir()
    if existing_env is not None:
        (admin_stack / ".env").write_text(existing_env)

    source = INSTALLER.read_text()
    source = source.replace('if [ "$EUID" -eq 0 ]; then', "if false; then", 1)
    source = source.replace(
        "STACK_DIR=/opt/stacks/azerothcore-admin",
        f"STACK_DIR={shlex.quote(str(admin_stack))}",
    )
    source = source.replace(
        "AC_STACK_DIR=/opt/stacks/azerothcore",
        f"AC_STACK_DIR={shlex.quote(str(ac_stack))}",
    )
    marker = "# --- Step 2: port selection"
    assert marker in source
    source = source.replace(marker, f"exit 0\n\n{marker}")
    script = tmp_path / "install-through-lan-ip.sh"
    script.write_text(source)
    script.chmod(script.stat().st_mode | stat.S_IXUSR)

    stubs = tmp_path / "stubs"
    stubs.mkdir()
    _write_stub(stubs / "tailscale", "#!/bin/sh\nprintf '100.64.0.1\\n'\n")
    _write_stub(
        stubs / "ip",
        "#!/bin/sh\n"
        "printf '1: lo    inet 127.0.0.1/8 scope host lo\\n'\n"
        "printf '2: enp2s0    inet 192.168.0.11/24 brd 192.168.0.255 scope global enp2s0\\n'\n",
    )
    _write_stub(stubs / "sysctl", "#!/bin/sh\necho \"${FAKE_NONLOCAL_BIND:-1}\"\n")
    env = os.environ.copy()
    env.pop("LAN_IP", None)
    env["FAKE_NONLOCAL_BIND"] = nonlocal_bind
    env["PATH"] = f"{stubs}:{env['PATH']}"
    if lan_ip is not None:
        env["LAN_IP"] = lan_ip
    return subprocess.run(
        [str(script)], text=True, capture_output=True, env=env, check=False
    )


def _installer_stubs(tmp_path: Path) -> Path:
    """Provide the external commands needed before the script's early exit."""
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    _write_stub(stubs / "tailscale", "#!/bin/sh\nprintf '100.64.0.1\\n'\n")
    _write_stub(stubs / "ss", "#!/bin/sh\nexit 1\n")
    _write_stub(
        stubs / "sudo",
        "#!/bin/sh\n"
        "if [ \"${FAIL_ENV_REWRITE:-0}\" = 1 ] && [ \"${1:-}\" = mv ]; then\n"
        "    exit 42\n"
        "fi\n"
        "exec \"$@\"\n",
    )
    return stubs


def _run_through_admin_yml(
    script: Path, stubs: Path, *, fail_env_rewrite: bool = False
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PATH"] = f"{stubs}:{env['PATH']}"
    if fail_env_rewrite:
        env["FAIL_ENV_REWRITE"] = "1"
    return subprocess.run(
        [str(script)], text=True, capture_output=True, env=env, check=False
    )


class InstallerScriptTest(unittest.TestCase):
    def test_compose_file_append_preserves_env_metadata_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            script, ac_stack = _installer_copy_through_admin_yml(tmp_path)
            ac_stack.mkdir()
            env_file = ac_stack / ".env"
            env_file.write_text(
                "MYSQL_PASSWORD=secret\n"
                "COMPOSE_FILE=docker-compose.yml:docker-compose.override.yml\n"
                "UNRELATED=value\n"
            )
            env_file.chmod(0o600)
            if os.geteuid() == 0:
                os.chown(env_file, 1234, 2345)
            before = env_file.stat()

            stubs = _installer_stubs(tmp_path)
            first = _run_through_admin_yml(script, stubs)

            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(
                env_file.read_text(),
                "MYSQL_PASSWORD=secret\n"
                "COMPOSE_FILE=docker-compose.yml:docker-compose.override.yml:docker-compose.admin.yml\n"
                "UNRELATED=value\n",
            )
            after = env_file.stat()
            self.assertEqual(after.st_uid, before.st_uid)
            self.assertEqual(after.st_gid, before.st_gid)
            self.assertEqual(stat.S_IMODE(after.st_mode), stat.S_IMODE(before.st_mode))

            second = _run_through_admin_yml(script, stubs)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(env_file.read_text().count("docker-compose.admin.yml"), 1)

    def test_failed_compose_file_rewrite_leaves_env_intact_and_cleans_temp(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            script, ac_stack = _installer_copy_through_admin_yml(tmp_path)
            ac_stack.mkdir()
            env_file = ac_stack / ".env"
            original = "COMPOSE_FILE=docker-compose.yml:docker-compose.override.yml\n"
            env_file.write_text(original)
            env_file.chmod(0o600)
            before = env_file.stat()

            result = _run_through_admin_yml(
                script, _installer_stubs(tmp_path), fail_env_rewrite=True
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(env_file.read_text(), original)
            after = env_file.stat()
            self.assertEqual(after.st_uid, before.st_uid)
            self.assertEqual(after.st_gid, before.st_gid)
            self.assertEqual(stat.S_IMODE(after.st_mode), stat.S_IMODE(before.st_mode))
            self.assertEqual(list(ac_stack.glob(".env.tmp*")), [])

    def test_installer_refuses_admin_yml_directory_without_removing_it(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            script, ac_stack = _installer_copy_through_admin_yml(tmp_path)
            ac_stack.mkdir()
            (ac_stack / ".env").write_text("")
            (ac_stack / "docker-compose.admin.yml").mkdir()

            stubs = tmp_path / "stubs"
            stubs.mkdir()
            _write_stub(stubs / "tailscale", "#!/bin/sh\nprintf '100.64.0.1\\n'\n")
            _write_stub(stubs / "ss", "#!/bin/sh\nexit 1\n")
            _write_stub(stubs / "sudo", "#!/bin/sh\nexec \"$@\"\n")

            env = os.environ.copy()
            env["PATH"] = f"{stubs}:{env['PATH']}"
            result = subprocess.run(
                [str(script)],
                text=True,
                capture_output=True,
                env=env,
                check=False,
            )

            self.assertEqual(result.returncode, 1)
            admin_yml = ac_stack / "docker-compose.admin.yml"
            self.assertTrue(admin_yml.is_dir())
            self.assertIn("exists as a directory", result.stderr)
            self.assertIn(str(admin_yml), result.stderr)

    def test_lan_ip_unset_keeps_tailscale_only(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            result = _lan_ip_step_run(Path(temp_dir), lan_ip=None)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("LAN IP:", result.stdout)

    def test_lan_ip_assigned_to_local_interface_is_accepted(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            result = _lan_ip_step_run(Path(temp_dir), lan_ip="192.168.0.11")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("LAN IP: 192.168.0.11", result.stdout)

    def test_lan_ip_warns_when_nonlocal_bind_is_off(self):
        for value, warns in (("0", True), ("1", False)):
            with self.subTest(nonlocal_bind=value), tempfile.TemporaryDirectory() as temp_dir:
                result = _lan_ip_step_run(
                    Path(temp_dir), lan_ip="192.168.0.11", nonlocal_bind=value
                )

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual("ip_nonlocal_bind is not 1" in result.stderr, warns)

    def test_lan_ip_rejects_unassigned_and_tailscale_addresses(self):
        cases = {
            "10.9.9.9": "not assigned to any local interface",
            "192.168.0.1": "not assigned to any local interface",
            "0.0.0.0": "not assigned to any local interface",
            "100.64.0.1": "must differ from the Tailscale IP",
        }
        for lan_ip, message in cases.items():
            with self.subTest(lan_ip=lan_ip), tempfile.TemporaryDirectory() as temp_dir:
                result = _lan_ip_step_run(Path(temp_dir), lan_ip=lan_ip)

                self.assertEqual(result.returncode, 1)
                self.assertIn(message, result.stderr)

    def test_lan_ip_is_preserved_from_existing_env_unless_overridden(self):
        existing = "TAILSCALE_IP=100.64.0.1\nLAN_IP=192.168.0.11\nADMIN_PORT=8765\n"
        with tempfile.TemporaryDirectory() as temp_dir:
            kept = _lan_ip_step_run(Path(temp_dir), lan_ip=None, existing_env=existing)
            self.assertEqual(kept.returncode, 0, kept.stderr)
            self.assertIn("LAN IP: 192.168.0.11", kept.stdout)

        with tempfile.TemporaryDirectory() as temp_dir:
            disabled = _lan_ip_step_run(Path(temp_dir), lan_ip="", existing_env=existing)
            self.assertEqual(disabled.returncode, 0, disabled.stderr)
            self.assertNotIn("LAN IP:", disabled.stdout)

    def test_installer_writes_lan_ip_to_env(self):
        source = INSTALLER.read_text()
        env_block = source.split('cat > "$STACK_DIR/.env" <<EOF\n', 1)[1].split("\nEOF\n", 1)[0]

        self.assertIn("LAN_IP=$LAN_IP", env_block.splitlines())

    def test_compose_lan_bind_falls_back_to_loopback_never_all_interfaces(self):
        compose = ADMIN_COMPOSE.read_text()

        self.assertIn('- "${TAILSCALE_IP}:${ADMIN_PORT:-8765}:8000"', compose)
        self.assertIn('- "${LAN_IP:-127.0.0.1}:${ADMIN_PORT:-8765}:8000"', compose)
        self.assertNotIn("${LAN_IP}:", compose)
        self.assertNotIn("${LAN_IP-", compose)

    def test_systemd_unit_waits_for_lan_ip_without_systemd_brace_expansion(self):
        source = INSTALLER.read_text()
        unit = source.split("<<'UNIT' >/dev/null\n", 1)[1].split("\nUNIT\n", 1)[0]
        exec_start_pre = [line for line in unit.splitlines() if line.startswith("ExecStartPre=")]

        self.assertEqual(len(exec_start_pre), 2)
        self.assertIn('Waiting for LAN IP $LAN_IP', exec_start_pre[1])
        self.assertIn('[ -z "$LAN_IP" ] && exit 0', exec_start_pre[1])
        self.assertTrue(exec_start_pre[1].endswith("exit 0'"), exec_start_pre[1])
        # systemd substitutes ${VAR} inside words; the bash snippets must not
        # rely on brace expansion of their own variables.
        for line in exec_start_pre:
            self.assertNotIn("${", line)

    def test_systemd_lan_wait_never_blocks_start_when_lan_ip_missing(self):
        source = INSTALLER.read_text()
        unit = source.split("<<'UNIT' >/dev/null\n", 1)[1].split("\nUNIT\n", 1)[0]
        lan_wait = [line for line in unit.splitlines() if "LAN IP" in line][0]
        snippet = shlex.split(lan_wait.removeprefix("ExecStartPre="))[2]

        with tempfile.TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            env_file = tmp_path / "admin.env"
            env_file.write_text("TAILSCALE_IP=100.64.0.1\nLAN_IP=192.168.0.11\n")
            snippet = snippet.replace("/opt/stacks/azerothcore-admin/.env", str(env_file))
            stubs = tmp_path / "stubs"
            stubs.mkdir()
            _write_stub(stubs / "ip", "#!/bin/sh\nprintf '1: lo    inet 127.0.0.1/8 scope host lo\\n'\n")
            _write_stub(stubs / "sleep", "#!/bin/sh\nexit 0\n")
            env = os.environ.copy()
            env["PATH"] = f"{stubs}:{env['PATH']}"

            result = subprocess.run(
                ["bash", "-c", snippet], text=True, capture_output=True, env=env, check=False
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("WARNING: LAN IP 192.168.0.11 not assigned; starting anyway", result.stdout)

    def test_admin_yml_bind_mount_disables_implicit_host_path_creation(self):
        compose = ADMIN_COMPOSE.read_text()

        self.assertIn(
            "source: /opt/stacks/azerothcore/docker-compose.admin.yml",
            compose,
        )
        self.assertIn("target: /ac/docker-compose.admin.yml", compose)
        self.assertIn("create_host_path: false", compose)
        self.assertNotIn(
            "- /opt/stacks/azerothcore/docker-compose.admin.yml:/ac/docker-compose.admin.yml:rw",
            compose,
        )

    def test_systemd_unit_prompt_defaults_to_yes(self):
        source = INSTALLER.read_text()

        self.assertIn(
            "Install azerothcore-admin.service systemd unit (auto-start at boot)? [Y/n] ",
            source,
        )
        self.assertNotIn("[y/N]", source)

        with tempfile.TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            script = _systemd_prompt_script(tmp_path)

            stubs = tmp_path / "stubs"
            stubs.mkdir()
            _write_stub(
                stubs / "sudo",
                "#!/bin/sh\n"
                "printf '%s\\n' \"$*\" >> \"$SUDO_LOG\"\n"
                "if [ \"${1:-}\" = \"tee\" ]; then\n"
                "    cat >/dev/null\n"
                "fi\n",
            )

            cases = {
                "": True,
                "y": True,
                "Y": True,
                "n": False,
                "N": False,
                "later": False,
            }
            for answer, should_install in cases.items():
                with self.subTest(answer=answer):
                    sudo_log = tmp_path / f"sudo-{answer or 'enter'}.log"
                    env = os.environ.copy()
                    env["PATH"] = f"{stubs}:{env['PATH']}"
                    env["SUDO_LOG"] = str(sudo_log)

                    result = subprocess.run(
                        [str(script)],
                        input=f"{answer}\n",
                        text=True,
                        capture_output=True,
                        env=env,
                        check=False,
                    )

                    self.assertEqual(result.returncode, 0, result.stderr)
                    sudo_calls = sudo_log.read_text() if sudo_log.exists() else ""
                    self.assertEqual(
                        "systemctl enable --now azerothcore-admin.service"
                        in sudo_calls,
                        should_install,
                    )


if __name__ == "__main__":
    unittest.main()
