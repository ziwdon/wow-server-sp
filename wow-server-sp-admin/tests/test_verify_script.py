import datetime as dt
import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
ADMIN_VERIFY_SH = REPO_ROOT / "scripts/verify-azerothcore-admin.sh"


def _executable(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _fake_admin_layout(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Copy the verifier into a disposable layout with a controllable delegate."""
    repo = tmp_path / "repo"
    admin_scripts = repo / "wow-server-sp-admin" / "scripts"
    root_scripts = repo / "scripts"
    admin_scripts.mkdir(parents=True)
    root_scripts.mkdir()
    shutil.copy2(ADMIN_VERIFY_SH, admin_scripts / ADMIN_VERIFY_SH.name)
    _executable(root_scripts / "verify-azerothcore.sh", "#!/bin/sh\nexit \"${ROOT_VERIFY_EXIT:-0}\"\n")

    admin_stack = tmp_path / "admin-stack"
    admin_stack.mkdir()
    admin_stack.joinpath(".env").write_text("TAILSCALE_IP=100.64.0.5\nADMIN_PORT=8000\n")
    ac_stack = tmp_path / "ac-stack"
    ac_stack.mkdir()
    ac_stack.joinpath(".env").write_text("COMPOSE_FILE=docker-compose.yml:docker-compose.admin.yml\n")
    ac_stack.joinpath("docker-compose.admin.yml").write_text("services: {}\n")
    return admin_scripts / ADMIN_VERIFY_SH.name, admin_stack, ac_stack


def _fake_commands(tmp_path: Path) -> tuple[Path, Path]:
    bind = tmp_path / "bin"
    bind.mkdir()
    log = tmp_path / "calls.log"
    _executable(bind / "docker", """#!/bin/sh
printf 'docker %s\\n' "$*" >> "$VERIFY_CALL_LOG"
case "$1" in
  inspect)
    case "$*" in *Health*) printf '%s\\n' healthy ;; *) printf '%s\\n' running ;; esac ;;
  exec) [ "${FAKE_DOCKER_EXEC_FAIL:-0}" = 0 ] || exit 1 ;;
esac
""")
    _executable(bind / "ss", "#!/bin/sh\nprintf 'LISTEN 0 0 100.64.0.5:8000 0.0.0.0:*\\n'\n")
    _executable(bind / "curl", "#!/bin/sh\nexit 0\n")
    _executable(bind / "ip", """#!/bin/sh
printf '1: lo    inet 127.0.0.1/8 scope host lo\\n'
[ "${FAKE_LAN_ASSIGNED:-1}" = 1 ] && printf '2: enp2s0    inet 192.168.0.11/24 scope global enp2s0\\n'
exit 0
""")
    _executable(bind / "sysctl", "#!/bin/sh\necho \"${FAKE_NONLOCAL_BIND:-1}\"\n")
    _executable(bind / "id", "#!/bin/sh\n[ \"$1\" = -un ] && echo verifier || echo verifygroup\n")
    _executable(bind / "stat", "#!/bin/sh\necho verifier:verifygroup\n")
    _executable(bind / "systemctl", "#!/bin/sh\nexit 0\n")
    return bind, log


def _run_admin_verifier(
    script: Path, admin_stack: Path, ac_stack: Path, bind: Path, log: Path, **extra_env: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(script)],
        env={
            **os.environ,
            "STACK_DIR": str(admin_stack),
            "AC_STACK_DIR": str(ac_stack),
            "VERIFY_CALL_LOG": str(log),
            "PATH": f"{bind}:{os.environ['PATH']}",
            **extra_env,
        },
        capture_output=True,
        text=True,
    )


def test_admin_compose_defines_healthcheck_against_healthz():
    compose = (REPO_ROOT / "docker-compose.yml").read_text()

    assert "healthcheck:" in compose
    assert "curl -fsS http://127.0.0.1:8000/healthz" in compose


def test_verify_script_checks_container_health_state():
    script = (REPO_ROOT / "scripts/verify-azerothcore-admin.sh").read_text()

    assert ".State.Health.Status" in script
    assert "container health is healthy" in script


def test_dockerfile_installs_git_and_bundles_backup_script():
    df = (REPO_ROOT / "Dockerfile").read_text()
    # git is needed for backup.sh's revision capture inside the container.
    assert " git" in df or "git " in df
    assert "COPY backup.sh /app/scripts/backup.sh" in df


def test_deploy_scripts_stage_backup_into_build_context():
    install = (REPO_ROOT / "scripts/install-azerothcore-admin.sh").read_text()
    redeploy = (REPO_ROOT / "scripts/redeploy-azerothcore-admin.sh").read_text()
    assert 'backup.sh" "$STACK_DIR/build/backup.sh"' in install
    # Redeploy stages a candidate build before it touches the running stack.
    assert 'backup.sh" "$STAGE_DIR/build/backup.sh"' in redeploy


def test_redeploy_excludes_htmx_vendor_files_from_rsync():
    s = (REPO_ROOT / "scripts/redeploy-azerothcore-admin.sh").read_text()
    # rsync must skip both vendor files so the real HTMX from the install
    # is not overwritten by the repo placeholders on every redeploy.
    assert "--exclude='app/static/htmx.min.js'" in s
    assert "--exclude='app/static/htmx-sse.js'" in s
    # Guard must exist so a missing vendor file causes a loud failure
    # rather than silently baking a broken image.
    assert "placeholder" in s or "htmx_size" in s or "_htmx_size" in s


def test_executable_verifier_keeps_info_advisory_and_uses_only_fake_stack(tmp_path):
    script, admin_stack, ac_stack = _fake_admin_layout(tmp_path)
    bind, log = _fake_commands(tmp_path)

    result = _run_admin_verifier(script, admin_stack, ac_stack, bind, log)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "[INFO] delegating to AC verify script (exit code preserved)" in result.stdout
    assert "Summary:" in result.stdout and "0 FAIL" in result.stdout
    assert "docker exec azerothcore-admin docker inspect ac-worldserver" in log.read_text()


def test_executable_verifier_reports_docker_permission_and_delegated_root_failures(tmp_path):
    script, admin_stack, ac_stack = _fake_admin_layout(tmp_path)
    bind, log = _fake_commands(tmp_path)

    result = _run_admin_verifier(
        script,
        admin_stack,
        ac_stack,
        bind,
        log,
        FAKE_DOCKER_EXEC_FAIL="1",
        ROOT_VERIFY_EXIT="1",
    )

    assert result.returncode == 1
    assert "Docker readiness failed: cannot inspect ac-worldserver from admin container" in result.stdout
    assert "AC verify reported issues" in result.stdout
    assert "docker exec azerothcore-admin docker inspect ac-worldserver" in log.read_text()


def test_executable_verifier_checks_optional_lan_bind(tmp_path):
    script, admin_stack, ac_stack = _fake_admin_layout(tmp_path)
    admin_stack.joinpath(".env").write_text(
        "TAILSCALE_IP=100.64.0.5\nLAN_IP=192.168.0.11\nADMIN_PORT=8000\n"
    )
    bind, log = _fake_commands(tmp_path)

    missing = _run_admin_verifier(script, admin_stack, ac_stack, bind, log)
    assert missing.returncode == 1
    assert "[FAIL] admin port NOT listening on LAN bind 192.168.0.11:8000" in missing.stdout

    _executable(
        bind / "ss",
        "#!/bin/sh\n"
        "printf 'LISTEN 0 0 100.64.0.5:8000 0.0.0.0:*\\n'\n"
        "printf 'LISTEN 0 0 192.168.0.11:8000 0.0.0.0:*\\n'\n",
    )
    present = _run_admin_verifier(script, admin_stack, ac_stack, bind, log)
    assert present.returncode == 0, present.stdout + present.stderr
    assert "admin port listening on LAN interface (192.168.0.11:8000)" in present.stdout
    assert "/healthz returns 200 on LAN bind" in present.stdout


def test_executable_verifier_treats_unassigned_lan_ip_as_advisory(tmp_path):
    script, admin_stack, ac_stack = _fake_admin_layout(tmp_path)
    admin_stack.joinpath(".env").write_text(
        "TAILSCALE_IP=100.64.0.5\nLAN_IP=192.168.0.11\nADMIN_PORT=8000\n"
    )
    bind, log = _fake_commands(tmp_path)

    result = _run_admin_verifier(
        script, admin_stack, ac_stack, bind, log,
        FAKE_LAN_ASSIGNED="0", FAKE_NONLOCAL_BIND="0",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "0 FAIL" in result.stdout
    assert "[INFO] LAN_IP 192.168.0.11 is not assigned to this host" in result.stdout
    assert "[INFO] net.ipv4.ip_nonlocal_bind is not 1" in result.stdout


def test_executable_verifier_skips_lan_checks_without_lan_ip(tmp_path):
    script, admin_stack, ac_stack = _fake_admin_layout(tmp_path)
    admin_stack.joinpath(".env").write_text("TAILSCALE_IP=100.64.0.5\nLAN_IP=\nADMIN_PORT=8000\n")
    bind, log = _fake_commands(tmp_path)

    result = _run_admin_verifier(script, admin_stack, ac_stack, bind, log)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "LAN" not in result.stdout


# --- step 8b: cloud backup is an INFO-only line (spec 6.11) ---


def _write_cloud_files(admin_stack: Path, config: object = None, state: object = None, *, raw: bool = False) -> None:
    """Create ``data/cloud/{config,state}.json`` in the fake admin stack (a str is written as-is when ``raw``)."""
    cloud = admin_stack / "data" / "cloud"
    cloud.mkdir(parents=True, exist_ok=True)
    for name, value in (("config.json", config), ("state.json", state)):
        if value is None:
            continue
        cloud.joinpath(name).write_text(value if raw else json.dumps(value))


def _summary(stdout: str) -> str:
    return next(line for line in stdout.splitlines() if line.startswith("Summary:"))


def _cloud_lines(stdout: str) -> list[str]:
    return [line for line in stdout.splitlines() if "cloud backup" in line]


def test_executable_verifier_reports_cloud_backup_off_without_files(tmp_path):
    script, admin_stack, ac_stack = _fake_admin_layout(tmp_path)
    bind, log = _fake_commands(tmp_path)

    result = _run_admin_verifier(script, admin_stack, ac_stack, bind, log)

    assert result.returncode == 0, result.stdout + result.stderr
    assert _cloud_lines(result.stdout) == ["[INFO] cloud backup: off"]


def test_executable_verifier_cloud_line_never_changes_pass_or_fail_counts(tmp_path):
    script, admin_stack, ac_stack = _fake_admin_layout(tmp_path)
    bind, log = _fake_commands(tmp_path)
    without = _run_admin_verifier(script, admin_stack, ac_stack, bind, log)

    _write_cloud_files(
        admin_stack,
        {"enabled": True, "folder": "/my-files/WoW Backups"},
        {"connection": "disconnected", "last_result": "error"},
    )
    with_cloud = _run_admin_verifier(script, admin_stack, ac_stack, bind, log)

    assert with_cloud.returncode == without.returncode == 0, with_cloud.stdout + with_cloud.stderr
    assert _cloud_lines(with_cloud.stdout) == ["[INFO] cloud backup: disconnected, last result error"]
    assert _summary(with_cloud.stdout) == _summary(without.stdout)
    assert "[FAIL]" not in with_cloud.stdout and "[OK]   cloud" not in with_cloud.stdout


def test_executable_verifier_cloud_line_is_not_a_failure_when_other_checks_fail(tmp_path):
    script, admin_stack, ac_stack = _fake_admin_layout(tmp_path)
    bind, log = _fake_commands(tmp_path)
    _write_cloud_files(admin_stack, {"enabled": True}, {"connection": "vault_error", "last_result": "error"})

    result = _run_admin_verifier(script, admin_stack, ac_stack, bind, log, ROOT_VERIFY_EXIT="1")

    assert result.returncode == 1
    assert "1 FAIL" in result.stdout  # only the delegated AC check; the cloud line adds none
    assert _cloud_lines(result.stdout) == ["[INFO] cloud backup: vault_error, last result error"]


def _ago(**delta) -> str:
    """A ``last_success`` stamp that far before now, as the admin writes it."""
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(**delta)).isoformat(timespec="seconds")


def test_executable_verifier_reports_a_connected_cloud_backup(tmp_path):
    script, admin_stack, ac_stack = _fake_admin_layout(tmp_path)
    bind, log = _fake_commands(tmp_path)
    recent = _ago(hours=5)
    _write_cloud_files(
        admin_stack,
        {"enabled": True},
        {"connection": "connected", "last_result": "ok", "last_success": recent},
    )

    ok = _run_admin_verifier(script, admin_stack, ac_stack, bind, log)
    assert _cloud_lines(ok.stdout) == [f"[INFO] cloud backup: connected, last success 5h ago ({recent})"]

    state = admin_stack / "data" / "cloud" / "state.json"
    state.write_text(json.dumps({"connection": "connected", "last_result": "partial"}))
    never = _run_admin_verifier(script, admin_stack, ac_stack, bind, log)
    assert _cloud_lines(never.stdout) == ["[INFO] cloud backup: connected, last success never, last result partial"]

    older = _ago(hours=30)
    state.write_text(json.dumps({"connection": "connected", "last_result": "error", "last_success": older}))
    failing = _run_admin_verifier(script, admin_stack, ac_stack, bind, log)
    assert _cloud_lines(failing.stdout) == [
        f"[INFO] cloud backup: connected, last success 30h ago ({older}), last result error"
    ]
    assert all(r.returncode == 0 for r in (ok, never, failing))


def test_executable_verifier_cloud_line_prints_the_last_success_age(tmp_path):
    # Spec 6.11: the age of the last success -- minutes under 1 h, hours up to 48 h, then days (M-6).
    script, admin_stack, ac_stack = _fake_admin_layout(tmp_path)
    bind, log = _fake_commands(tmp_path)
    utc_z = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    cases = [
        (_ago(minutes=30), "30m ago"),
        (_ago(seconds=20), "0m ago"),
        (_ago(hours=1, minutes=1), "1h ago"),
        (_ago(hours=47), "47h ago"),
        (_ago(hours=49), "2d ago"),
        (_ago(days=9, hours=3), "9d ago"),
        (utc_z, "2h ago"),
    ]
    for stamp, age in cases:
        _write_cloud_files(admin_stack, {"enabled": True}, {"connection": "connected", "last_result": "ok", "last_success": stamp})
        result = _run_admin_verifier(script, admin_stack, ac_stack, bind, log)
        assert result.returncode == 0, result.stdout + result.stderr
        assert _cloud_lines(result.stdout) == [f"[INFO] cloud backup: connected, last success {age} ({stamp})"], stamp

    # The right shape but not a real time: no age to give.
    _write_cloud_files(
        admin_stack, {"enabled": True}, {"connection": "connected", "last_success": "2026-13-45T99:99:99+00:00"}
    )
    odd = _run_admin_verifier(script, admin_stack, ac_stack, bind, log)
    assert _cloud_lines(odd.stdout) == ["[INFO] cloud backup: connected, last success unknown"]


def test_executable_verifier_cloud_backup_is_off_when_disabled_or_unconfigured(tmp_path):
    script, admin_stack, ac_stack = _fake_admin_layout(tmp_path)
    bind, log = _fake_commands(tmp_path)
    # Disabled in the settings, whatever the leftover state says.
    _write_cloud_files(admin_stack, {"enabled": False}, {"connection": "connected", "last_result": "ok"})
    disabled = _run_admin_verifier(script, admin_stack, ac_stack, bind, log)
    assert _cloud_lines(disabled.stdout) == ["[INFO] cloud backup: off"]

    # Only a state file (no settings yet): the defaults say disabled.
    (admin_stack / "data" / "cloud" / "config.json").unlink()
    no_config = _run_admin_verifier(script, admin_stack, ac_stack, bind, log)
    assert _cloud_lines(no_config.stdout) == ["[INFO] cloud backup: off"]


def test_executable_verifier_cloud_line_without_a_state_file_uses_defaults(tmp_path):
    script, admin_stack, ac_stack = _fake_admin_layout(tmp_path)
    bind, log = _fake_commands(tmp_path)
    _write_cloud_files(admin_stack, {"enabled": True})

    result = _run_admin_verifier(script, admin_stack, ac_stack, bind, log)

    assert _cloud_lines(result.stdout) == ["[INFO] cloud backup: disconnected, last result none"]


def test_executable_verifier_reports_unreadable_cloud_files_as_info(tmp_path):
    script, admin_stack, ac_stack = _fake_admin_layout(tmp_path)
    bind, log = _fake_commands(tmp_path)
    clean = _run_admin_verifier(script, admin_stack, ac_stack, bind, log)
    _write_cloud_files(admin_stack, "{not json", '{"connection": "connected"}', raw=True)

    result = _run_admin_verifier(script, admin_stack, ac_stack, bind, log)

    assert result.returncode == 0, result.stdout + result.stderr
    lines = _cloud_lines(result.stdout)
    assert len(lines) == 1 and lines[0].startswith("[INFO] cloud backup: status unreadable (")
    assert "{not json" not in result.stdout and "Traceback" not in result.stdout + result.stderr
    assert _summary(result.stdout) == _summary(clean.stdout)

    # Valid JSON of the wrong shape is unreadable too, not a crash.
    (admin_stack / "data" / "cloud" / "config.json").write_text("[1, 2]")
    wrong = _run_admin_verifier(script, admin_stack, ac_stack, bind, log)
    assert _cloud_lines(wrong.stdout)[0].startswith("[INFO] cloud backup: status unreadable (")


def test_executable_verifier_survives_a_failing_python3(tmp_path):
    script, admin_stack, ac_stack = _fake_admin_layout(tmp_path)
    bind, log = _fake_commands(tmp_path)
    _write_cloud_files(admin_stack, {"enabled": True}, {"connection": "connected"})
    _executable(bind / "python3", "#!/bin/sh\nexit 1\n")

    result = _run_admin_verifier(script, admin_stack, ac_stack, bind, log)

    assert result.returncode == 0, result.stdout + result.stderr
    lines = _cloud_lines(result.stdout)
    assert len(lines) == 1 and lines[0].startswith("[INFO] cloud backup: status unreadable (")
    assert "0 FAIL" in result.stdout


def test_executable_verifier_cloud_line_prints_only_the_four_status_fields(tmp_path):
    script, admin_stack, ac_stack = _fake_admin_layout(tmp_path)
    bind, log = _fake_commands(tmp_path)
    _write_cloud_files(
        admin_stack,
        {"enabled": True, "folder": "/my-files/Private Name", "labels": ["daily"], "retention_days": 7},
        {
            "connection": "connected",
            "last_result": "error",
            "last_success": "2026-10-28T03:00:12+00:00",
            "last_error": "SECRET-ERROR-TEXT userKeyPassword",
            "issues": [{"name": "azerothcore-backup-daily-2026-10-28.tar.gz", "kind": "conflict"}],
            "remote": [{"name": "azerothcore-backup-daily-2026-10-28.tar.gz"}],
        },
    )

    result = _run_admin_verifier(script, admin_stack, ac_stack, bind, log)

    for forbidden in ("Private Name", "SECRET-ERROR-TEXT", "userKeyPassword", "azerothcore-backup-daily"):
        assert forbidden not in result.stdout + result.stderr
    (line,) = _cloud_lines(result.stdout)
    assert re.fullmatch(
        r"\[INFO\] cloud backup: connected, last success [0-9]+[mhd] ago \(2026-10-28T03:00:12\+00:00\), last result error",
        line,
    ), line


def test_executable_verifier_cloud_line_ignores_odd_field_values(tmp_path):
    script, admin_stack, ac_stack = _fake_admin_layout(tmp_path)
    bind, log = _fake_commands(tmp_path)
    # Free text in a status field is never echoed: unknown values print as "unknown".
    _write_cloud_files(
        admin_stack,
        {"enabled": True},
        {"connection": "disconnected\n[FAIL] injected", "last_result": "x" * 500, "last_success": "not a date\n"},
    )

    result = _run_admin_verifier(script, admin_stack, ac_stack, bind, log)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "injected" not in result.stdout and "xxxx" not in result.stdout
    assert _cloud_lines(result.stdout) == ["[INFO] cloud backup: unknown, last result unknown"]


def test_verify_script_cloud_step_runs_before_the_ac_delegate():
    script = ADMIN_VERIFY_SH.read_text()

    assert script.index("# 8b.") < script.index("# 9. AC stack still functional")
    assert "$STACK_DIR/data/cloud" in script
