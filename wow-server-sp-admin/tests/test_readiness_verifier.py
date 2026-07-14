from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_liveness_healthcheck_stays_independent_of_docker_and_database_readiness():
    compose = (REPO_ROOT / "docker-compose.yml").read_text()

    assert "curl -fsS http://127.0.0.1:8000/healthz" in compose
    assert "/readyz" not in compose


def test_verifier_checks_non_root_docker_access_and_classifies_database_failures():
    script = (REPO_ROOT / "scripts/verify-azerothcore-admin.sh").read_text()

    assert "timeout 5 docker exec azerothcore-admin docker inspect ac-worldserver" in script
    assert "timeout 5 docker exec azerothcore-admin getent hosts ac-database" in script
    assert 'socket.create_connection(("ac-database", 3306), timeout=2)' in script
    assert "DB_DOWN" in script
    assert "DB_DNS_UNAVAILABLE" in script
