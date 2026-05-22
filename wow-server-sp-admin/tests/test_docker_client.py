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


@patch("app.services.docker_client.docker.from_env")
def test_stats_worldserver_cpu_is_system_relative(mock_from_env):
    from app.services.docker_client import stats_worldserver

    c = MagicMock()
    c.status = "running"
    # 8-core host; container used 10 000 000 ns of cpu out of 1 000 000 000 ns total.
    # System-relative: (10M / 1B) * 100 = 1.0%
    # Old per-core formula would give 1.0% * 8 = 8.0%
    c.stats.return_value = {
        "cpu_stats": {
            "cpu_usage": {"total_usage": 10_000_000},
            "system_cpu_usage": 1_000_000_000,
            "online_cpus": 8,
        },
        "precpu_stats": {
            "cpu_usage": {"total_usage": 0},
            "system_cpu_usage": 0,
        },
        "memory_stats": {
            "usage": 1024,
            "limit": 1024 * 1024,
            "stats": {"cache": 0},
        },
    }
    mock_from_env.return_value = MagicMock()
    mock_from_env.return_value.containers.get.return_value = c

    result = stats_worldserver()
    assert result is not None
    assert result.cpu_percent == 1.0
