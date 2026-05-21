from unittest.mock import MagicMock, patch

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
        run_backup=False,  # skip backup in unit test
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
    result = run_stop(on_progress=lambda *_: None, run_backup=False, grace_seconds=30)
    assert result == ActionResult.OK


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
