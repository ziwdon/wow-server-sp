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
