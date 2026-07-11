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
