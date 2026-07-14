import subprocess
from unittest.mock import ANY, MagicMock, patch

from app.services import actions
from app.services.actions import ActionResult, run_stop


@patch("app.services.actions.time.sleep")  # collapse waits in unit tests
@patch("app.services.actions.subprocess.run")
@patch("app.services.actions.WorldserverConsole")
@patch("app.services.actions.inspect_worldserver")
def test_stop_runs_console_commands_then_docker_stop(
    mock_inspect, mock_console_cls, mock_run, mock_sleep,
):
    from app.services.docker_client import ContainerInfo

    states = iter([
        # initial inspect: running
        ContainerInfo(status="running", started_at=None, exit_code=None, image=None),
        # post-docker-stop inspect: exited
        ContainerInfo(status="exited", started_at=None, exit_code=0, image=None),
    ])
    mock_inspect.side_effect = lambda: next(states)

    console = MagicMock()
    mock_console_cls.return_value.__enter__.return_value = console

    progress: list[str] = []
    result = run_stop(
        on_progress=lambda step, msg: progress.append(step),
        grace_seconds=30,
    )

    assert result == ActionResult.OK

    # Console commands, in order:
    sent = [call.args[0] for call in console.send.call_args_list]
    assert sent[0].startswith("announce ") and "30 seconds" in sent[0]
    assert sent[1].startswith("notify ") and "30s" in sent[1]
    assert sent[2].startswith("announce ") and "Final 10 seconds" in sent[2]
    assert sent[3].startswith("notify ") and "10s" in sent[3]
    assert sent[4] == "saveall"
    # No 'server shutdown N' in the sequence — by design.
    assert not any("server shutdown" in s for s in sent)

    # docker stop is called with --time 60 (bumped from 30 to give AC's
    # final saveall headroom under bot-heavy load).
    docker_stop_calls = [
        c for c in mock_run.call_args_list if "stop" in c.args[0]
    ]
    assert docker_stop_calls, "docker stop must be called"
    assert "--time" in docker_stop_calls[0].args[0]
    assert "60" in docker_stop_calls[0].args[0]

    assert "docker_stop" in progress
    assert "wait_exit" in progress


@patch("app.services.actions.inspect_worldserver")
def test_stop_skips_when_already_stopped(mock_inspect):
    from app.services.docker_client import ContainerInfo

    mock_inspect.return_value = ContainerInfo(
        status="exited", started_at=None, exit_code=0, image=None,
    )
    result = run_stop(on_progress=lambda *_: None, grace_seconds=30)
    assert result == ActionResult.OK


@patch("app.services.actions.WorldserverConsole")
@patch("app.services.actions.inspect_worldserver")
def test_stop_skips_when_container_is_not_running(
    mock_inspect, mock_console,
):
    from app.services.docker_client import ContainerInfo

    for status in ("created", "dead"):
        mock_inspect.return_value = ContainerInfo(
            status=status, started_at=None, exit_code=None, image=None,
        )

        result = run_stop(on_progress=lambda *_: None, grace_seconds=30)

        assert result == ActionResult.OK
        mock_console.assert_not_called()


@patch("app.services.actions.subprocess.run")
@patch("app.services.actions.WorldserverConsole")
@patch("app.services.actions.inspect_worldserver")
def test_stop_skips_docker_stop_when_container_stops_during_attach(
    mock_inspect, mock_console, mock_run,
):
    from app.services.docker_client import ContainerInfo

    states = iter([
        ContainerInfo(status="running", started_at=None, exit_code=None, image=None),
        ContainerInfo(status="exited", started_at=None, exit_code=0, image=None),
    ])
    mock_inspect.side_effect = lambda: next(states)
    mock_console.return_value.__enter__.side_effect = RuntimeError("attach lost")

    result = run_stop(on_progress=lambda *_: None, grace_seconds=30)

    assert result == ActionResult.OK
    mock_run.assert_not_called()


@patch("app.services.actions.subprocess.run")
@patch("app.services.actions._wait_for_world_init")
@patch("app.services.actions.inspect_worldserver")
def test_start_runs_compose_up_and_waits_for_world_init(
    mock_inspect, mock_wait_init, mock_run,
):
    from app.services.docker_client import ContainerInfo

    states = iter([
        ContainerInfo(status="exited", started_at=None, exit_code=0, image=None),
        ContainerInfo(status="running", started_at=None, exit_code=None, image=None),
    ])
    mock_inspect.side_effect = lambda: next(states)
    mock_wait_init.return_value = True
    mock_run.return_value = MagicMock(returncode=0)

    from app.services.actions import run_start
    result = run_start(on_progress=lambda *_: None)
    assert result == ActionResult.OK
    assert any("compose" in str(c.args) for c in mock_run.call_args_list)


@patch("app.services.actions.docker.from_env")
def test_ac_compose_base_args_translates_host_mount_and_compose_env(
    mock_docker_from_env, tmp_path,
):
    """Compose reads files inside /ac but resolves mounts on the host."""
    from app.services.actions import _ac_compose_base_args

    ac_stack = tmp_path / "ac"
    ac_stack.mkdir()
    (ac_stack / ".env").write_text(
        "COMPOSE_FILE=docker-compose.yml:docker-compose.override.yml:docker-compose.admin.yml\n"
        "COMPOSE_PROJECT_NAME=custom-azerothcore\n"
    )
    mock_docker_from_env.return_value.containers.get.return_value.attrs = {
        "Mounts": [
            {"Source": "/var/lib/azerothcore", "Destination": "/ac"},
        ],
    }

    command, extra_env = _ac_compose_base_args(ac_stack)

    assert command == [
        "docker", "compose",
        "--project-name", "custom-azerothcore",
        "--project-directory", "/var/lib/azerothcore",
        "-f", str(ac_stack / "docker-compose.yml"),
        "-f", str(ac_stack / "docker-compose.override.yml"),
        "-f", str(ac_stack / "docker-compose.admin.yml"),
        "--env-file", str(ac_stack / ".env"),
    ]
    assert extra_env == {
        "DOCKER_AC_ENV_FILE": str(ac_stack / "conf/dist/env.ac"),
    }


@patch("app.services.actions.docker.from_env", side_effect=RuntimeError("no daemon"))
def test_ac_compose_base_args_falls_back_when_inspection_or_env_is_unavailable(
    _mock_docker_from_env, tmp_path,
):
    from app.services.actions import _ac_compose_base_args

    ac_stack = tmp_path / "missing-ac-stack"

    command, extra_env = _ac_compose_base_args(ac_stack)

    assert command == [
        "docker", "compose",
        "--project-name", "azerothcore",
        "--project-directory", "/opt/stacks/azerothcore",
        "-f", str(ac_stack / "docker-compose.yml"),
        "--env-file", str(ac_stack / ".env"),
    ]
    assert extra_env == {
        "DOCKER_AC_ENV_FILE": str(ac_stack / "conf/dist/env.ac"),
    }


@patch("app.services.actions.docker.from_env")
def test_ac_compose_base_args_ignores_malformed_env_lines(
    mock_docker_from_env, tmp_path,
):
    from app.services.actions import _ac_compose_base_args

    ac_stack = tmp_path / "ac"
    ac_stack.mkdir()
    (ac_stack / ".env").write_text(
        "COMPOSE_FILE docker-compose.override.yml\n"
        "COMPOSE_PROJECT_NAME custom-azerothcore\n"
        "UNRELATED=value\n"
    )
    mock_docker_from_env.return_value.containers.get.return_value.attrs = {"Mounts": []}

    command, _extra_env = _ac_compose_base_args(ac_stack)

    assert command == [
        "docker", "compose",
        "--project-name", "azerothcore",
        "--project-directory", "/opt/stacks/azerothcore",
        "-f", str(ac_stack / "docker-compose.yml"),
        "--env-file", str(ac_stack / ".env"),
    ]


@patch("app.services.actions.subprocess.run")
@patch("app.services.actions._wait_for_world_init", return_value=True)
@patch("app.services.actions._ac_compose_base_args")
@patch("app.services.actions.inspect_worldserver")
def test_start_uses_translated_compose_command_and_absolute_env_override(
    mock_inspect, mock_compose_args, mock_wait_init, mock_run, monkeypatch,
):
    from app.services.actions import run_start
    from app.services.docker_client import ContainerInfo

    monkeypatch.setenv("AC_STACK_DIR", "/ac")
    mock_inspect.return_value = ContainerInfo("exited", None, 0, None)
    mock_compose_args.return_value = (
        [
            "docker", "compose", "--project-name", "custom-azerothcore",
            "--project-directory", "/var/lib/azerothcore",
            "-f", "/ac/docker-compose.yml", "--env-file", "/ac/.env",
        ],
        {"DOCKER_AC_ENV_FILE": "/ac/conf/dist/env.ac"},
    )
    mock_run.return_value = MagicMock(returncode=0)

    assert run_start(on_progress=lambda *_: None) == ActionResult.OK

    assert mock_compose_args.call_args.args == (actions.Path("/ac"),)
    assert mock_run.call_args.args == (
        [
            "docker", "compose", "--project-name", "custom-azerothcore",
            "--project-directory", "/var/lib/azerothcore",
            "-f", "/ac/docker-compose.yml", "--env-file", "/ac/.env",
            "up", "-d", "ac-worldserver", "ac-database",
        ],
    )
    assert mock_run.call_args.kwargs["env"]["DOCKER_AC_ENV_FILE"] == "/ac/conf/dist/env.ac"
    assert mock_run.call_args.kwargs["capture_output"] is True
    assert mock_run.call_args.kwargs["text"] is True
    assert mock_run.call_args.kwargs["timeout"] == 300
    mock_wait_init.assert_called_once_with(timeout=300, on_progress=ANY)


@patch("app.services.actions.subprocess.run")
@patch("app.services.actions._ac_compose_base_args")
@patch("app.services.actions.inspect_worldserver")
def test_start_returns_error_when_translated_compose_up_fails(
    mock_inspect, mock_compose_args, mock_run,
):
    from app.services.actions import run_start
    from app.services.docker_client import ContainerInfo

    mock_inspect.return_value = ContainerInfo("exited", None, 0, None)
    mock_compose_args.return_value = (["docker", "compose"], {"DOCKER_AC_ENV_FILE": "/ac/env.ac"})
    mock_run.return_value = MagicMock(returncode=1, stderr="invalid compose file")
    progress: list[tuple[str, str]] = []

    assert run_start(on_progress=lambda step, message: progress.append((step, message))) == ActionResult.ERROR
    assert progress == [("compose_up", "docker compose up -d ac-worldserver ac-database"),
                        ("compose_up", "compose up FAILED: invalid compose file")]


def test_wait_for_world_init_matches_real_ac_log_line(tmp_path, monkeypatch):
    """Pin the exact log line AC emits. Verified against a real AC
    install — see CLAUDE.md's note about 'WORLD: World Initialized'.

    Simulates the production flow: file has stale prior-boot content
    (already contains 'World Initialized'), then AC opens in mode 'w'
    which truncates (size drops below entry baseline), then AC writes
    the new boot's lines."""
    from app.services.actions import _wait_for_world_init

    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    log_path = log_dir / "Server.log"
    # Stale prior-boot tail — must NOT match.
    log_path.write_text("WORLD: World Initialized In 0 Minutes 12 Seconds\n" * 4)
    monkeypatch.setenv("AC_STACK_DIR", str(tmp_path))

    state = {"step": 0}

    def _fake_sleep(_secs):
        state["step"] += 1
        if state["step"] == 1:
            # AC opens Server.log with mode 'w' -> truncate.
            log_path.write_text("")
        elif state["step"] == 2:
            # AC writes the boot init lines and the world-init marker.
            log_path.write_text(
                "Init line A\nInit line B\n"
                "WORLD: World Initialized In 0 Minutes 8 Seconds\n"
            )

    monkeypatch.setattr("app.services.actions.time.sleep", _fake_sleep)
    assert _wait_for_world_init(timeout=10, on_progress=lambda *_: None) is True


def test_wait_for_world_init_case_insensitive(tmp_path, monkeypatch):
    """Future-proof against upstream casing changes. Same truncate-then-write
    flow as above so we exercise the post-baseline path."""
    from app.services.actions import _wait_for_world_init

    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    log_path = log_dir / "Server.log"
    log_path.write_text("stale prior boot junk\n" * 5)
    monkeypatch.setenv("AC_STACK_DIR", str(tmp_path))

    fired = {"v": False}

    def _fake_sleep(_secs):
        if not fired["v"]:
            log_path.write_text("")
            with log_path.open("a") as f:
                f.write("world initialized in 5 minutes 1 seconds\n")
            fired["v"] = True

    monkeypatch.setattr("app.services.actions.time.sleep", _fake_sleep)
    assert _wait_for_world_init(timeout=10, on_progress=lambda *_: None) is True


def test_wait_for_world_init_ignores_stale_prior_boot_line(tmp_path, monkeypatch):
    """If Server.log only contains a 'World Initialized' line from the
    previous boot and nothing new is written, the function must NOT match
    — otherwise Restart reports success while AC is still booting."""
    from app.services.actions import _wait_for_world_init

    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "Server.log").write_text(
        "WORLD: World Initialized In 0 Minutes 12 Seconds\n"
    )
    monkeypatch.setenv("AC_STACK_DIR", str(tmp_path))
    # No file mutation during wait -> must time out.
    monkeypatch.setattr("app.services.actions.time.sleep", lambda _s: None)
    assert _wait_for_world_init(timeout=0, on_progress=lambda *_: None) is False


@patch("app.services.actions.subprocess.run")
@patch("app.services.actions._wait_for_status", return_value=True)
@patch("app.services.actions.WorldserverConsole")
@patch("app.services.actions.inspect_worldserver")
def test_run_stop_does_not_take_a_backup(mock_inspect, mock_console, mock_wait, mock_run):
    mock_inspect.return_value = type("I", (), {"status": "running", "started_at": None, "exit_code": None})()
    mock_console.return_value.__enter__.return_value = mock_console.return_value
    with patch("app.services.backup.run_backup") as mock_backup:
        result = actions.run_stop(on_progress=lambda *a: None, grace_seconds=0)
        mock_backup.assert_not_called()
    assert result == ActionResult.OK


@patch("app.services.backup.run_backup")
def test_run_backup_manual_uses_manual_label(mock_backup):
    mock_backup.return_value = type("R", (), {"ok": True, "archive": "x", "output": ""})()
    result = actions.run_backup_manual(on_progress=lambda *a: None)
    assert result == ActionResult.OK
    assert mock_backup.call_args.args[0] == "manual"


@patch("app.services.backup.run_backup")
def test_run_backup_manual_maps_backup_timeout_to_timeout(mock_backup):
    mock_backup.return_value = type(
        "R", (), {"ok": False, "archive": None, "output": "", "timed_out": True}
    )()
    assert actions.run_backup_manual(on_progress=lambda *_: None) == ActionResult.TIMEOUT


@patch("app.services.actions.subprocess.run", side_effect=subprocess.TimeoutExpired("docker", 300))
@patch("app.services.actions.inspect_worldserver")
def test_start_maps_compose_timeout_to_timeout(mock_inspect, mock_run):
    from app.services.docker_client import ContainerInfo
    from app.services.actions import run_start

    mock_inspect.return_value = ContainerInfo("exited", None, 0, None)
    assert run_start(on_progress=lambda *_: None) == ActionResult.TIMEOUT


@patch("app.services.actions.subprocess.run", side_effect=subprocess.TimeoutExpired("docker", 180))
@patch("app.services.actions.WorldserverConsole")
@patch("app.services.actions.inspect_worldserver")
def test_stop_maps_docker_timeout_to_timeout(mock_inspect, mock_console, mock_run):
    from app.services.docker_client import ContainerInfo

    mock_inspect.return_value = ContainerInfo("running", None, None, None)
    assert run_stop(on_progress=lambda *_: None, grace_seconds=0) == ActionResult.TIMEOUT


@patch("app.services.actions.subprocess.run")
@patch("app.services.actions._wait_for_status", return_value=True)
def test_force_stop_escalates_to_kill(mock_wait, mock_run):
    from app.services.actions import run_force_stop

    mock_run.side_effect = [MagicMock(returncode=1, stderr="nope"), MagicMock(returncode=0)]
    assert run_force_stop(on_progress=lambda *_: None) == ActionResult.OK
    assert mock_run.call_args_list[1].args[0] == ["docker", "kill", "ac-worldserver"]


@patch("app.services.actions.subprocess.run", side_effect=subprocess.TimeoutExpired("docker", 180))
def test_force_stop_maps_timeout_to_timeout(mock_run):
    from app.services.actions import run_force_stop

    assert run_force_stop(on_progress=lambda *_: None) == ActionResult.TIMEOUT
