import os
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.main import app
from app.state import init_state


def test_verify_env_vars_bound_reports_missing_and_silent_drop():
    from app.services.actions import verify_env_vars_bound

    with patch("app.services.actions._read_live_env", return_value={"AC_FOO_ENABLE": "1"}), \
         patch("app.services.actions._read_loaded_config", return_value={"AC_FOO_ENABLE"}):
        assert verify_env_vars_bound(
            {"AC_FOO_ENABLE": "1"}, env_var_to_key={"AC_FOO_ENABLE": "Foo.Enable"},
            on_progress=lambda *_: None,
        ) == []
    with patch("app.services.actions._read_live_env", return_value={}), \
         patch("app.services.actions._read_loaded_config", return_value={"AC_FOO_ENABLE"}):
        failed = verify_env_vars_bound(
            {"AC_FOO_ENABLE": "1"}, env_var_to_key={"AC_FOO_ENABLE": "Foo.Enable"},
            on_progress=lambda *_: None,
        )
        assert failed[0].env_var == "AC_FOO_ENABLE"
        assert "mismatch" in failed[0].reason
    with patch("app.services.actions._read_live_env", return_value={"AC_FOO_ENABLE": "1"}), \
         patch("app.services.actions._read_loaded_config", return_value=set()):
        failed = verify_env_vars_bound(
            {"AC_FOO_ENABLE": "1"}, env_var_to_key={"AC_FOO_ENABLE": "Foo.Enable"},
            on_progress=lambda *_: None,
        )
        assert "silently dropped" in failed[0].reason


def test_apply_verification_failure_returns_error(tmp_path):
    from app.main import _run_apply_then_verify
    from app.services.actions import ActionResult, VerifyFailure

    _init_apply_state(tmp_path)
    import app.main as main
    state = __import__("app.state", fromlist=["get_state"]).get_state()
    current = MagicMock(verify_failed=[])
    main.runner._current = current
    try:
        with patch("app.main.run_restart", return_value=ActionResult.OK), \
             patch("app.main.verify_env_vars_bound", return_value=[
                 VerifyFailure("AC_FOO_ENABLE", "Foo.Enable", "silently dropped")
             ]):
            assert _run_apply_then_verify(state, lambda *_: None) == ActionResult.ERROR
        assert current.verify_failed[0].env_var == "AC_FOO_ENABLE"
    finally:
        main.runner._current = None


def _init_apply_state(tmp_path, *, admin_yml_content: str | None = None):
    """Common fixture setup for apply tests."""
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "worldserver.conf.dist").write_text(
        "#\n#    Foo.Enable\n#\n\nFoo.Enable = 1\n"
    )
    (dist / "playerbots.conf.dist").write_text("")
    # mod_ahbot.conf.dist must be present so AuctionHouseBot.GUIDs is in
    # the key index — exercises the blocklist, not the unknown-key path.
    (dist / "mod_ahbot.conf.dist").write_text(
        "#\n#    AuctionHouseBot.GUIDs\n#\n\nAuctionHouseBot.GUIDs = 0\n"
    )
    (dist / "individualProgression.conf.dist").write_text("")
    admin_yml = tmp_path / "docker-compose.admin.yml"
    admin_yml.write_text(
        admin_yml_content
        or "services:\n  ac-worldserver:\n    environment: {}\n"
    )
    override_yml = tmp_path / "docker-compose.override.yml"
    override_yml.write_text("services:\n  ac-worldserver:\n    environment: {}\n")
    configs = tmp_path / "configs"
    configs.mkdir()
    snapshots = tmp_path / "snapshots"
    snapshots.mkdir()
    init_state(
        dist_dir=dist,
        admin_yml=admin_yml,
        override_yml=override_yml,
        configs_dir=configs,
        snapshots_dir=snapshots,
    )
    return admin_yml, snapshots


@patch("app.main._run_apply_then_verify")
def test_apply_writes_admin_yml_and_returns_action_id(mock_apply_verify, tmp_path):
    """Apply is fire-and-forget: it writes admin.yml synchronously and
    returns an action id; the restart+verify runs in the background.
    The test patches the inner runner-fn so the test doesn't actually
    drive a restart."""
    from app.services.actions import ActionResult
    mock_apply_verify.return_value = ActionResult.OK

    admin_yml, _snapshots = _init_apply_state(tmp_path)
    client = TestClient(app)
    r = client.post("/api/settings/apply", json={"pending": {"Foo.Enable": "0"}})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "running"
    assert "id" in body
    contents = admin_yml.read_text()
    assert "AC_FOO_ENABLE: '0'" in contents or "AC_FOO_ENABLE: 0" in contents


@patch("app.main._run_apply_then_verify")
def test_apply_delete_removes_env_var(mock_apply_verify, tmp_path):
    """A pending empty-string value deletes the env var from admin.yml."""
    from app.services.actions import ActionResult
    mock_apply_verify.return_value = ActionResult.OK

    admin_yml, _ = _init_apply_state(
        tmp_path,
        admin_yml_content=(
            "services:\n  ac-worldserver:\n    environment:\n"
            "      AC_FOO_ENABLE: '0'\n"
        ),
    )
    client = TestClient(app)
    r = client.post("/api/settings/apply", json={"pending": {"Foo.Enable": ""}})
    assert r.status_code == 200
    contents = admin_yml.read_text()
    assert "AC_FOO_ENABLE" not in contents


@patch("app.main._run_apply_then_verify")
def test_apply_refuses_blocked_keys_without_writing(mock_apply_verify, tmp_path):
    """AuctionHouseBot.GUIDs is installer-managed. Server MUST refuse regardless
    of client-side hiding. admin.yml stays untouched."""
    from app.services.actions import ActionResult
    mock_apply_verify.return_value = ActionResult.OK

    admin_yml, _ = _init_apply_state(tmp_path)
    before = admin_yml.read_text()
    client = TestClient(app)
    r = client.post(
        "/api/settings/apply",
        json={"pending": {"AuctionHouseBot.GUIDs": "12345,12346"}},
    )
    assert r.status_code == 400
    assert "AuctionHouseBot.GUIDs" in r.json()["detail"]
    # File must be untouched — the blocklist check runs BEFORE write.
    assert admin_yml.read_text() == before
    # And no action should have been kicked off.
    assert not mock_apply_verify.called


@patch("app.main.runner")
@patch("app.main._run_apply_then_verify")
def test_apply_refuses_when_action_in_flight_without_writing(
    mock_apply_verify, mock_runner, tmp_path,
):
    """If another action is running, apply MUST 409 BEFORE touching admin.yml."""
    from app.services.actions import ActionResult
    mock_apply_verify.return_value = ActionResult.OK
    # Pretend an action is currently running.
    mock_runner.current.return_value = object()

    admin_yml, _ = _init_apply_state(tmp_path)
    before = admin_yml.read_text()
    client = TestClient(app)
    r = client.post("/api/settings/apply", json={"pending": {"Foo.Enable": "0"}})
    assert r.status_code == 409
    assert admin_yml.read_text() == before
    # No background work kicked off.
    assert not mock_runner.start.called


@patch("app.main._run_apply_then_verify")
def test_apply_rejects_malformed_typed_values_before_snapshot_or_write(mock_apply_verify, tmp_path):
    admin_yml, snapshots = _init_apply_state(tmp_path)
    before = admin_yml.read_text()
    client = TestClient(app)

    r = client.post("/api/settings/apply", json={"pending": {"Foo.Enable": "yes"}})

    assert r.status_code == 400
    assert "Foo.Enable" in r.json()["detail"]
    assert admin_yml.read_text() == before
    assert not list(snapshots.iterdir())
    assert not mock_apply_verify.called


class _InlineRunner:
    """Run a route action synchronously while preserving runner ordering.

    The production runner deliberately hands its action to a background task.
    This narrow adapter invokes the supplied pre-hook and action inline so
    endpoint tests can assert real filesystem state without a timing race.
    """

    def __init__(self):
        self._current = None
        self.calls = []
        self.records = []
        self.results = []

    def current(self):
        return self._current

    def start(self, name, func, *, pre=None):
        self.calls.append(name)
        if pre is not None:
            pre()
        record = SimpleNamespace(id="rollback-action", verify_failed=[])
        self._current = record
        self.records.append(record)
        try:
            self.results.append(func(lambda *_: None))
        finally:
            self._current = None
        return record


def _install_inline_runner(monkeypatch):
    import app.main as main

    runner = _InlineRunner()
    monkeypatch.setattr(main, "runner", runner)
    return runner


def _rollback_snapshot(snapshots, admin_yml, suffix, contents, *, mtime):
    snapshot = snapshots / f"{admin_yml.name}.bak.{suffix}"
    snapshot.write_text(contents)
    os.utime(snapshot, (mtime, mtime))
    return snapshot


def test_rollback_returns_404_when_no_snapshot_exists(tmp_path, monkeypatch):
    _init_apply_state(tmp_path)
    _install_inline_runner(monkeypatch)

    response = TestClient(app).post("/api/settings/rollback")

    assert response.status_code == 404
    assert response.json()["detail"] == "no admin.yml snapshots to roll back to"


def test_rollback_restores_latest_snapshot_byte_for_byte_and_keeps_forward_snapshot(
    tmp_path, monkeypatch,
):
    import app.main as main
    from app.services.actions import ActionResult

    live = (
        "# current operator-edited compose file\n"
        "services:\n  ac-worldserver:\n    environment:\n      AC_FOO_ENABLE: '1'\n"
    )
    older = "# old snapshot\nservices:\n  ac-worldserver:\n    environment: {}\n"
    newest = (
        "# chosen snapshot: comments and whitespace must survive exactly\n"
        "services:\n  ac-worldserver:\n    environment:\n      AC_FOO_ENABLE: '0'\n\n"
    )
    admin_yml, snapshots = _init_apply_state(tmp_path, admin_yml_content=live)
    old_snapshot = _rollback_snapshot(
        snapshots, admin_yml, "old", older, mtime=time.time() - 20,
    )
    latest_snapshot = _rollback_snapshot(
        snapshots, admin_yml, "newest", newest, mtime=time.time() - 10,
    )
    runner = _install_inline_runner(monkeypatch)
    monkeypatch.setattr(main, "run_restart", lambda **_kwargs: ActionResult.OK)
    monkeypatch.setattr(main, "verify_env_vars_bound", lambda *_args, **_kwargs: [])

    response = TestClient(app).post("/api/settings/rollback")

    assert response.status_code == 200
    assert response.json() == {
        "id": "rollback-action",
        "status": "running",
        "restored_from": latest_snapshot.name,
    }
    assert admin_yml.read_text() == newest
    assert runner.calls == ["rollback"]
    assert runner.results == [ActionResult.OK]
    forward = set(snapshots.iterdir()) - {old_snapshot, latest_snapshot}
    assert len(forward) == 1
    assert forward.pop().read_text() == live


def test_rollback_runner_race_returns_409_without_snapshot_or_write(tmp_path, monkeypatch):
    import app.main as main

    live = "services:\n  ac-worldserver:\n    environment:\n      AC_FOO_ENABLE: '1'\n"
    restore = "services:\n  ac-worldserver:\n    environment:\n      AC_FOO_ENABLE: '0'\n"
    admin_yml, snapshots = _init_apply_state(tmp_path, admin_yml_content=live)
    snapshot = _rollback_snapshot(
        snapshots, admin_yml, "candidate", restore, mtime=time.time(),
    )

    class _RaceRunner:
        def current(self):
            return None

        def start(self, *_args, **_kwargs):
            raise RuntimeError("another action already running")

    monkeypatch.setattr(main, "runner", _RaceRunner())
    response = TestClient(app).post("/api/settings/rollback")

    assert response.status_code == 409
    assert response.json()["detail"] == "another action already running"
    assert admin_yml.read_text() == live
    assert list(snapshots.iterdir()) == [snapshot]


def test_rollback_write_failure_leaves_live_file_and_returns_500(tmp_path, monkeypatch):
    live = "services:\n  ac-worldserver:\n    environment:\n      AC_FOO_ENABLE: '1'\n"
    restore = "services:\n  ac-worldserver:\n    environment:\n      AC_FOO_ENABLE: '0'\n"
    admin_yml, snapshots = _init_apply_state(tmp_path, admin_yml_content=live)
    snapshot = _rollback_snapshot(
        snapshots, admin_yml, "candidate", restore, mtime=time.time(),
    )
    _install_inline_runner(monkeypatch)
    original_open = Path.open

    def fail_live_write(path, mode="r", *args, **kwargs):
        if path == admin_yml and mode == "w":
            raise OSError("simulated full disk")
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_live_write)

    response = TestClient(app, raise_server_exceptions=False).post(
        "/api/settings/rollback",
    )

    assert response.status_code == 500
    assert admin_yml.read_text() == live
    forward = set(snapshots.iterdir()) - {snapshot}
    assert len(forward) == 1
    assert forward.pop().read_text() == live


def test_rollback_preserves_restart_timeout_result(tmp_path, monkeypatch):
    import app.main as main
    from app.services.actions import ActionResult

    admin_yml, snapshots = _init_apply_state(tmp_path)
    _rollback_snapshot(
        snapshots, admin_yml, "candidate", admin_yml.read_text(), mtime=time.time(),
    )
    runner = _install_inline_runner(monkeypatch)
    monkeypatch.setattr(main, "run_restart", lambda **_kwargs: ActionResult.TIMEOUT)
    verify = MagicMock(return_value=[])
    monkeypatch.setattr(main, "verify_env_vars_bound", verify)

    response = TestClient(app).post("/api/settings/rollback")

    assert response.status_code == 200
    assert runner.results == [ActionResult.TIMEOUT]
    verify.assert_not_called()


def test_rollback_records_post_verify_failure(tmp_path, monkeypatch):
    import app.main as main
    from app.services.actions import ActionResult, VerifyFailure

    admin_yml, snapshots = _init_apply_state(tmp_path)
    _rollback_snapshot(
        snapshots, admin_yml, admin_yml.name, admin_yml.read_text(), mtime=time.time(),
    )
    runner = _install_inline_runner(monkeypatch)
    failure = VerifyFailure("AC_FOO_ENABLE", "Foo.Enable", "silently dropped")
    monkeypatch.setattr(main, "run_restart", lambda **_kwargs: ActionResult.OK)
    monkeypatch.setattr(main, "verify_env_vars_bound", lambda *_args, **_kwargs: [failure])

    response = TestClient(app).post("/api/settings/rollback")

    assert response.status_code == 200
    assert runner.results == [ActionResult.ERROR]
    assert runner.records[-1].verify_failed == [failure]
