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
    "AC_PLAYERBOTS_MIN_RANDOM_BOTS", "AC_PLAYERBOTS_MAX_RANDOM_BOTS",
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


def _stack_unit() -> str:
    systemd_unit = re.search(
        r"sudo tee /etc/systemd/system/azerothcore\.service <<'EOF' >/dev/null\n(.*?)\nEOF",
        INSTALL,
        re.S,
    )
    assert systemd_unit, "azerothcore.service heredoc not found"
    return systemd_unit.group(1)


def test_systemd_stack_unit_starts_only_database_and_login_server():
    """The world server starts only from the admin app's Start button."""
    starts = [line for line in _stack_unit().splitlines() if line.startswith("ExecStart=")]
    assert starts == ["ExecStart=/usr/bin/docker compose up -d --no-build ac-database ac-authserver"]


def _override_heredoc() -> str:
    match = re.search(r"cat > docker-compose\.override\.yml <<'EOF'\n(.*?)\nEOF\n", INSTALL, re.S)
    assert match, "override heredoc not found"
    return match.group(1)


def test_override_heredoc_turns_off_worldserver_auto_restart():
    """Docker must not start the world server by itself, not even after a
    power cut (unless-stopped would)."""
    block = re.search(r"^  ac-worldserver:\n(.*?)(?=^  \S|\Z)", _override_heredoc(), re.S | re.M)
    assert block, "ac-worldserver block not found in override heredoc"
    assert '    restart: "no"' in block.group(1).splitlines()
    assert _override_heredoc().count("restart:") == 1


def _phase_2_6_restart_probe() -> str:
    phase = INSTALL[INSTALL.index("# PHASE 2.6"):INSTALL.index("# PHASE 3 ")]
    lines = [line for line in phase.splitlines() if line.lstrip().startswith('ws_restart="$(awk')]
    assert len(lines) == 1, "Phase 2.6 must read ac-worldserver's effective restart policy once"
    return lines[0].strip()


@pytest.mark.parametrize(
    ("effective", "expected"),
    [
        ('services:\n  ac-database:\n    restart: unless-stopped\n  ac-worldserver:\n'
         '    image: x\n    restart: "no"\n  ac-zzz:\n    restart: always\n', "no"),
        ('services:\n  ac-database:\n    restart: "no"\n  ac-worldserver:\n'
         '    image: x\n    restart: unless-stopped\n', "unless-stopped"),
        ('services:\n  ac-worldserver:\n    image: x\n  ac-zzz:\n    restart: "no"\n', ""),
        # Compose v5 renders the YAML-reserved word in single quotes.
        ("services:\n  ac-worldserver:\n    restart: 'no'\n", "no"),
    ],
)
def test_phase_2_6_reads_the_worldservers_own_effective_restart_policy(tmp_path, effective, expected):
    import subprocess

    config = tmp_path / "effective.yml"
    config.write_text(effective)
    script = f'COMPOSE_EFFECTIVE={config}\n{_phase_2_6_restart_probe()}\nprintf "%s" "$ws_restart"\n'
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True)
    assert result.stdout == expected


def test_phase_2_6_fails_unless_worldserver_restart_is_no():
    phase = INSTALL[INSTALL.index("# PHASE 2.6"):INSTALL.index("# PHASE 3 ")]
    assert 'if [ "$ws_restart" != "no" ]; then' in phase


def test_installer_tells_the_owner_the_world_server_does_not_start_at_boot():
    final = INSTALL[INSTALL.index("# Finalisation"):]
    assert "The world server does not start at boot" in final
    assert "docker compose up -d ac-worldserver" in final


def test_no_script_uses_the_deprecated_docker_stop_time_flag():
    """Docker 28+ deprecates `docker stop --time` (warns, may be dropped);
    `-t` works on every version."""
    scripts = sorted(SCRIPTS.glob("*.sh")) + sorted((SCRIPTS / "lib").glob("*.sh"))
    admin_scripts = SCRIPTS.parent / "wow-server-sp-admin/scripts"
    scripts += sorted(admin_scripts.glob("*.sh")) if admin_scripts.is_dir() else []
    offenders = [
        f"{path.name}:{n}"
        for path in scripts
        for n, line in enumerate(path.read_text().splitlines(), 1)
        if re.search(r"\bstop\b.*--time\b(?!out)", line)
    ]
    assert offenders == []


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

    assert 'AC_PLAYERBOTS_MIN_RANDOM_BOTS:-1500' in INSTALL
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


def test_phase1_pins_upstream_revisions_and_applies_local_patches():
    assert 'source "${SCRIPT_SOURCE_DIR}/lib/upstream-versions.sh"' in INSTALL
    phase1 = INSTALL[INSTALL.index('"ZhengPeiRu21/mod-individual-progression"'):INSTALL.index("# VERIFY Phase 1")]
    assert "pin_upstream_checkouts" in phase1


def test_upstream_versions_file_pins_full_shas():
    versions = (Path(__file__).resolve().parents[1] / "upstream-versions.env").read_text()
    for key in ("AC_CORE_REV", "MOD_PLAYERBOTS_REV", "MOD_AHBOT_REV", "MOD_IP_REV"):
        value = next(l.split("=", 1)[1] for l in versions.splitlines() if l.startswith(key + "="))
        assert len(value) == 40 and all(c in "0123456789abcdef" for c in value), key


def test_no_pre_rename_playerbots_names_remain():
    verify = (Path(__file__).resolve().parents[1] / "verify-azerothcore.sh").read_text()
    for text in (INSTALL, verify):
        assert "AC_AI_PLAYERBOT_" not in text
        assert "AiPlayerbot." not in text
    assert 'PLAYERBOT_COUNT="${AC_PLAYERBOTS_MIN_RANDOM_BOTS:-1500}"' in INSTALL
