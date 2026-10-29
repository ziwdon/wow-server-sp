"""Backups tab: the Cloud backup panel, its routes, the summary card and the "In Drive" badges (spec 6.8, 6.9).

The service is the real ``CloudService`` on the fakes of ``test_cloud_service`` (fake vault, fake CLI,
fake ``auth login`` process), swapped in for ``cloud_backup.get_service`` so the routes run exactly
as they do live. Every route answers HTTP 200 with the re-rendered panel; errors are text in it.
"""

from __future__ import annotations

import html
import re
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.services import cloud_backup
from app.services.cloud_config import RemoteFile
from app.services.cloud_vault import UNLOCK_FAILED_MESSAGE, VaultError
from tests.test_cloud_service import D27, FOLDER, NEW, NOW, SECRET_WORDS, URL, Rig, iso, log_entry, outcome

ALL_SECRET_WORDS = (*SECRET_WORDS, "cachePassword")
BAD_RETENTION = "Keep-for days must be a whole number from 1 to 90."
NO_TPM_TEXT = (
    "Could not use this server's TPM security chip, which cloud backup needs to keep the Proton "
    "session encrypted."
)
DISCONNECTED_TEXT = (
    "Removed from this server. To end the session at Proton too, "
    "revoke it in your Proton account's session list."
)
STATE_UNSAVED_TEXT = "Could not save the cloud status file — check that the admin data folder is writable."
OTHER = "/my-files/Other"


@pytest.fixture(autouse=True)
def short_waits(monkeypatch):
    monkeypatch.setattr(cloud_backup, "POLL_SECONDS", 0.01)
    monkeypatch.setattr(cloud_backup, "FIRST_LINE_TIMEOUT_SECONDS", 5.0)
    monkeypatch.setattr(cloud_backup, "SIGN_IN_DEADLINE_SECONDS", 20.0)
    monkeypatch.setattr(cloud_backup, "SIGN_IN_SETUP_TIMEOUT_SECONDS", 10.0)
    monkeypatch.setattr(cloud_backup, "CANCEL_TERM_GRACE_SECONDS", 2.0)
    monkeypatch.setattr(cloud_backup, "CANCEL_JOIN_SECONDS", 5.0)
    monkeypatch.setattr(cloud_backup, "DISCONNECT_WAIT_SECONDS", 5.0)


@pytest.fixture
def rig(tmp_path, monkeypatch):
    base = tmp_path / "cloud-rig"
    base.mkdir()
    r = Rig(base)
    monkeypatch.setattr(cloud_backup, "get_service", lambda: r.svc)
    yield r
    r.close()


@pytest.fixture
def client(tmp_path, monkeypatch, rig):
    monkeypatch.setenv("AC_STACK_DIR", str(tmp_path))
    monkeypatch.setenv("ADMIN_DATA_DIR", str(tmp_path / "admin-data"))
    (tmp_path / "backups").mkdir()
    (tmp_path / "logs").mkdir()
    dist = tmp_path / "dist"
    dist.mkdir()
    for name in ("worldserver", "playerbots", "mod_ahbot", "individualProgression"):
        (dist / f"{name}.conf.dist").write_text("")
    from app.state import init_state

    init_state(
        dist_dir=Path("/app/dist") if Path("/app/dist").exists() else dist,
        admin_yml=tmp_path / "docker-compose.admin.yml",
        override_yml=tmp_path / "docker-compose.override.yml",
        configs_dir=tmp_path / "configs",
        snapshots_dir=tmp_path / "snap",
    )
    from app.main import app

    return TestClient(app)


def shown(response) -> str:
    """The response body as the browser shows it (entities decoded)."""
    assert response.status_code == 200
    return html.unescape(response.text)


def panel(client) -> str:
    return shown(client.get("/api/cloud/panel"))


def connect(client, rig) -> str:
    rig.cli.queue_login()
    return shown(client.post("/api/cloud/connect"))


def local_archive(tmp_path, name: str) -> None:
    (tmp_path / "backups" / name).write_bytes(b"x")


# --- the Backups page ---


def test_backups_page_has_cloud_panel_container(client):
    page = client.get("/backups").text

    assert 'hx-get="/api/cloud/panel"' in page
    assert 'id="cloud-panel"' in page
    # The container loads once; only the rendered panel may poll.
    assert 'hx-trigger="load"' in page[page.index('id="cloud-panel"'):]
    assert "every 5s" not in page


def test_activity_panel_sits_between_the_action_bar_and_the_cloud_panel(client):
    page = client.get("/backups").text

    assert page.index('class="action-bar"') < page.index('id="action-log"') < page.index('id="cloud-panel"')


def test_wait_indicator_makes_no_promise_about_how_long(client, rig):
    text = panel(client)

    assert "Working… this can take a minute." in text
    assert "30 seconds" not in text


# --- panel states ---


def _checking(rig):
    rig.svc = rig.service(probe=False)


def _unavailable(rig):
    rig.vault.tpm = False
    rig.svc.probe()


def _connected(rig):
    rig.configure(enabled=True)
    rig.set_state("connected", cli_version="0.9.0", folder_status="ok", last_success=iso(NOW.replace(hour=11)))


def _vault_error(rig):
    rig.configure(enabled=True)
    rig.set_state("vault_error", last_error=UNLOCK_FAILED_MESSAGE)


def _stalled(rig):
    rig.configure(enabled=True)
    rig.set_state(
        "connected", folder_status="ok", last_result="error", last_error="Proton said no (boom)",
        last_success=iso(NOW.replace(day=26)),
    )


def _conflicts(rig):
    rig.configure(enabled=True)
    rig.set_state(
        "connected", folder_status="ok", last_result="warning",
        issues=[{"name": NEW, "kind": "conflict", "detail": "A different file with this name is already there."}],
    )


def _failed(rig):
    rig.configure(enabled=True)
    rig.set_state(
        "connected", folder_status="ok", last_result="partial",
        issues=[{"name": D27, "kind": "failed", "detail": "Upload failed (disk quota)."}],
    )


def _folder_missing(rig):
    rig.configure(enabled=True)
    rig.set_state("connected", folder_status="missing")


def _folder_is_a_file(rig):
    rig.configure(enabled=True)
    rig.set_state("connected", folder_status="not_folder")


def _no_folder(rig):
    rig.set_state("connected")


def _upload_buttons(rig):
    _connected(rig)


STATE_CASES = {
    "checking": (
        _checking,
        ["Checking"],
        ['hx-post="/api/cloud/connect"', 'hx-post="/api/cloud/upload-now"'],
    ),
    "unavailable": (
        _unavailable,
        ["Not available", NO_TPM_TEXT],
        ['hx-post="/api/cloud/connect"', 'id="cloud-settings-form"', "no usable TPM"],
    ),
    "disconnected": (
        lambda rig: None,
        ["Not connected", 'hx-post="/api/cloud/connect"'],
        ['hx-post="/api/cloud/disconnect"', 'hx-post="/api/cloud/upload-now"', "Waiting for sign-in"],
    ),
    "connected": (
        _connected,
        [
            "Connected", "0.9.0", "2026-10-28 11:00 UTC",
            'hx-post="/api/cloud/disconnect"', 'hx-post="/api/cloud/upload-now"',
        ],
        ['hx-post="/api/cloud/connect"', "Stalled", "Not connected", 'hx-post="/api/cloud/create-folder"'],
    ),
    "vault_error": (
        _vault_error,
        ['hx-post="/api/cloud/connect"', 'hx-post="/api/cloud/disconnect"'],
        ['hx-post="/api/cloud/upload-now"', "Stalled"],
    ),
    "stalled": (
        _stalled,
        ["Stalled — no fully successful cloud run for over a day.", "Proton said no (boom)"],
        ["no successful upload"],
    ),
    "conflicts": (
        _conflicts,
        [NEW, "Move that copy to the Drive trash yourself", "cloud-conflict"],
        ["Stalled"],
    ),
    "failed": (
        _failed,
        [D27, "Upload failed (disk quota).", "cloud-failed"],
        ["cloud-conflict"],
    ),
    "folder_missing": (
        _folder_missing,
        ['hx-post="/api/cloud/create-folder"', FOLDER],
        ['hx-post="/api/cloud/upload-now"'],
    ),
    "folder_is_a_file": (
        _folder_is_a_file,
        ["is a file, not a folder"],
        ['hx-post="/api/cloud/create-folder"', 'hx-post="/api/cloud/upload-now"'],
    ),
    "no_folder": (
        _no_folder,
        ["Choose a Drive folder"],
        ['hx-post="/api/cloud/upload-now"', 'hx-post="/api/cloud/create-folder"'],
    ),
}


@pytest.mark.parametrize("case", list(STATE_CASES))
def test_panel_states_render(client, rig, case):
    setup, must, must_not = STATE_CASES[case]
    setup(rig)

    text = panel(client)

    for needle in must:
        assert needle in text, f"{case}: expected {needle!r}"
    for needle in must_not:
        assert needle not in text, f"{case}: did not expect {needle!r}"
    assert 'id="cloud-panel"' in text


def test_vault_error_message_is_shown_once(client, rig):
    _vault_error(rig)

    assert panel(client).count("Can't unlock the stored session") == 1


def test_pending_panel_shows_the_link_with_copy_button(client, rig):
    text = connect(client, rig)

    assert "Waiting for sign-in" in text
    link = re.search(r'<a [^>]*href="([^"]+)"[^>]*>', text)
    assert link and link.group(1) == URL
    assert 'target="_blank"' in link.group(0)
    assert "noopener" in link.group(0)
    assert f'data-copy="{URL}"' in text
    assert "Copy link" in text
    assert "expires in 10 minutes" in text
    assert 'hx-post="/api/cloud/connect/cancel"' in text
    assert 'hx-post="/api/cloud/connect"' not in text
    # Reading the panel again (the poll) shows the same pending sign-in.
    assert URL in panel(client)


def test_sign_in_link_is_escaped(client, rig):
    # The service only hands out plain Proton links; the template must not rely on that.
    sentinel = 'https://account.proton.me/auth?x="><script>alert(1)</script>'
    rig.svc._signin = cloud_backup._SignIn(url=sentinel)
    try:
        raw = client.get("/api/cloud/panel").text
    finally:
        rig.svc._signin = None

    assert "<script>alert(1)</script>" not in raw
    assert "&lt;script&gt;" in raw


def test_pending_panel_polls_and_idle_panel_does_not(client, rig):
    idle = client.get("/api/cloud/panel").text
    assert "every 5s" not in idle
    assert "hx-trigger" not in idle.split('<div class="panel-header">')[0]

    pending = html.unescape(connect(client, rig))
    root = pending[: pending.index(">", pending.index('id="cloud-panel"'))]
    assert 'hx-get="/api/cloud/panel"' in root
    assert 'hx-trigger="every 5s"' in root
    assert 'hx-swap="outerHTML"' in root


def test_busy_panel_polls_and_disables_its_buttons(client, rig):
    _connected(rig)
    rig.svc._acquire("run")
    try:
        text = client.get("/api/cloud/panel").text
    finally:
        rig.svc._release()

    assert 'hx-trigger="every 5s"' in text
    assert re.search(r'<button[^>]*hx-post="/api/cloud/upload-now"[^>]*\bdisabled\b', text)
    assert "Working on Proton Drive" in text
    # Once idle again the panel stops polling.
    assert "every 5s" not in client.get("/api/cloud/panel").text


def test_checking_panel_polls_until_the_probe_has_an_answer(client, rig):
    _checking(rig)

    checking = client.get("/api/cloud/panel").text

    root = checking[: checking.index(">", checking.index('id="cloud-panel"'))]
    assert "Checking" in checking
    assert 'hx-get="/api/cloud/panel"' in root and 'hx-trigger="every 5s"' in root
    rig.svc.probe()
    resolved = client.get("/api/cloud/panel").text
    assert "Checking" not in resolved
    assert "every 5s" not in resolved


def test_recent_runs_are_listed_newest_first_and_capped_at_ten(client, rig):
    _connected(rig)  # last success 2026-10-28 11:00: a stamp that is not in any run below
    for day in range(1, 13):
        rig.store.append_log(log_entry(iso(NOW.replace(day=day))))

    text = panel(client)

    assert text.count("cloud-run-row") == 10
    stamps = [f"2026-10-{day:02d} 12:00 UTC" for day in range(12, 2, -1)]  # the 10 newest, newest first
    assert all(text.count(stamp) == 1 for stamp in stamps)
    positions = [text.index(stamp) for stamp in stamps]
    assert positions == sorted(positions)
    assert "2026-10-02 12:00 UTC" not in text and "2026-10-01 12:00 UTC" not in text


def test_recent_runs_show_result_counts_and_error(client, rig):
    _connected(rig)
    rig.store.append_log(
        type(log_entry("x"))(
            started=iso(NOW), finished=iso(NOW), trigger="manual", result="partial",
            uploaded=[NEW], conflicts=[], failed=[D27, "b"], trashed=[], error="Upload failed.",
        )
    )

    text = panel(client)

    assert "cloud-run-bad" in text
    assert "1 uploaded" in text
    assert "2 failed" in text
    assert "Upload failed." in text
    assert "manual" in text


def test_no_recent_runs_says_so(client, rig):
    assert "No cloud runs yet." in panel(client)


def test_help_text_and_disconnect_advice_are_shown(client, rig):
    text = panel(client)

    assert "Checks for new backups every 10 minutes and uploads only what's new." in text
    assert "Drive trash" in text
    assert "revoke it in your Proton account's session list" in text


# --- settings ---


@pytest.mark.parametrize(
    "fields, message",
    [
        ({"folder": "/my-files"}, "That is not a usable Drive folder"),
        ({"enabled": "1", "folder": ""}, "Choose a Proton Drive folder"),
        ({"folder": FOLDER, "retention_days": "0"}, BAD_RETENTION),
        ({"folder": FOLDER, "retention_days": "91"}, BAD_RETENTION),
        ({"folder": FOLDER, "retention_days": "abc"}, BAD_RETENTION),
        ({"folder": FOLDER, "retention_days": ""}, BAD_RETENTION),
        ({"folder": FOLDER, "retention_days": "1_0"}, BAD_RETENTION),
        ({"folder": FOLDER, "labels": ["daily", "bogus"]}, "Unknown backup kind"),
    ],
)
def test_settings_validation_error_is_shown_and_not_saved(client, rig, fields, message):
    rig.configure(enabled=False, retention_days=9)
    before = rig.store.config_path.read_bytes()
    form = {"retention_days": "14", **fields}

    response = client.post("/api/cloud/settings", data=form)

    text = shown(response)
    assert message in text
    assert re.search(r'role="alert"[^>]*>[^<]*' + re.escape(message), text)
    assert "Settings saved." not in text
    assert rig.store.config_path.read_bytes() == before
    # What was typed stays in the form.
    assert f'value="{html.escape(form["folder"])}"' in response.text or form["folder"] == ""


def test_settings_save_round_trip(client, rig):
    response = client.post(
        "/api/cloud/settings",
        data={
            "enabled": "1",
            "folder": "  /my-files/WoW Backups/  ",
            "labels": ["manual", "daily"],
            "retention_days": "14",
        },
    )

    text = shown(response)
    assert "Settings saved." in text
    cfg = rig.store.load_config()
    assert (cfg.enabled, cfg.folder, cfg.labels, cfg.retention_days) == (True, FOLDER, ("daily", "manual"), 14)
    # The form shows what was saved, and so does a fresh read of the panel.
    for page in (text, panel(client)):
        assert f'value="{FOLDER}"' in page
        assert re.search(r'name="enabled"[^>]*\bchecked\b', page)
        assert re.search(r'name="labels" value="daily"[^>]*\bchecked\b', page)
        assert re.search(r'name="labels" value="manual"[^>]*\bchecked\b', page)
        assert not re.search(r'name="labels" value="preclear"[^>]*\bchecked\b', page)
        assert 'name="retention_days"' in page and 'value="14"' in page


def test_settings_without_enabled_box_turns_it_off(client, rig):
    rig.configure(enabled=True)

    client.post("/api/cloud/settings", data={"folder": FOLDER, "labels": ["daily"], "retention_days": "7"})

    assert rig.store.load_config().enabled is False


def test_settings_save_checks_the_folder_when_connected(client, rig):
    rig.set_state("connected", folder_status="missing")

    text = shown(client.post("/api/cloud/settings", data={"folder": FOLDER, "retention_days": "7"}))

    assert rig.cli.count("info") == 1
    assert rig.store.load_state().folder_status == "ok"
    assert 'hx-post="/api/cloud/create-folder"' not in text


def test_settings_save_when_not_connected_makes_no_cli_call(client, rig):
    text = shown(client.post("/api/cloud/settings", data={"folder": FOLDER, "retention_days": "7"}))

    assert "Settings saved." in text
    assert rig.cli.calls == []


def test_settings_save_failure_keeps_the_form_values(client, rig, monkeypatch):
    def refuse(cfg):
        raise OSError("read-only file system")

    monkeypatch.setattr(rig.svc, "save_settings", refuse)

    response = client.post("/api/cloud/settings", data={"folder": FOLDER, "retention_days": "21"})

    text = shown(response)
    assert "Could not save the settings (the admin data folder is not writable)." in text
    assert "read-only" not in text
    assert f'value="{FOLDER}"' in text and 'value="21"' in text


def test_polling_read_keeps_the_form_the_user_is_typing_in(client, rig):
    # htmx keeps the live form when the poll's form carries hx-preserve; action responses do not.
    polled = client.get("/api/cloud/panel").text
    saved = client.post("/api/cloud/settings", data={"folder": FOLDER, "retention_days": "7"}).text

    assert re.search(r'<form[^>]*hx-preserve[^>]*>', polled)
    assert 'id="cloud-settings-form"' in polled
    assert "hx-preserve" not in saved


# --- actions ---


def test_connect_then_cancel(client, rig):
    connect(client, rig)

    text = shown(client.post("/api/cloud/connect/cancel"))

    assert "Not connected" in text
    assert "Waiting for sign-in" not in text
    assert rig.svc.wait_idle(5)
    assert "every 5s" not in text


def test_connect_failure_is_shown_in_the_panel(client, rig):
    text = shown(client.post("/api/cloud/connect"))  # nothing queued: the fake CLI cannot start a login

    assert 'role="alert"' in text
    assert "Waiting for sign-in" not in text
    assert "Traceback" not in text


def test_connect_unavailable_shows_the_reason(client, rig):
    _unavailable(rig)

    text = shown(client.post("/api/cloud/connect"))

    assert NO_TPM_TEXT in text


@pytest.mark.parametrize(
    "connection, stored, offered",
    [
        ("connected", True, True),
        ("connected", False, True),
        ("vault_error", False, True),
        ("disconnected", True, True),
        ("disconnected", False, False),
    ],
)
def test_unavailable_panel_still_offers_disconnect_when_there_is_something_to_remove(
    client, rig, connection, stored, offered
):
    # Spec 6.9: Disconnect works even when the vault cannot be unsealed (M-1).
    _unavailable(rig)
    rig.set_state(connection)
    rig.vault.set_exists(stored)

    text = panel(client)

    assert "Not available" in text
    assert ('hx-post="/api/cloud/disconnect"' in text) is offered
    assert 'hx-post="/api/cloud/connect"' not in text and 'hx-post="/api/cloud/upload-now"' not in text
    if offered:
        button = re.search(r'<button[^>]*hx-post="/api/cloud/disconnect"[^>]*>', text).group(0)
        assert "hx-confirm=" in button


def test_disconnect_works_while_cloud_backup_is_unavailable(client, rig):
    _unavailable(rig)
    rig.set_state("connected")
    rig.vault.unlock_errors.append(VaultError(UNLOCK_FAILED_MESSAGE))  # the TPM cannot unseal it

    text = shown(client.post("/api/cloud/disconnect"))

    assert DISCONNECTED_TEXT in text
    assert "wipe" in rig.vault.calls and rig.store.load_state().connection == "disconnected"
    assert 'hx-post="/api/cloud/disconnect"' not in text  # nothing left to remove


def test_disconnect_removes_the_session_and_says_how_to_end_it_at_proton(client, rig):
    _connected(rig)

    text = shown(client.post("/api/cloud/disconnect"))

    assert (
        "Removed from this server. To end the session at Proton too, "
        "revoke it in your Proton account's session list."
    ) in text
    assert "Not connected" in text
    assert "wipe" in rig.vault.calls
    assert rig.store.load_state().connection == "disconnected"
    assert rig.store.load_config().folder == FOLDER  # settings survive


def test_create_folder_creates_it_and_the_button_goes_away(client, rig):
    _folder_missing(rig)

    text = shown(client.post("/api/cloud/create-folder"))

    assert "Folder created." in text
    assert rig.cli.count("create_folder") == 1
    assert 'hx-post="/api/cloud/create-folder"' not in text
    assert rig.store.load_state().folder_status == "ok"


def test_create_folder_when_not_connected_is_refused_in_the_panel(client, rig):
    text = shown(client.post("/api/cloud/create-folder"))

    assert cloud_backup.NOT_CONNECTED in text
    assert rig.cli.count("create_folder") == 0


def test_disconnect_and_create_folder_require_confirmation(client, rig):
    _folder_missing(rig)
    text = panel(client)

    disconnect = re.search(r'<button[^>]*hx-post="/api/cloud/disconnect"[^>]*>', text).group(0)
    create = re.search(r'<button[^>]*hx-post="/api/cloud/create-folder"[^>]*>', text).group(0)
    assert "hx-confirm=" in disconnect
    assert "Proton" in disconnect  # says where the session is really ended
    assert "hx-confirm=" in create
    assert FOLDER in create  # names the full path
    # Plain actions need no confirmation.
    _connected(rig)
    upload = re.search(r'<button[^>]*hx-post="/api/cloud/upload-now"[^>]*>', panel(client)).group(0)
    assert "hx-confirm" not in upload


def test_upload_now_starts_a_run_and_the_panel_follows_it(client, rig, monkeypatch):
    _connected(rig)
    gate = threading.Event()
    calls = []

    def gated(**kw):
        calls.append(kw)
        assert gate.wait(5), "the test never opened the gate"
        return outcome("ok", uploaded=[NEW], remote=[RemoteFile(NEW, 3, "aa")])

    monkeypatch.setattr(cloud_backup, "run_sync", gated)
    try:
        started = shown(client.post("/api/cloud/upload-now"))
        assert "Upload started." in started
        assert "every 5s" in started  # busy: the panel polls
        assert "Working on Proton Drive" in started
    finally:
        gate.set()
    assert rig.svc.wait_idle(5)

    done = panel(client)
    assert "every 5s" not in done
    assert "cloud-run-ok" in done
    assert "1 uploaded" in done
    assert len(calls) == 1


def test_upload_now_when_not_connected_is_refused_in_the_panel(client, rig):
    text = shown(client.post("/api/cloud/upload-now"))

    assert cloud_backup.NOT_CONNECTED in text


def test_busy_actions_show_busy_message(client, rig):
    _connected(rig)
    rig.svc._acquire("run")
    try:
        for route in ("upload-now", "create-folder", "disconnect"):
            text = shown(client.post(f"/api/cloud/{route}"))
            assert cloud_backup.BUSY in text, route
            assert 'role="alert"' in text, route
        rig.set_state("disconnected")  # a connected server refuses a new sign-in before it looks at the lock
        text = shown(client.post("/api/cloud/connect"))
        assert cloud_backup.BUSY in text and 'role="alert"' in text
        _connected(rig)
        # Saving settings still works; only the follow-up folder check is skipped.
        saved = shown(client.post("/api/cloud/settings", data={"folder": FOLDER, "retention_days": "7"}))
        assert "Settings saved." in saved
        assert cloud_backup.BUSY not in saved
    finally:
        rig.svc._release()


@pytest.mark.parametrize("holder", ["run", "check", "create"])
def test_settings_cannot_move_the_folder_while_a_drive_task_uses_it(client, rig, holder):
    # A run, folder check or folder creation keeps using the folder it started with (I-1).
    _connected(rig)
    before = rig.store.config_path.read_bytes()
    rig.svc._acquire(holder)
    try:
        text = shown(
            client.post("/api/cloud/settings", data={"enabled": "1", "folder": OTHER, "retention_days": "9"})
        )
    finally:
        rig.svc._release()

    assert cloud_backup.BUSY in text and re.search(r'role="alert"[^>]*>' + re.escape(cloud_backup.BUSY), text)
    assert "Settings saved." not in text
    assert f'value="{OTHER}"' in text and 'value="9"' in text  # what was typed stays in the form
    assert rig.store.config_path.read_bytes() == before
    assert rig.cli.calls == []


def test_unexpected_errors_are_logged_not_echoed(client, rig, monkeypatch, caplog):
    _connected(rig)

    def boom(trigger):
        raise RuntimeError("kaboom userKeyPassword=hunter2")

    monkeypatch.setattr(rig.svc, "start_run", boom)

    text = shown(client.post("/api/cloud/upload-now"))

    assert "Something went wrong — see the admin log." in text
    assert "kaboom" not in text and "hunter2" not in text and "RuntimeError" not in text
    assert any(record.levelname == "ERROR" and record.exc_info for record in caplog.records)


def test_panel_warns_while_the_cloud_status_file_cannot_be_saved(client, rig, monkeypatch):
    _connected(rig)
    real_save = rig.store.save_state

    def read_only(state):
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(rig.store, "save_state", read_only)
    rig.svc.check_folder()  # any state write; this one fails

    warned = panel(client)
    assert re.search(r'class="cloud-msg cloud-msg-error[^"]*"[^>]*>' + re.escape(STATE_UNSAVED_TEXT), warned)
    assert "Read-only file system" not in warned

    monkeypatch.setattr(rig.store, "save_state", real_save)
    rig.svc.check_folder()  # a later write works: the warning goes away
    assert STATE_UNSAVED_TEXT not in panel(client)


def test_state_changing_cloud_routes_wait_for_their_worker_thread(client, rig, monkeypatch):
    # CLAUDE.md: a cancellable mutation on a worker thread goes through _await_thread_completion (M-5).
    import app.main as main

    seen: list[str] = []
    real = main._await_thread_completion

    async def spy(func, /, *args):
        seen.append(func.__name__)
        return await real(func, *args)

    monkeypatch.setattr(main, "_await_thread_completion", spy)
    monkeypatch.setattr(cloud_backup, "run_sync", lambda **kw: outcome("ok"))
    _folder_missing(rig)

    routes = {
        "connect": ["start_sign_in"],
        "connect/cancel": ["cancel_sign_in"],
        "upload-now": ["start_run"],
        "create-folder": ["create_folder"],
        "settings": ["save_settings", "check_folder"],
        "disconnect": ["disconnect"],
    }
    for route, methods in routes.items():
        seen.clear()
        data = {"folder": FOLDER, "retention_days": "7"} if route == "settings" else None
        shown(client.post(f"/api/cloud/{route}", data=data))
        assert rig.svc.wait_idle(5)
        assert seen == methods, route

    seen.clear()
    panel(client)
    assert seen == []  # reading the panel changes nothing


def test_status_failure_shows_a_plain_message(client, rig, monkeypatch):
    def boom():
        raise RuntimeError("state exploded")

    monkeypatch.setattr(rig.svc, "status", boom)

    text = panel(client)

    assert "status could not be read" in text
    assert "state exploded" not in text


# --- summary card and list badges ---


def _card(client) -> str:
    text = shown(client.get("/api/backups/summary"))
    return next(part for part in text.split('<div class="stat-card">') if "Cloud copies" in part)


def _card_disabled(rig):
    rig.configure(enabled=False)
    rig.set_state("connected", remote=[RemoteFile(NEW, 1, None)])


def _card_ok(rig):
    rig.configure(enabled=True)
    rig.set_state(
        "connected", last_result="ok", last_success=iso(NOW.replace(hour=11)),
        remote=[RemoteFile(NEW, 1, None), RemoteFile(D27, 1, None)],
    )


def _card_none_yet(rig):
    rig.configure(enabled=True)
    rig.set_state("connected")


def _card_stalled(rig):
    _stalled(rig)


def _card_failed(rig):
    rig.configure(enabled=True)
    rig.set_state("connected", last_result="error", last_error="network down", last_success=iso(NOW.replace(hour=11)))


CARD_CASES = {
    "off": (_card_disabled, "Off", None, "cloud-muted"),
    "ok": (_card_ok, "2026-10-28 11:00 UTC", "2 in Drive", None),
    "none_yet": (_card_none_yet, "No uploads yet", "0 in Drive", "cloud-muted"),
    "disconnected": (lambda rig: rig.configure(enabled=True), "Not connected", None, "cloud-bad"),
    "vault_error": (_vault_error, "Can't unlock", None, "cloud-bad"),
    "unavailable": (lambda rig: (rig.configure(enabled=True), _unavailable(rig)), "Unavailable", None, "cloud-bad"),
    "stalled": (_card_stalled, "Stalled", "No fully successful cloud run for over a day", "cloud-bad"),
    "failed": (_card_failed, "Last upload failed", None, "cloud-bad"),
}


@pytest.mark.parametrize("case", list(CARD_CASES))
def test_summary_card(client, rig, case):
    setup, value, sub, tone = CARD_CASES[case]
    setup(rig)

    card = _card(client)

    assert "Cloud copies" in card
    assert value in card, card
    if sub:
        assert sub in card
    if tone:
        assert tone in card
    else:
        assert "cloud-bad" not in card and "cloud-muted" not in card
    # The other three cards are unchanged.
    summary = shown(client.get("/api/backups/summary"))
    for label in ("Last Backup", "Available Backups", "Disk Used"):
        assert label in summary


def test_summary_card_and_in_drive_badges(client, rig, tmp_path):
    local_archive(tmp_path, NEW)
    local_archive(tmp_path, D27)
    rig.configure(enabled=True)
    rig.set_state("connected", last_result="ok", last_success=iso(NOW), remote=[RemoteFile(NEW, 1, None)])

    listing = shown(client.get("/api/backups/list"))

    rows = [part for part in listing.split("<li ")[1:]]
    assert len(rows) == 2
    with_badge = [row for row in rows if "In Drive" in row]
    assert len(with_badge) == 1
    assert f'data-archive="{NEW}"' in with_badge[0]
    assert 'class="backup-badge badge-in-drive"' in with_badge[0]
    assert listing.count("badge-in-drive") == 1


def test_an_archive_with_a_conflict_gets_no_in_drive_badge(client, rig, tmp_path):
    # The Drive holds a different file under that name, so this archive is not in the Drive (I-2).
    local_archive(tmp_path, NEW)
    local_archive(tmp_path, D27)
    rig.configure(enabled=True)
    rig.set_state(
        "connected", folder_status="ok", last_result="warning", last_success=iso(NOW),
        remote=[RemoteFile(NEW, 1, None), RemoteFile(D27, 1, None)],
        issues=[{"name": NEW, "kind": "conflict", "detail": "A different file with this name is already there."}],
    )

    listing = shown(client.get("/api/backups/list"))

    rows = {name: row for row in listing.split("<li ")[1:] for name in (NEW, D27) if f'data-archive="{name}"' in row}
    assert "In Drive" not in rows[NEW]
    assert "In Drive" in rows[D27]
    assert NEW in panel(client)  # the panel names the conflict instead


def test_disclaimer_mentions_the_drive_folder_only_when_enabled(client, rig, tmp_path):
    local_archive(tmp_path, NEW)
    rig.configure(enabled=False)

    off = shown(client.get("/api/backups/list"))
    assert "Backups are automatically deleted after 7 days" in off
    assert "Proton Drive" not in off

    rig.configure(enabled=True)
    on = client.get("/api/backups/list").text

    assert "Backups are automatically deleted after 7 days" in html.unescape(on)
    assert f"<code>{FOLDER}</code>" in on
    assert (
        "Copies also go to Proton Drive folder"
    ) in on
    assert "and leave it on the same schedule; old copies go to the Drive trash, which you empty yourself." in on


@pytest.mark.parametrize("days, kept", [(14, "and are kept there for 14 days;"), (1, "and are kept there for 1 day;")])
def test_disclaimer_names_the_cloud_retention_when_it_differs_from_seven_days(client, rig, tmp_path, days, kept):
    local_archive(tmp_path, NEW)
    rig.configure(enabled=True, retention_days=days)

    text = shown(client.get("/api/backups/list"))

    assert "Backups are automatically deleted after 7 days" in text
    assert f"Copies also go to Proton Drive folder <code>{FOLDER}</code> {kept} old copies go to the Drive trash" in text
    assert "same schedule" not in text


def test_disclaimer_folder_is_escaped(client, rig, tmp_path):
    rig.configure(enabled=True, folder="/my-files/<b>x</b> & y")

    text = client.get("/api/backups/list").text

    assert "<b>x</b>" not in text
    assert "&lt;b&gt;x&lt;/b&gt; &amp; y" in text


OUT_OF_RANGE = "0001-01-01T00:00:00+14:00"  # valid ISO text, but year 0 in UTC: astimezone() overflows


def test_utc_stamp_returns_unconvertible_text_as_is():
    from app.main import _utc_stamp

    assert _utc_stamp(OUT_OF_RANGE) == OUT_OF_RANGE
    assert _utc_stamp("not a time") == "not a time"
    assert _utc_stamp(None) == ""
    assert _utc_stamp("2026-10-28T12:00:00+02:00") == "2026-10-28 10:00 UTC"


def test_out_of_range_timestamps_never_break_the_pages(client, rig, tmp_path):
    local_archive(tmp_path, NEW)
    rig.configure(enabled=True)
    rig.set_state(
        "connected", folder_status="ok", last_result="ok", last_success=OUT_OF_RANGE,
        remote=[RemoteFile(NEW, 1, None)],
    )
    rig.store.append_log(log_entry(OUT_OF_RANGE))

    summary = client.get("/api/backups/summary")
    listing = client.get("/api/backups/list")
    cloud = client.get("/api/cloud/panel")

    assert summary.status_code == listing.status_code == cloud.status_code == 200
    assert "Cloud copies" in summary.text and OUT_OF_RANGE in summary.text
    assert "Available Backups" in summary.text
    assert OUT_OF_RANGE in cloud.text  # shown as text, in the "last upload" line and the runs list


def test_a_failing_cloud_card_never_breaks_the_local_summary(client, rig, monkeypatch):
    import app.main as main

    def boom(view):
        raise RuntimeError("card exploded")

    monkeypatch.setattr(main, "_cloud_card", boom)

    summary = client.get("/api/backups/summary")

    assert summary.status_code == 200
    assert "Available Backups" in summary.text and "Disk Used" in summary.text
    assert "Cloud copies" not in summary.text
    assert "card exploded" not in summary.text


def test_partials_survive_a_status_failure(client, rig, tmp_path, monkeypatch):
    local_archive(tmp_path, NEW)

    def boom():
        raise RuntimeError("no cloud today userKeyPassword")

    monkeypatch.setattr(cloud_backup, "get_service", boom)

    summary = client.get("/api/backups/summary")
    listing = client.get("/api/backups/list")

    assert summary.status_code == listing.status_code == 200
    assert "Cloud copies" not in summary.text
    assert "Available Backups" in summary.text
    assert NEW in listing.text
    assert "In Drive" not in listing.text
    assert "userKeyPassword" not in summary.text + listing.text


# --- secrets ---


def test_no_route_exposes_session_material(client, rig, tmp_path):
    local_archive(tmp_path, NEW)
    rig.configure(enabled=True)
    # Older or hand-edited files may still carry text that names a session field.
    rig.set_state(
        "connected",
        folder_status="ok",
        last_result="partial",
        last_error="login said userKeyPassword=abc",
        issues=[
            {"name": NEW, "kind": "failed", "detail": "accessToken leaked here"},
            {"name": D27, "kind": "conflict", "detail": "refreshToken too"},
        ],
        remote=[RemoteFile(NEW, 1, None)],
    )
    rig.store.append_log(
        type(log_entry("x"))(
            started=iso(NOW), finished=iso(NOW), trigger="auto", result="error",
            uploaded=[], conflicts=[], failed=[], trashed=[], error="cachePassword in a message",
        )
    )

    responses = [
        client.get("/api/cloud/panel"),
        client.get("/api/backups/summary"),
        client.get("/api/backups/list"),
        client.post("/api/cloud/settings", data={"folder": FOLDER, "retention_days": "7"}),
        client.post("/api/cloud/create-folder"),
        client.post("/api/cloud/upload-now"),
        client.post("/api/cloud/connect/cancel"),
        client.post("/api/cloud/connect"),
        client.post("/api/cloud/disconnect"),
        client.get("/backups"),
    ]

    assert rig.svc.wait_idle(5)
    for response in responses:
        assert response.status_code == 200
        lowered = html.unescape(response.text).lower()
        for word in ALL_SECRET_WORDS:
            assert word.lower() not in lowered, (response.request.url, word)


# --- lifespan ---


def test_lifespan_starts_and_stops_the_cloud_service_before_the_other_tasks(client, monkeypatch):
    from app.main import app
    from app.services import presence as presence_svc

    events: list[str] = []

    class Stub:
        async def start(self):
            events.append("cloud-start")

        async def stop(self):
            events.append("cloud-stop")

    stub = Stub()
    monkeypatch.setattr(cloud_backup, "get_service", lambda: stub)
    real_stop = presence_svc.PresenceAnnouncer.stop

    async def recording_stop(self):
        events.append("presence-stop")
        await real_stop(self)

    monkeypatch.setattr(presence_svc.PresenceAnnouncer, "stop", recording_stop)

    with TestClient(app):
        assert app.state.cloud_backup is stub
        assert events == ["cloud-start"]

    assert events == ["cloud-start", "cloud-stop", "presence-stop"]


def test_lifespan_still_stops_the_other_tasks_when_the_cloud_stop_fails(client, monkeypatch):
    from app.main import app
    from app.services import presence as presence_svc

    events: list[str] = []

    class Stub:
        async def start(self):
            pass

        async def stop(self):
            raise RuntimeError("stop failed")

    monkeypatch.setattr(cloud_backup, "get_service", lambda: Stub())
    real_stop = presence_svc.PresenceAnnouncer.stop

    async def recording_stop(self):
        events.append("presence-stop")
        await real_stop(self)

    monkeypatch.setattr(presence_svc.PresenceAnnouncer, "stop", recording_stop)

    with TestClient(app):
        pass

    assert events == ["presence-stop"]
