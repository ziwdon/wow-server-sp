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
