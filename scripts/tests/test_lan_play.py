"""Optional direct-LAN WoW play (LAN_IP): installer helpers + generated config."""

import re
import stat
import subprocess
from pathlib import Path

import pytest


SCRIPTS = Path("/src") if Path("/src/install-azerothcore.sh").is_file() else Path(__file__).resolve().parents[1]
INSTALL = (SCRIPTS / "install-azerothcore.sh").read_text()

IP_STUB = """#!/bin/sh
printf '1: lo    inet 127.0.0.1/8 scope host lo\\n'
printf '2: enp2s0    inet 192.168.0.11/24 brd 192.168.0.255 scope global dynamic enp2s0\\n'
printf '3: tailscale0    inet 100.97.217.79/32 scope global tailscale0\\n'
printf '4: eth9    inet 10.20.0.5/20 scope global eth9\\n'
"""


def _function(name: str) -> str:
    match = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", INSTALL, re.S | re.M)
    assert match, f"function not found: {name}"
    return match.group(0)


def _bash(tmp_path: Path, body: str, stdin: str = "") -> subprocess.CompletedProcess[str]:
    bind = tmp_path / "bin"
    bind.mkdir(exist_ok=True)
    ip = bind / "ip"
    ip.write_text(IP_STUB)
    ip.chmod(ip.stat().st_mode | stat.S_IXUSR)
    functions = "".join(_function(n) for n in ("lan_ip_prefix_len", "prefix_to_netmask", "prompt_lan_ip"))
    return subprocess.run(
        ["bash", "-c", f"set -euo pipefail\n{functions}\n{body}"],
        input=stdin,
        text=True,
        capture_output=True,
        env={"PATH": f"{bind}:/usr/bin:/bin"},
        check=False,
    )


@pytest.mark.parametrize(
    ("bits", "mask"),
    [(0, "0.0.0.0"), (8, "255.0.0.0"), (20, "255.255.240.0"), (24, "255.255.255.0"), (32, "255.255.255.255")],
)
def test_prefix_to_netmask(tmp_path, bits, mask):
    result = _bash(tmp_path, f"prefix_to_netmask {bits}")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == mask


def test_lan_ip_prefix_len_matches_exact_local_address(tmp_path):
    result = _bash(
        tmp_path,
        'echo "a=$(lan_ip_prefix_len 192.168.0.11) b=$(lan_ip_prefix_len 10.20.0.5) '
        'c=$(lan_ip_prefix_len 192.168.0.1) d=$(lan_ip_prefix_len 192.168.0.111)"',
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "a=24 b=20 c= d="


def test_prompt_lan_ip_blank_means_tailscale_only(tmp_path):
    result = _bash(tmp_path, 'prompt_lan_ip; echo "RESULT=[$PROMPT_RESULT]"', stdin="\n")
    assert result.returncode == 0, result.stderr
    assert "RESULT=[]" in result.stdout


def test_prompt_lan_ip_rejects_invalid_then_accepts_local_address(tmp_path):
    answers = "not-an-ip\n127.0.0.1\n100.97.217.79\n192.168.0.1\n192.168.0.11\n"
    result = _bash(tmp_path, 'prompt_lan_ip; echo "RESULT=[$PROMPT_RESULT]"', stdin=answers)

    assert result.returncode == 0, result.stderr
    assert "RESULT=[192.168.0.11]" in result.stdout
    assert "Must be an IPv4 address" in result.stderr
    assert result.stderr.count("loopback/Tailscale address") == 2
    assert "192.168.0.1 is not assigned to any local interface" in result.stderr


def test_prompt_lan_ip_allows_non_cgnat_100_networks(tmp_path):
    # 100.64.0.0/10 is Tailscale's CGNAT range; other 100.x are ordinary
    # (if unusual) addresses and must reach the assignment check instead.
    result = _bash(tmp_path, "prompt_lan_ip", stdin="100.10.0.1\n\n")
    assert result.returncode == 0, result.stderr
    assert "100.10.0.1 is not assigned" in result.stderr
    assert "loopback/Tailscale" not in result.stderr


def test_override_heredoc_ships_loopback_placeholders_substituted_from_lan_ip():
    phase25 = INSTALL.split("# PHASE 2.5", 1)[1].split("# PHASE 2.6", 1)[0]
    heredoc = phase25.split("<<'EOF'\n", 1)[1].split("\nEOF\n", 1)[0]

    world = heredoc.split("  ac-worldserver:\n", 1)[1].split("\n  ac-", 1)[0]
    auth = heredoc.split("  ac-authserver:\n", 1)[1].split("\n  ac-", 1)[0]
    assert '      - "127.0.0.1:8085:8085"' in world.splitlines()
    assert '      - "127.0.0.1:3724:3724"' in auth.splitlines()
    assert 'LAN_BIND_IP="${LAN_IP:-127.0.0.1}"' in phase25
    assert "ip_nonlocal_bind" in phase25


def test_phase25_substitution_rewrites_only_the_lan_port_lines(tmp_path):
    phase25 = INSTALL.split("# PHASE 2.5", 1)[1].split("# PHASE 2.6", 1)[0]
    snippet = re.search(r'    LAN_BIND_IP=.*?\n    done\n', phase25, re.S).group(0)
    override = tmp_path / "docker-compose.override.yml"
    original = (
        '      - "127.0.0.1:8085:8085"\n'
        '      - "127.0.0.1:3724:3724"\n'
        '      - "127.0.0.1:7878:7878"\n'
    )
    for lan_ip, bind_ip in (("192.168.0.11", "192.168.0.11"), ("", "127.0.0.1")):
        override.write_text(original)
        result = subprocess.run(
            ["bash", "-c", f"set -euo pipefail\ncd {tmp_path}\nLAN_IP={lan_ip!r}\n{snippet}"],
            text=True, capture_output=True, check=False,
        )
        assert result.returncode == 0, result.stderr
        assert override.read_text() == (
            f'      - "{bind_ip}:8085:8085"\n'
            f'      - "{bind_ip}:3724:3724"\n'
            '      - "127.0.0.1:7878:7878"\n'
        )


def test_lan_ip_is_persisted_and_checked_everywhere():
    save_config = _function("save_config")
    assert "LAN_IP=${LAN_IP:-}" in save_config
    assert 'LAN_IP="${LAN_IP:-}"' in INSTALL  # backfill for older saved configs

    phase26 = INSTALL.split("# PHASE 2.6", 1)[1].split("# PHASE 3", 1)[0]
    assert '"${LAN_IP:-127.0.0.1}:3724"' in phase26
    assert '"${LAN_IP:-127.0.0.1}:8085"' in phase26

    phase5 = INSTALL.split("# PHASE 5 ", 1)[1].split("# PHASE 5.1", 1)[0]
    assert "UPDATE realmlist SET localAddress='${REALM_LOCAL_ADDRESS}', localSubnetMask='${REALM_LOCAL_MASK}' WHERE id=1;" in phase5
    assert 'REALM_LOCAL_ADDRESS="127.0.0.1"' in phase5


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        ('  ac-authserver:\n    ports:\n      - "192.168.0.11:3724:3724"\n', "192.168.0.11"),
        ('  ac-authserver:\n    ports:\n      - "127.0.0.1:3724:3724"\n', ""),
        ("services: {}\n", ""),
        (None, ""),
    ],
)
def test_existing_override_lan_ip_reads_the_authserver_lan_line(tmp_path, override, expected):
    if override is not None:
        (tmp_path / "docker-compose.override.yml").write_text(override)
    result = subprocess.run(
        ["bash", "-c", f"set -euo pipefail\nSTACK_DIR={tmp_path}\n{_function('existing_override_lan_ip')}\n"
         'echo "[$(existing_override_lan_ip)]"'],
        text=True, capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"[{expected}]"


def test_adopt_mode_never_prompts_for_a_new_lan_ip():
    # Adopt skips Phase 2.5, so a prompted LAN IP would reach the realmlist
    # (Phase 5) without any listener. It must come from the existing override.
    block = INSTALL.split('    prompt_yn "Enable systemd auto-start on boot?" y\n', 1)[1].split("    save_config\n", 1)[0]
    adopt, _, fresh = block.partition("    else\n")
    assert 'if [ "$ADOPT" = true ]; then' in adopt
    assert 'LAN_IP="$(existing_override_lan_ip)"' in adopt
    assert "prompt_lan_ip" not in adopt
    assert "prompt_lan_ip" in fresh


def _ac_unit_lan_wait() -> str:
    unit = re.search(
        r"sudo tee /etc/systemd/system/azerothcore\.service <<'EOF' >/dev/null\n(.*?)\nEOF", INSTALL, re.S
    ).group(1)
    waits = [line for line in unit.splitlines() if line.startswith("ExecStartPre=") and "LAN IP" in line]
    assert len(waits) == 1
    return waits[0]


@pytest.mark.parametrize(
    ("override_ip", "assigned", "warns"),
    [("192.168.0.11", False, True), ("192.168.0.11", True, False), ("127.0.0.1", False, False), (None, False, False)],
)
def test_ac_systemd_lan_wait_never_blocks_start(tmp_path, override_ip, assigned, warns):
    import shlex

    line = _ac_unit_lan_wait()
    snippet = shlex.split(line.removeprefix("ExecStartPre="))[2]
    override = tmp_path / "docker-compose.override.yml"
    if override_ip:
        override.write_text(f'  ac-authserver:\n    ports:\n      - "{override_ip}:3724:3724"\n')
    snippet = snippet.replace("/opt/stacks/azerothcore/docker-compose.override.yml", str(override))
    bind = tmp_path / "bin"
    bind.mkdir()
    ip_out = "2: enp2s0    inet 192.168.0.11/24 scope global enp2s0\\n" if assigned else ""
    for name, body in (("ip", f"#!/bin/sh\nprintf '{ip_out}'\n"), ("sleep", "#!/bin/sh\nexit 0\n")):
        (bind / name).write_text(body)
        (bind / name).chmod(0o755)

    result = subprocess.run(
        ["bash", "-c", snippet], text=True, capture_output=True,
        env={"PATH": f"{bind}:/usr/bin:/bin"}, check=False,
    )

    assert result.returncode == 0, result.stderr
    assert ("WARNING: LAN IP 192.168.0.11 not assigned; starting anyway" in result.stdout) == warns
    # systemd substitutes ${VAR} inside words; the snippet must not rely on it.
    assert "${" not in line
