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


class InstallerScriptTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
