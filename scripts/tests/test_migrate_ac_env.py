"""migrate-ac-env.sh: AC_AI_PLAYERBOT_* -> AC_PLAYERBOTS_* plus admin.yml pins."""

import os
import subprocess
from pathlib import Path

import pytest

SCRIPTS_DIR = Path("/src") if Path("/src/migrate-ac-env.sh").is_file() else Path(__file__).resolve().parents[1]
MIGRATE = SCRIPTS_DIR / "migrate-ac-env.sh"
ENV_VAR_PY = Path(os.environ.get(
    "ENV_VAR_PY", SCRIPTS_DIR.parent / "wow-server-sp-admin" / "app" / "services" / "env_var.py"))

OVERRIDE = """\
services:
  ac-worldserver:
    environment:
      # ----- mod-playerbots -----
      AC_PLAYERBOTS_DATABASE_INFO: "ac-database;3306;root;pw;acore_playerbots"
      AC_AI_PLAYERBOT_ENABLED: "1"
      AC_AI_PLAYERBOT_MIN_RANDOM_BOTS: "1000"
"""
ADMIN = """\
services:
  ac-worldserver:
    environment:
      AC_AI_PLAYERBOT_DISABLED_WITHOUT_REAL_PLAYER: '0'
      AC_AI_PLAYERBOT_RPG_STATUS_PROB_WEIGHT_TRAVEL_FLIGHT: '0'
      AC_RATE_XP_PET: '5'
"""
DIST = """\
Playerbots.Enabled = 1
Playerbots.MinRandomBots = 500
Playerbots.DisabledWithoutRealPlayer = 0
Playerbots.RpgStatusProbWeight.TravelFlight = 15
DungeonFinder.OptionsMask = 7
Rate.XP.Pet = 1
"""


def _stack(tmp_path: Path, admin: str | None = ADMIN) -> Path:
    stack = tmp_path / "stack"
    stack.mkdir()
    (stack / "docker-compose.override.yml").write_text(OVERRIDE)
    if admin is not None:
        (stack / "docker-compose.admin.yml").write_text(admin)
    (tmp_path / "new.conf.dist").write_text(DIST)
    return stack


ROOT_GUARD = (
    'if [ "${EUID:-$(id -u)}" -eq 0 ]; then\n'
    '    echo "ERROR: do not run as root; the overlays must stay owned by your user." >&2\n'
    '    exit 1\n'
    'fi\n'
)


def _script(tmp_path: Path) -> Path:
    """migrate-ac-env.sh without its root guard (the sandbox runs tests as root)."""
    text = MIGRATE.read_text()
    assert ROOT_GUARD in text
    script = tmp_path / "migrate-ac-env.sh"
    if not script.exists():
        script.write_text(text.replace(ROOT_GUARD, "", 1))
    return script


def _run(tmp_path: Path, stack: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(_script(tmp_path)), "--stack-dir", str(stack), *args],
        env={**os.environ, "ENV_VAR_PY": str(ENV_VAR_PY)},
        capture_output=True, text=True, timeout=30,
    )


def test_renames_every_legacy_var_in_place_and_keeps_comments(tmp_path):
    stack = _stack(tmp_path)
    admin = stack / "docker-compose.admin.yml"
    inode = admin.stat().st_ino
    r = _run(tmp_path, stack, "--backup-dir", str(tmp_path / "bk"),
             "--validate-against", str(tmp_path / "new.conf.dist"))
    assert r.returncode == 0, r.stderr
    override = (stack / "docker-compose.override.yml").read_text()
    assert "AC_AI_PLAYERBOT_" not in override + admin.read_text()
    assert '      AC_PLAYERBOTS_ENABLED: "1"' in override
    assert "# ----- mod-playerbots -----" in override
    assert "AC_PLAYERBOTS_RPG_STATUS_PROB_WEIGHT_TRAVEL_FLIGHT: '0'" in admin.read_text()
    assert admin.stat().st_ino == inode, "admin.yml must be rewritten in place (bind-mount inode)"
    assert (tmp_path / "bk" / "docker-compose.admin.yml.pre-migrate").read_text() == ADMIN


def test_second_run_is_a_no_op(tmp_path):
    stack = _stack(tmp_path)
    assert _run(tmp_path, stack, "--backup-dir", str(tmp_path / "a")).returncode == 0
    before = (stack / "docker-compose.admin.yml").read_text()
    r = _run(tmp_path, stack, "--backup-dir", str(tmp_path / "b"))
    assert r.returncode == 0
    assert "0 renamed" in r.stdout
    assert (stack / "docker-compose.admin.yml").read_text() == before


def test_dry_run_prints_a_diff_and_writes_nothing(tmp_path):
    stack = _stack(tmp_path)
    r = _run(tmp_path, stack, "--dry-run")
    assert r.returncode == 0, r.stderr
    assert "+      AC_PLAYERBOTS_ENABLED" in r.stdout
    assert (stack / "docker-compose.override.yml").read_text() == OVERRIDE
    assert not (tmp_path / "bk").exists()


def test_refuses_when_old_and_new_names_are_both_set(tmp_path):
    stack = _stack(tmp_path, admin=ADMIN + "      AC_PLAYERBOTS_DISABLED_WITHOUT_REAL_PLAYER: '1'\n")
    r = _run(tmp_path, stack, "--backup-dir", str(tmp_path / "bk"))
    assert r.returncode == 1
    assert "both AC_AI_PLAYERBOT_DISABLED_WITHOUT_REAL_PLAYER and AC_PLAYERBOTS_DISABLED_WITHOUT_REAL_PLAYER" in r.stderr
    assert "AC_AI_PLAYERBOT_" in (stack / "docker-compose.override.yml").read_text(), "nothing written on refusal"


def test_pins_are_appended_once_and_conflicts_refused(tmp_path):
    stack = _stack(tmp_path)
    pin = ["--pin", "AC_DUNGEON_FINDER_OPTIONS_MASK=5"]
    assert _run(tmp_path, stack, "--backup-dir", str(tmp_path / "a"), *pin).returncode == 0
    admin = (stack / "docker-compose.admin.yml").read_text()
    assert admin.count("AC_DUNGEON_FINDER_OPTIONS_MASK: '5'") == 1
    assert _run(tmp_path, stack, "--backup-dir", str(tmp_path / "b"), *pin).returncode == 0
    assert (stack / "docker-compose.admin.yml").read_text() == admin
    r = _run(tmp_path, stack, "--backup-dir", str(tmp_path / "c"), "--pin", "AC_DUNGEON_FINDER_OPTIONS_MASK=7")
    assert r.returncode == 1
    assert "refusing to replace" in r.stderr


def test_pin_creates_environment_block_for_an_empty_overlay(tmp_path):
    stack = _stack(tmp_path, admin="services: {}\n")
    r = _run(tmp_path, stack, "--backup-dir", str(tmp_path / "bk"), "--pin", "AC_DUNGEON_FINDER_OPTIONS_MASK=5")
    assert r.returncode == 0, r.stderr
    assert (stack / "docker-compose.admin.yml").read_text() == (
        "services:\n  ac-worldserver:\n    environment:\n      AC_DUNGEON_FINDER_OPTIONS_MASK: '5'\n"
    )


def test_validation_rejects_names_with_no_new_config_key(tmp_path):
    stack = _stack(tmp_path)
    r = _run(tmp_path, stack, "--dry-run", "--pin", "AC_NOT_A_KEY=1",
             "--validate-against", str(tmp_path / "new.conf.dist"))
    assert r.returncode == 1
    assert "AC_NOT_A_KEY" in r.stderr
    assert "AC_PLAYERBOTS_ENABLED" not in r.stderr


def test_rejects_unsafe_pin_values(tmp_path):
    stack = _stack(tmp_path)
    r = _run(tmp_path, stack, "--dry-run", "--pin", "AC_RATE_XP_PET=5'; rm -rf /")
    assert r.returncode == 1
    assert "invalid --pin" in r.stderr


def test_refuses_to_run_as_root(tmp_path):
    stack = _stack(tmp_path)
    r = subprocess.run(["bash", str(MIGRATE), "--stack-dir", str(stack), "--dry-run"],
                       env={**os.environ, "ENV_VAR_PY": str(ENV_VAR_PY)}, capture_output=True, text=True, timeout=30)
    if os.geteuid() == 0:
        assert r.returncode == 1
        assert "do not run as root" in r.stderr
    else:
        assert "do not run as root" not in r.stderr, r.stderr


def test_pin_fills_an_empty_environment_mapping(tmp_path):
    # yaml.safe_dump (the admin's writer) leaves `environment: {}` once every key is removed.
    stack = _stack(tmp_path, admin="services:\n  ac-worldserver:\n    environment: {}\n")
    r = _run(tmp_path, stack, "--backup-dir", str(tmp_path / "bk"), "--pin", "AC_DUNGEON_FINDER_OPTIONS_MASK=5")
    assert r.returncode == 0, r.stderr
    assert (stack / "docker-compose.admin.yml").read_text() == (
        "services:\n  ac-worldserver:\n    environment:\n      AC_DUNGEON_FINDER_OPTIONS_MASK: '5'\n"
    )


@pytest.mark.parametrize("line", [
    "      - AC_AI_PLAYERBOT_ENABLED=1\n",
    '      "AC_AI_PLAYERBOT_MAX_RANDOM_BOTS": "50"\n',
])
def test_refuses_a_legacy_name_the_rename_cannot_reach(tmp_path, line):
    stack = _stack(tmp_path)
    override = OVERRIDE + line
    (stack / "docker-compose.override.yml").write_text(override)
    r = _run(tmp_path, stack, "--backup-dir", str(tmp_path / "bk"))
    assert r.returncode == 1
    assert "AC_AI_PLAYERBOT_" in r.stderr and "by hand" in r.stderr
    assert (stack / "docker-compose.override.yml").read_text() == override, "nothing written"
    assert (stack / "docker-compose.admin.yml").read_text() == ADMIN, "nothing written"


def test_a_commented_legacy_name_is_not_a_leftover(tmp_path):
    stack = _stack(tmp_path)
    (stack / "docker-compose.override.yml").write_text(OVERRIDE + "      # AC_AI_PLAYERBOT_ENABLED was renamed\n")
    r = _run(tmp_path, stack, "--backup-dir", str(tmp_path / "bk"))
    assert r.returncode == 0, r.stderr


def test_a_rerun_never_overwrites_the_first_backup(tmp_path):
    # After a partial or complete first run the overlays hold migrated content;
    # the originals in --backup-dir must survive any re-run.
    stack = _stack(tmp_path)
    assert _run(tmp_path, stack, "--backup-dir", str(tmp_path / "bk")).returncode == 0
    r = _run(tmp_path, stack, "--backup-dir", str(tmp_path / "bk"))
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "bk" / "docker-compose.override.yml.pre-migrate").read_text() == OVERRIDE
    assert (tmp_path / "bk" / "docker-compose.admin.yml.pre-migrate").read_text() == ADMIN
