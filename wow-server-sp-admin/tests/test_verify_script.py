import os
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
