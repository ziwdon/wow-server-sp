"""Regression guards for duplicated installer/verification conventions."""

import re
from pathlib import Path

import pytest


SCRIPTS = Path("/src") if Path("/src/install-azerothcore.sh").is_file() else Path(__file__).resolve().parents[1]
INSTALL = (SCRIPTS / "install-azerothcore.sh").read_text()
VERIFY = (SCRIPTS / "verify-azerothcore.sh").read_text()
EXCLUDED = {"AC_PLAYERBOTS_DATABASE_INFO"}
XP = {
    "AC_RATE_XP_QUEST", "AC_RATE_XP_KILL", "AC_RATE_XP_EXPLORE", "AC_RATE_XP_MONEY",
    "AC_RATE_XP_REPUTATION", "AC_RATE_XP_SKILL_DISCOVERY", "AC_RATE_XP_ITEM_NORMAL",
    "AC_RATE_XP_ITEM_UNCOMMON", "AC_RATE_SKILL_CRAFTING", "AC_RATE_SKILL_GATHERING",
    "AC_RATE_SKILL_WEAPON", "AC_RATE_SKILL_DEFENSE",
}
DYNAMIC = {
    "AC_AI_PLAYERBOT_MIN_RANDOM_BOTS", "AC_AI_PLAYERBOT_MAX_RANDOM_BOTS",
    "AC_MAP_UPDATE_THREADS", "AC_GAME_TYPE",
}


def _array(text: str, marker: str) -> set[str]:
    match = re.search(marker + r"\s*=\(\n(.*?)\n\s*\)", text, re.S)
    assert match, f"array not found: {marker}"
    return set(re.findall(r"\bAC_[A-Z0-9_]+\b", match.group(1)))


def test_managed_static_env_var_lists_stay_in_sync():
    install_managed = _array(INSTALL, r"local managed_vars")
    verify_managed = _array(VERIFY, r"managed_vars")
    override_expected = _array(VERIFY, r"OVERRIDE_EXPECTED")
    phase26 = set(re.findall(
        r"\bAC_[A-Z0-9_]+\b",
        re.search(r"# Worldserver overrides must be present.*?done", INSTALL, re.S).group(0),
    ))
    # Prompt-substituted values have dedicated shape/value checks rather than
    # entries in OVERRIDE_EXPECTED, but must still appear in Phase 2.6.
    expected = install_managed - XP - EXCLUDED - DYNAMIC
    assert expected == verify_managed - XP - EXCLUDED - DYNAMIC == override_expected - XP - EXCLUDED
    assert (install_managed | EXCLUDED | DYNAMIC) - XP <= phase26
    assert "AC_GAME_TYPE" in phase26


def test_xp_field_order_and_uninstall_safety_are_pinned():
    fields = (
        "quest kill explore money reputation skill_discovery item_normal item_uncommon "
        "skill_crafting skill_gathering skill_weapon skill_defense"
    )
    normalized = re.sub(r"\\\s*\n", " ", INSTALL)
    read_pattern = re.compile(
        r"read -r\s+" + re.escape(fields).replace(r"\ ", r"\s+")
        + r"\s+< <\(xp_rate_values", re.S,
    )
    assert len(read_pattern.findall(normalized)) >= 3
    core_uninstaller = "\n".join(
        line for line in (SCRIPTS / "uninstall-azerothcore.sh").read_text().splitlines()
        if not line.lstrip().startswith("#")
    )
    assert "--remove-orphans" not in core_uninstaller
    admin_uninstaller = SCRIPTS.parent / "wow-server-sp-admin/scripts/uninstall-azerothcore-admin.sh"
    if admin_uninstaller.is_file():
        admin_code = "\n".join(
            line for line in admin_uninstaller.read_text().splitlines()
            if not line.lstrip().startswith("#")
        )
        assert "--remove-orphans" not in admin_code
    else:
        pytest.skip("admin sibling is not mounted in the standalone scripts test container")


def test_systemd_stack_unit_recovers_from_docker_restarts():
    """The Compose launcher must survive a daemon/package restart mid-start."""
    systemd_unit = re.search(
        r"sudo tee /etc/systemd/system/azerothcore\.service <<'EOF' >/dev/null\n(.*?)\nEOF",
        INSTALL,
        re.S,
    )
    assert systemd_unit, "azerothcore.service heredoc not found"
    unit = systemd_unit.group(1)
    assert "PartOf=docker.service" in unit
    assert "Restart=on-failure" in unit
    assert "RestartSec=10s" in unit


def test_systemd_stack_unit_gives_worldserver_a_save_grace_period():
    systemd_unit = re.search(
        r"sudo tee /etc/systemd/system/azerothcore\.service <<'EOF' >/dev/null\n(.*?)\nEOF",
        INSTALL,
        re.S,
    )
    assert systemd_unit, "azerothcore.service heredoc not found"
    unit = systemd_unit.group(1)
    assert "ExecStop=/usr/bin/docker compose down --timeout 60" in unit
    assert "TimeoutStopSec=75" in unit


def test_intentional_shellcheck_cases_are_narrowly_suppressed_and_documented():
    claude = (SCRIPTS.parent / "CLAUDE.md").read_text()

    assert INSTALL.count("# shellcheck disable=SC2001") == 1
    assert INSTALL.count("# shellcheck disable=SC2016") == 1
    assert INSTALL.count("# shellcheck disable=SC2012") == 3
    assert "shellcheck scripts/*.sh wow-server-sp-admin/scripts/*.sh" in claude
    assert "narrow local suppressions" in claude


def test_manual_compose_override_guidance_recreates_only_worldserver():
    readme = (SCRIPTS.parent / "README.md").read_text()
    post_install_tuning = INSTALL.split('echo "Post-install tuning:"', 1)[1]
    worldserver_reference = (
        SCRIPTS.parent / "skills/wow-server-sp-gamemaster/references/ref-config-worldserver.md"
    ).read_text()
    manual_override_guidance = worldserver_reference.split("## Where to Edit Config", 1)[1].split(
        "## Key worldserver.conf Settings", 1
    )[0]

    assert "docker compose up -d --force-recreate ac-worldserver" in readme
    assert "docker compose up -d --force-recreate ac-worldserver" in post_install_tuning
    assert "docker compose restart ac-worldserver" not in post_install_tuning
    assert "docker compose up -d --force-recreate ac-worldserver" in manual_override_guidance
    assert "docker compose restart ac-worldserver" not in manual_override_guidance
    assert "docker logs --tail 50 ac-worldserver" in manual_override_guidance
    assert "Confirm the logs include `WORLD: World Initialized`." in manual_override_guidance


def test_installer_profile_default_and_installation_reference_stay_aligned():
    reference = (SCRIPTS.parent / "skills/wow-server-sp-gamemaster/references/ref-installation.md").read_text()

    assert 'AC_AI_PLAYERBOT_MIN_RANDOM_BOTS:-1500' in INSTALL
    assert 'Random bot count (1-2000, applied to both MIN and MAX)" 1 2000 1500' in INSTALL
    assert "Server XP rate (x1, x3, x5, or x7)" in reference
    assert "Playerbot count (default: 1500)" in reference


def test_capacity_warning_override_is_documented():
    readme = (SCRIPTS.parent / "README.md").read_text()
    reference = (SCRIPTS.parent / "skills/wow-server-sp-gamemaster/references/ref-installation.md").read_text()

    assert "--allow-capacity-warnings" in readme
    assert "--allow-capacity-warnings" in reference


def test_capacity_metric_test_seams_are_not_available_to_production_installs():
    assert "INSTALLER_TEST_OPT_FREE_KIB" not in INSTALL
    assert "INSTALLER_TEST_MEM_TOTAL_KIB" not in INSTALL
    assert "INSTALLER_TEST_ASSUME_INTERACTIVE" not in INSTALL
