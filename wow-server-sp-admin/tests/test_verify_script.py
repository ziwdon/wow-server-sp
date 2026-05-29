from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


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
    for name in ("install-azerothcore-admin.sh", "redeploy-azerothcore-admin.sh"):
        s = (REPO_ROOT / "scripts" / name).read_text()
        assert 'backup.sh" "$STACK_DIR/build/backup.sh"' in s
