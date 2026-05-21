from unittest.mock import MagicMock, patch

from app.services.docker_client import inspect_worldserver, ContainerInfo


def _mock_container(status="running", started_at="2026-05-20T10:00:00Z"):
    c = MagicMock()
    c.attrs = {
        "State": {
            "Status": status,
            "StartedAt": started_at,
            "ExitCode": 0,
        },
        "Config": {"Image": "acore/ac-wotlk-worldserver:playerbot-local"},
        "Name": "/ac-worldserver",
    }
    c.status = status
    return c


@patch("app.services.docker_client.docker.from_env")
def test_inspect_running_container(mock_from_env):
    client = MagicMock()
    client.containers.get.return_value = _mock_container()
    mock_from_env.return_value = client

    info = inspect_worldserver()
    assert info.status == "running"
    assert info.started_at == "2026-05-20T10:00:00Z"


@patch("app.services.docker_client.docker.from_env")
def test_inspect_missing_container_returns_missing_status(mock_from_env):
    import docker.errors

    client = MagicMock()
    client.containers.get.side_effect = docker.errors.NotFound("nope")
    mock_from_env.return_value = client

    info = inspect_worldserver()
    assert info.status == "missing"
