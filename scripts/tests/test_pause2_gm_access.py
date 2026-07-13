"""Executable regression coverage for Pause 2 GM privilege verification."""

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


SCRIPTS_DIR = (
    Path("/src")
    if Path("/src/install-azerothcore.sh").is_file()
    else Path(__file__).resolve().parents[1]
)
INSTALLER = SCRIPTS_DIR / "install-azerothcore.sh"
GM_USERNAME = "GameMaster"
GM_UPPER = GM_USERNAME.upper()


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _installer_fixture(tmp_path: Path) -> Path:
    """Copy the installer and stop immediately after the Pause 2 checkpoint."""
    source = INSTALLER.read_text()
    source = source.replace(
        'STACK_DIR="/opt/stacks/azerothcore"',
        f'STACK_DIR="{tmp_path / "stack"}"',
        1,
    )
    root_guard = (
        'if [ "${EUID}" -eq 0 ]; then\n'
        '    echo "ERROR: Do not run this installer with sudo or as root." >&2\n'
        '    echo "Run it as your normal user; the script will ask for sudo when needed." >&2\n'
        '    exit 2\n'
        'fi\n\n'
    )
    assert root_guard in source
    source = source.replace(root_guard, "", 1)
    tty_check = '        if [ -w /dev/tty ]; then\n'
    assert tty_check in source
    source = source.replace(tty_check, '        if false; then\n', 1)
    acknowledgment = '        read -rp "When done, press Enter to continue..." _ignored\n'
    assert acknowledgment in source
    source = source.replace(acknowledgment, '        : # test fixture auto-acknowledges Pause 2\n', 1)
    checkpoint = '    mark_phase_complete "pause-2" "GM + AHBOT accounts created"\n'
    assert checkpoint in source
    source = source.replace(checkpoint, checkpoint + "    clean_exit 0\n", 1)
    fixture = tmp_path / "install-azerothcore.sh"
    _write_executable(fixture, source)
    return fixture


def _make_docker_stub(bindir: Path) -> None:
    _write_executable(
        bindir / "docker",
        """#!/bin/bash
if [ "$1" = inspect ]; then
    echo running
    exit 0
fi
if [ "$1" = exec ]; then
    query="${@: -1}"
    printf '%s\\n' "$query" >> "$DOCKER_SQL_LOG"
    case "$query" in
        "SELECT gmlevel, RealmID FROM acore_auth.account_access WHERE id = (SELECT id FROM acore_auth.account WHERE username = 'GAMEMASTER') AND gmlevel = 3 AND RealmID = -1;") printf '%b' "$TEST_GM_ACCESS_ROWS" ;;
        *"FROM acore_auth.account_access"*)
            echo "Unexpected GM access SQL: $query" >&2
            exit 64
            ;;
        *"FROM acore_auth.account"*) printf '%b' "$TEST_ACCOUNT_ROWS" ;;
    esac
    exit 0
fi
exit 0
""",
    )


def _prepare(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    installer = _installer_fixture(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    stack = tmp_path / "stack"
    stack.mkdir()
    stack.joinpath(".env").write_text("DOCKER_DB_ROOT_PASSWORD=db-root-secret\n")
    home.joinpath(".azerothcore-install-config").write_text(
        "\n".join(
            (
                "DB_ROOT_PASSWORD=db-root-secret",
                f"GM_USERNAME={GM_USERNAME}",
                "GM_PASSWORD=gm-plaintext-secret",
                "AHBOT_PASSWORD=ahbot-plaintext-secret",
                "PLAYERBOT_COUNT=1",
                "SERVER_XP_RATE=x5",
                "SERVER_PVP=y",
                "INNODB_BUFFER_POOL_SIZE=1G",
                "MAP_UPDATE_THREADS=1",
                "AHBOT_CHARACTER_COUNT=1",
                "INSTALL_UFW=n",
                "ENABLE_SYSTEMD=n",
            )
        )
        + "\n"
    )
    completed_before_pause2 = (
        "0.0", "0.1", "0.2", "0.3", "0.4", "0.5", "1", "2.1", "2.2",
        "2.3", "2.4", "2.5", "2.6", "3", "3.1", "4",
    )
    home.joinpath(".azerothcore-install-state").write_text(
        "".join(f"{phase}|seed|completed before Pause 2\n" for phase in completed_before_pause2)
    )
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _make_docker_stub(bindir)
    _write_executable(
        bindir / "sudo",
        "#!/bin/sh\n[ \"${1:-}\" = -n ] && exit 1\nexit 0\n",
    )
    return installer, home, bindir, stack


def _run_pause2(
    tmp_path: Path, *, account_rows: str, access_rows: str
) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    installer, home, bindir, _stack = _prepare(tmp_path)
    sql_log = tmp_path / "docker-sql.log"
    result = subprocess.run(
        ["bash", str(installer), "--resume-from=pause-2"],
        input="\n\n\n",
        capture_output=True,
        text=True,
        timeout=10,
        env={
            **os.environ,
            "HOME": str(home),
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "DOCKER_SQL_LOG": str(sql_log),
            "TEST_ACCOUNT_ROWS": account_rows,
            "TEST_GM_ACCESS_ROWS": access_rows,
        },
    )
    return result, home, sql_log


class Pause2GmAccessTest(unittest.TestCase):
    def test_rejects_non_global_level3_gm_access_without_checkpoint(self):
        cases = (
            ("missing-access", ""),
            ("wrong-security", "2\\t-1\\n"),
            ("wrong-realm", "3\\t1\\n"),
        )
        for name, access_rows in cases:
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp:
                result, home, sql_log = _run_pause2(
                    Path(tmp),
                    account_rows=f"{GM_UPPER}\\nAHBOT\\n",
                    access_rows=access_rows,
                )

                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn(
                    "GM account has no global security level 3 access", result.stdout
                )
                self.assertIn(
                    f"Retry command: account set gmlevel {GM_USERNAME} 3 -1",
                    result.stdout,
                )
                self.assertNotIn(
                    "pause-2|", home.joinpath(".azerothcore-install-state").read_text()
                )
                sql = sql_log.read_text()
                self.assertIn("acore_auth.account_access", sql)
                self.assertNotIn("gm-plaintext-secret", sql)
                self.assertNotIn("ahbot-plaintext-secret", sql)

    def test_accepts_global_level3_gm_access_without_password_revalidation(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, home, sql_log = _run_pause2(
                Path(tmp),
                account_rows=f"{GM_UPPER}\\nAHBOT\\n",
                access_rows="3\\t-1\\n",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("GM account has global security level 3 access", result.stdout)
            self.assertIn(
                "pause-2|", home.joinpath(".azerothcore-install-state").read_text()
            )
            sql = sql_log.read_text()
            self.assertIn("acore_auth.account_access", sql)
            self.assertNotIn("gm-plaintext-secret", sql)
            self.assertNotIn("ahbot-plaintext-secret", sql)


if __name__ == "__main__":
    unittest.main(verbosity=2)
