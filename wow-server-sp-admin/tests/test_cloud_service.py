"""CloudService (spec 6.1, 6.5-6.7, 6.9): the cloud lock, sign-in, runs, the loop and the status view.

Fakes: ``FakeVault`` records create/unlock/wipe and can refuse to unlock;
``ServiceCli`` is the Task 5 in-memory ``FakeCli`` plus sign-in, version,
create-folder and logout; ``FakePopen`` stands in for the ``auth login`` process.
Its stdout and stderr are real pipes the test writes into, so the service's
``selectors`` read and its stderr drain run for real. Its pid is far above the
Linux pid ceiling (2**22) and every signal goes through an injected ``killpg``
that only knows FakePopens, so no real process is ever signalled. The waits are
module constants, shortened per test; every test ends with its threads joined.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import itertools
import json
import os
import shutil
import signal
import subprocess
import threading
from contextlib import contextmanager, suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.services import cloud_backup
from app.services.cloud_backup import (
    CloudActionError,
    CloudBusy,
    CloudService,
    CloudStatusView,
    CloudUnavailable,
)
from app.services.cloud_cli import CliPaths, CloudCliError
from app.services.cloud_config import CloudConfig, CloudLogEntry, CloudState, CloudStore, RemoteFile
from app.services.cloud_plan import RemoteEntry
from app.services.cloud_sync import RunOutcome
from app.services.cloud_vault import UNLOCK_FAILED_MESSAGE, VaultError
from tests.test_cloud_sync import FakeCli

FOLDER = "/my-files/WoW Backups"
URL = "https://account.proton.me/auth/fork?selector=abc123"
NOW = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)
SECRET_WORDS = ("userKeyPassword", "accessToken", "refreshToken")
CONFLICT_DETAIL = (
    "A different file with this name is already in the Drive folder. Move that copy to the "
    "Drive trash yourself if you want this server's version uploaded."
)


def daily(day: str) -> str:
    return f"azerothcore-backup-daily-{day}.tar.gz"


NEW = daily("2026-10-28")
D27 = daily("2026-10-27")
D26 = daily("2026-10-26")


def iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def url_line(url: str = URL) -> str:
    return json.dumps({"signInUrl": url}) + "\n"


def outcome(result: str = "ok", **fields) -> RunOutcome:
    base = dict(
        result=result, connection="connected", folder_status="ok", uploaded=[], conflicts=[],
        failed=[], trashed=[], remote=[], error=None, failure_details={}, listed=True,
    )
    base.update(fields)
    return RunOutcome(**base)


def log_entry(started: str, result: str = "ok") -> CloudLogEntry:
    return CloudLogEntry(
        started=started, finished=started, trigger="auto", result=result,
        uploaded=[], conflicts=[], failed=[], trashed=[], error=None,
    )


def locked(svc: CloudService) -> bool:
    return svc._lock.locked()


# --- fakes ---


class FakeClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta) -> None:
        self.now += timedelta(**delta)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


class FakePopen:
    """The ``auth login`` process: real pipes, scripted output, an exit the test decides."""

    # Linux pids never exceed 2**22 = 4194304, so these can never name a real process.
    _pids = itertools.count(90_000_000)

    def __init__(self, *, ignore_term: bool = False) -> None:
        out_r, self._out_w = os.pipe()
        err_r, self._err_w = os.pipe()
        self.stdout = os.fdopen(out_r, "r", encoding="utf-8", errors="replace")
        self.stderr = os.fdopen(err_r, "r", encoding="utf-8", errors="replace")
        self.pid = next(self._pids)
        self.returncode: int | None = None
        self.ignore_term = ignore_term
        self.signals: list[int] = []
        self._done = threading.Event()
        self._mu = threading.Lock()
        self._writers_open = True

    # what the "CLI" prints
    def say(self, text: str) -> None:
        assert self._writers_open, "the process already exited"
        _write_all(self._out_w, text.encode())

    def complain(self, text: str) -> None:
        assert self._writers_open, "the process already exited"
        _write_all(self._err_w, text.encode())

    def finish(self, code: int) -> None:
        with self._mu:
            if self.returncode is None:
                self.returncode = code
            if self._writers_open:
                # Closed exactly once: a closed fd number can be reused by another pipe.
                self._writers_open = False
                os.close(self._out_w)
                os.close(self._err_w)
        self._done.set()

    # the Popen interface the service uses
    def poll(self) -> int | None:
        return self.returncode if self._done.is_set() else None

    def wait(self, timeout: float | None = None) -> int:
        if not self._done.wait(timeout):
            raise subprocess.TimeoutExpired(["proton-drive"], timeout)
        return self.returncode

    def send_signal(self, sig: int) -> None:
        self.signals.append(sig)
        if sig == signal.SIGTERM and self.ignore_term:
            return
        self.finish(-sig)

    def terminate(self) -> None:
        self.send_signal(signal.SIGTERM)

    def kill(self) -> None:
        self.send_signal(signal.SIGKILL)

    @property
    def killed(self) -> bool:
        return signal.SIGKILL in self.signals

    @property
    def terminated(self) -> bool:
        return signal.SIGTERM in self.signals

    def close(self) -> None:
        self.finish(-signal.SIGKILL)
        for stream in (self.stdout, self.stderr):
            with suppress(OSError, ValueError):
                stream.close()


class Killer:
    """Stands in for ``os.killpg``: signals only the FakePopens it knows."""

    def __init__(self) -> None:
        self.procs: dict[int, FakePopen] = {}
        self.calls: list[tuple[int, int]] = []

    def __call__(self, pid: int, sig: int) -> None:
        self.calls.append((pid, sig))
        proc = self.procs.get(pid)
        if proc is None:
            raise ProcessLookupError(pid)
        proc.send_signal(sig)


class FakeVault:
    def __init__(self, secrets_dir: Path) -> None:
        self.secrets_dir = secrets_dir
        self.gnupg_home = secrets_dir / "gnupg"
        self.store_dir = secrets_dir / "store"
        self.cli_dir = secrets_dir / "cli"
        self.tpm = True
        self.unlock_errors: list[Exception] = []  # raised by the next unlocked() entries, in order
        self.create_error: Exception | None = None
        self.wipe_removes = True
        self.calls: list[str] = []
        self.probe_threads: list[threading.Thread] = []
        self.inside = False
        self._exists = True
        self.secrets_dir.mkdir(parents=True)
        (self.secrets_dir / "passphrase.jwe").write_text("sealed")

    def set_exists(self, value: bool) -> None:
        self._exists = value

    def tpm_available(self) -> bool:
        self.calls.append("tpm")
        self.probe_threads.append(threading.current_thread())
        return self.tpm

    def exists(self) -> bool:
        return self._exists

    def create(self) -> None:
        self.calls.append("create")
        if self.create_error is not None:
            raise self.create_error
        self.secrets_dir.mkdir(parents=True, exist_ok=True)
        self._exists = True

    @contextmanager
    def unlocked(self):
        self.calls.append("unlock")
        if self.unlock_errors:
            raise self.unlock_errors.pop(0)
        self.inside = True
        try:
            yield
        finally:
            self.inside = False

    def wipe(self) -> None:
        self.calls.append("wipe")
        if self.wipe_removes:
            shutil.rmtree(self.secrets_dir, ignore_errors=True)
            self._exists = False


class ServiceCli(FakeCli):
    """``FakeCli`` (Task 5) plus the calls the service makes outside a run."""

    def __init__(self, vault: FakeVault, paths: CliPaths, killer: Killer) -> None:
        super().__init__(vault)
        self.paths = paths
        self.killer = killer
        self.queued: list[FakePopen] = []
        self.started: list[FakePopen] = []
        self.login_inside_vault: list[bool] = []
        self.login_error: Exception | None = None
        self.version_error: Exception | None = None
        self.create_error: Exception | None = None
        self.logout_error: Exception | None = None

    def queue_login(self, first_line: str | None = None, **kw) -> FakePopen:
        proc = FakePopen(**kw)
        self.killer.procs[proc.pid] = proc
        self.queued.append(proc)
        proc.say(url_line() if first_line is None else first_line)
        return proc

    def queue_silent_login(self, **kw) -> FakePopen:
        proc = FakePopen(**kw)
        self.killer.procs[proc.pid] = proc
        self.queued.append(proc)
        return proc

    def start_login(self):
        self._enter("start_login", None)
        self.login_inside_vault.append(self.vault.inside)
        if self.login_error is not None:
            raise self.login_error
        proc = self.queued.pop(0)
        self.started.append(proc)
        return proc

    def version(self) -> str:
        self._enter("version", None)
        if self.version_error is not None:
            raise self.version_error
        return "0.9.0"

    def create_folder(self, folder: str) -> RemoteEntry:
        self._enter("create_folder", folder)
        if self.create_error is not None:
            raise self.create_error
        return RemoteEntry("WoW Backups", "folder", "u-new", None, None)

    def logout(self) -> None:
        self._enter("logout", None)
        if self.logout_error is not None:
            raise self.logout_error

    def all_procs(self) -> list[FakePopen]:
        return [*self.started, *self.queued]


class Rig:
    def __init__(self, tmp_path: Path) -> None:
        self.store = CloudStore(tmp_path / "data")
        self.backups = tmp_path / "backups"
        self.backups.mkdir()
        self.vault = FakeVault(self.store.secrets_dir)
        binary = tmp_path / "bin" / "proton-drive"
        binary.parent.mkdir()
        binary.write_text("never run by these tests\n")
        self.killer = Killer()
        paths = CliPaths(
            binary=binary, cache_dir=self.vault.cli_dir, gnupg_home=self.vault.gnupg_home,
            store_dir=self.vault.store_dir, backups_dir=self.backups,
        )
        self.cli = ServiceCli(self.vault, paths, self.killer)
        self.clock = FakeClock(NOW)
        self.services: list[CloudService] = []
        self.svc = self.service()

    def service(self, store: CloudStore | None = None, *, probe: bool = True, killpg=None) -> CloudService:
        svc = CloudService(
            store or self.store, backups_dir=self.backups, vault=self.vault, cli=self.cli,
            clock=self.clock, killpg=killpg or self.killer,
        )
        if probe:
            svc.probe()
        self.services.append(svc)
        return svc

    def configure(self, **fields) -> CloudConfig:
        cfg = CloudConfig(**{"folder": FOLDER, **fields})
        self.store.save_config(cfg)
        return self.store.load_config()

    def set_state(self, connection: str = "connected", **fields) -> None:
        self.store.save_state(CloudState(connection=connection, **fields))

    def local(self, name: str, data: bytes | None = None) -> None:
        path = self.backups / name
        path.write_bytes(name.encode() if data is None else data)
        os.utime(path, (NOW.timestamp(), NOW.timestamp()))

    def close(self) -> None:
        for svc in self.services:
            svc.cancel_sign_in()
        for proc in self.cli.all_procs():
            proc.finish(-signal.SIGKILL)
        idle = [svc.wait_idle(5) for svc in self.services]
        for proc in self.cli.all_procs():
            proc.close()
        assert all(idle), "a service thread was still running at the end of the test"


@pytest.fixture(autouse=True)
def short_waits(monkeypatch):
    monkeypatch.setattr(cloud_backup, "POLL_SECONDS", 0.01)
    monkeypatch.setattr(cloud_backup, "FIRST_LINE_TIMEOUT_SECONDS", 5.0)
    monkeypatch.setattr(cloud_backup, "SIGN_IN_DEADLINE_SECONDS", 20.0)
    monkeypatch.setattr(cloud_backup, "SIGN_IN_SETUP_TIMEOUT_SECONDS", 10.0)
    monkeypatch.setattr(cloud_backup, "CANCEL_TERM_GRACE_SECONDS", 2.0)
    monkeypatch.setattr(cloud_backup, "CANCEL_JOIN_SECONDS", 5.0)
    monkeypatch.setattr(cloud_backup, "DISCONNECT_WAIT_SECONDS", 5.0)
    monkeypatch.setattr(cloud_backup, "WAKE_SECONDS", 600)


@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path)
    yield r
    r.close()


@pytest.fixture
def gated_run(monkeypatch):
    """Replace run_sync with one that waits for ``gate`` and records its arguments."""
    gate = threading.Event()
    calls: list[dict] = []

    def fake_run_sync(**kw):
        calls.append(kw)
        assert gate.wait(5), "the test never opened the gate"
        return outcome("ok", remote=[RemoteFile(NEW, 3, "aa")])

    monkeypatch.setattr(cloud_backup, "run_sync", fake_run_sync)
    yield gate, calls
    gate.set()


# --- status and availability ---


def test_status_never_calls_the_cli(rig, monkeypatch):
    rig.configure(enabled=True)
    rig.set_state(remote=[RemoteFile(NEW, 3, "aa"), RemoteFile(D27, 4, None)], cli_version="0.9.0")
    for hours_ago in range(12, 0, -1):
        rig.store.append_log(log_entry(iso(NOW - timedelta(hours=hours_ago))))
    rig.vault.calls.clear()
    rig.cli.calls.clear()

    def no_process(*args, **kwargs):
        raise AssertionError("status() must not start a process")

    monkeypatch.setattr(subprocess, "run", no_process)
    monkeypatch.setattr(subprocess, "Popen", no_process)

    view = rig.svc.status()

    assert isinstance(view, CloudStatusView)
    assert rig.vault.calls == [] and rig.cli.calls == []
    assert view.available is True and view.unavailable_reason is None
    assert view.connection == "connected" and view.state.cli_version == "0.9.0"
    assert view.config.folder == FOLDER and view.config.enabled is True
    assert view.in_drive == frozenset({NEW, D27})
    assert len(view.log) == 10 and view.log[0].started == iso(NOW - timedelta(hours=1))  # newest first
    assert view.busy is False and view.signin_pending is False and view.signin_url is None
    assert view.stalled is False


def test_unavailable_without_tpm_or_binary(rig):
    unprobed = rig.service(probe=False)
    assert unprobed.status().available is None  # "Checking..."
    with pytest.raises(CloudUnavailable, match="Still checking"):
        unprobed.start_sign_in()

    rig.cli.paths.binary.unlink()
    rig.vault.calls.clear()
    rig.svc.probe()
    view = rig.svc.status()
    assert view.available is False
    assert view.unavailable_reason == "The Proton Drive program is missing from this admin image."
    assert "tpm" not in rig.vault.calls  # no TPM probe, so no process, without the program
    with pytest.raises(CloudUnavailable) as err:
        rig.svc.start_sign_in()
    assert str(err.value) == view.unavailable_reason

    rig.cli.paths.binary.write_text("back\n")
    rig.vault.tpm = False
    rig.svc.probe()
    view = rig.svc.status()
    assert view.available is False and "no usable TPM security chip" in view.unavailable_reason
    with pytest.raises(CloudUnavailable, match="TPM"):
        rig.svc.start_sign_in()
    assert rig.cli.started == [] and not locked(rig.svc)

    rig.vault.tpm = True
    rig.svc.probe()
    view = rig.svc.status()
    assert view.available is True and view.unavailable_reason is None


def test_probe_that_crashes_reports_unavailable(rig, monkeypatch):
    def broken() -> bool:
        raise RuntimeError("unexpected")

    monkeypatch.setattr(rig.vault, "tpm_available", broken)
    rig.svc.probe()
    view = rig.svc.status()
    assert view.available is False and view.unavailable_reason


# --- sign-in ---


def test_sign_in_returns_valid_url_and_connects_on_exit_zero(rig):
    rig.configure()
    proc = rig.cli.queue_login()

    assert rig.svc.start_sign_in() == URL

    view = rig.svc.status()
    assert view.signin_pending is True and view.signin_url == URL
    assert view.busy is False and view.connection == "disconnected"
    assert rig.cli.login_inside_vault == [True]
    proc.say('{"anything": "printed later is drained and ignored"}\n')
    proc.finish(0)
    assert rig.svc.wait_idle(5)

    view = rig.svc.status()
    assert view.connection == "connected"
    assert view.signin_pending is False and view.signin_url is None
    assert view.state.cli_version == "0.9.0"
    assert view.state.folder_status == "ok" and view.state.last_error is None
    assert rig.cli.count("info") == 1 and rig.cli.outside_vault_calls == 0
    assert not rig.vault.inside and not locked(rig.svc)
    assert proc.stdout.closed and proc.stderr.closed
    assert proc.signals == []  # it exited by itself


def test_sign_in_without_a_folder_connects_without_checking(rig):
    rig.cli.version_error = CloudCliError("failed", "no version")
    proc = rig.cli.queue_login()
    rig.svc.start_sign_in()
    proc.finish(0)
    assert rig.svc.wait_idle(5)
    state = rig.store.load_state()
    assert state.connection == "connected" and state.cli_version is None and state.folder_status is None
    assert rig.cli.count("info") == 0


def test_sign_in_refused_while_already_connected(rig):
    rig.set_state()
    with pytest.raises(CloudActionError, match="^Already connected — disconnect first$"):
        rig.svc.start_sign_in()
    assert rig.cli.calls == [] and not locked(rig.svc)


@pytest.mark.parametrize(
    "first_line",
    [
        url_line("https://evil.example/"),
        url_line("https://account.proton.me.evil.example/x"),
        url_line("http://account.proton.me/auth"),
        url_line("https://account.proton.me/a b"),
        json.dumps({"signInUrl": 42}) + "\n",
        json.dumps({"url": URL}) + "\n",
        "Sign in in your browser. Keep the terminal open.\n",
    ],
)
def test_sign_in_rejects_foreign_url_and_kills_process(rig, first_line):
    proc = rig.cli.queue_login(first_line)

    with pytest.raises(CloudActionError) as err:
        rig.svc.start_sign_in()

    assert "sign-in link" in str(err.value) and "evil" not in str(err.value)
    assert proc.killed
    assert rig.killer.calls == [(proc.pid, signal.SIGKILL)]  # the whole process group
    assert not locked(rig.svc) and not rig.vault.inside
    view = rig.svc.status()
    assert view.signin_pending is False and view.signin_url is None
    assert view.connection == "disconnected"


def test_kill_falls_back_to_the_process_when_its_group_is_gone(rig):
    def group_gone(pid: int, sig: int) -> None:
        raise ProcessLookupError(pid)

    svc = rig.service(killpg=group_gone)
    proc = rig.cli.queue_login(url_line("https://evil.example/"))
    with pytest.raises(CloudActionError):
        svc.start_sign_in()
    assert proc.killed and not locked(svc)


@pytest.mark.parametrize("case", ["missing", "unsealable", "usable"])
def test_sign_in_creates_vault_when_missing_or_unsealable(rig, case):
    if case == "missing":
        rig.vault.set_exists(False)
    elif case == "unsealable":
        rig.vault.unlock_errors.append(VaultError(UNLOCK_FAILED_MESSAGE))
    rig.cli.queue_login()

    assert rig.svc.start_sign_in() == URL

    expected = {
        "missing": ["create", "unlock"],
        "unsealable": ["unlock", "create", "unlock"],
        "usable": ["unlock"],
    }[case]
    assert [c for c in rig.vault.calls if c in ("create", "unlock", "wipe")] == expected
    assert rig.cli.login_inside_vault == [True]
    rig.svc.cancel_sign_in()


def test_sign_in_reports_a_vault_that_cannot_be_set_up(rig):
    rig.vault.set_exists(False)
    rig.vault.create_error = VaultError("Couldn't set up the secure store for the Proton Drive session.")

    with pytest.raises(CloudActionError, match="Couldn't set up the secure store"):
        rig.svc.start_sign_in()

    assert rig.cli.started == [] and not locked(rig.svc)
    state = rig.store.load_state()
    assert state.connection == "disconnected" and "secure store" in state.last_error


def test_busy_while_sign_in_pending(rig):
    rig.local(NEW)
    rig.configure(enabled=True)
    rig.cli.queue_login()
    rig.svc.start_sign_in()

    for call in (
        lambda: rig.svc.start_run("manual"),
        rig.svc.check_folder,
        rig.svc.create_folder,
        rig.svc.start_sign_in,
    ):
        with pytest.raises(CloudBusy):
            call()
    assert rig.svc.due(NOW) is False
    view = rig.svc.status()
    assert view.busy is False and view.signin_pending is True  # pending, not "busy"
    rig.svc.cancel_sign_in()


def test_cancel_sign_in_releases_lock(rig):
    rig.svc.cancel_sign_in()  # nothing pending: no-op
    proc = rig.cli.queue_login()
    rig.svc.start_sign_in()

    rig.svc.cancel_sign_in()

    assert proc.terminated and not proc.killed
    assert rig.killer.calls == [(proc.pid, signal.SIGTERM)]
    assert not locked(rig.svc) and not rig.vault.inside
    view = rig.svc.status()
    assert view.signin_pending is False and view.connection == "disconnected"
    assert view.state.last_error is None  # a cancel is not an error
    # A new sign-in can start straight away.
    second = rig.cli.queue_login()
    assert rig.svc.start_sign_in() == URL
    rig.svc.cancel_sign_in()
    assert second.terminated


def test_cancel_kills_a_sign_in_that_ignores_sigterm(rig, monkeypatch):
    monkeypatch.setattr(cloud_backup, "CANCEL_TERM_GRACE_SECONDS", 0.1)
    proc = rig.cli.queue_login(ignore_term=True)
    rig.svc.start_sign_in()

    rig.svc.cancel_sign_in()

    assert proc.terminated and proc.killed
    assert not locked(rig.svc)


def test_failed_sign_in_shows_the_last_stderr_line(rig):
    proc = rig.cli.queue_login()
    rig.svc.start_sign_in()
    proc.complain("Polling for approval...\n\x1b[31mAuthentication timed out\x1b[0m\n\n")
    proc.finish(1)
    assert rig.svc.wait_idle(5)
    state = rig.store.load_state()
    assert state.connection == "disconnected" and state.last_error == "Authentication timed out"


def test_failed_sign_in_without_stderr_says_so(rig):
    proc = rig.cli.queue_login()
    rig.svc.start_sign_in()
    proc.finish(2)
    assert rig.svc.wait_idle(5)
    assert rig.store.load_state().last_error == "Sign-in failed."


def test_a_chatty_stderr_never_stalls_the_sign_in(rig):
    proc = rig.cli.queue_silent_login()

    def chatter() -> None:
        proc.complain(("debug " * 170 + "\n") * 256)  # ~256 KiB, far more than a pipe holds
        proc.say(url_line())

    writer = threading.Thread(target=chatter, daemon=True)
    writer.start()
    assert rig.svc.start_sign_in() == URL
    writer.join(5)
    assert not writer.is_alive()
    proc.complain("Authentication failed: denied\n")
    proc.finish(1)
    assert rig.svc.wait_idle(5)
    assert rig.store.load_state().last_error == "Authentication failed: denied"


def test_sign_in_without_a_link_in_time_is_killed(rig, monkeypatch):
    monkeypatch.setattr(cloud_backup, "FIRST_LINE_TIMEOUT_SECONDS", 0.2)
    proc = rig.cli.queue_silent_login()
    with pytest.raises(CloudActionError, match="sign-in link"):
        rig.svc.start_sign_in()
    assert proc.killed and not locked(rig.svc)


def test_sign_in_that_stops_before_the_link_shows_why(rig):
    proc = rig.cli.queue_silent_login()
    proc.complain("Error: could not reach Proton\n")
    proc.finish(1)
    with pytest.raises(CloudActionError) as err:
        rig.svc.start_sign_in()
    assert "sign-in link" in str(err.value) and "could not reach Proton" in str(err.value)
    assert not locked(rig.svc)


def test_sign_in_that_never_finishes_is_killed_after_the_deadline(rig, monkeypatch):
    monkeypatch.setattr(cloud_backup, "SIGN_IN_DEADLINE_SECONDS", 0.3)
    proc = rig.cli.queue_login()
    assert rig.svc.start_sign_in() == URL
    assert rig.svc.wait_idle(5)
    assert proc.killed
    state = rig.store.load_state()
    assert state.connection == "disconnected" and state.last_error == "Sign-in timed out — try again."


def test_new_service_after_restart_is_not_busy_or_pending(rig, tmp_path):
    rig.cli.queue_login()
    rig.svc.start_sign_in()
    assert rig.svc.status().signin_pending

    restarted = rig.service(CloudStore(rig.store.data_dir))
    view = restarted.status()

    assert view.busy is False and view.signin_pending is False and view.signin_url is None
    assert view.connection == "disconnected"
    rig.svc.cancel_sign_in()


# --- disconnect ---


@pytest.mark.parametrize("problem", ["logout_fails", "vault_locked"])
def test_disconnect_wipes_vault_keeps_config_even_if_logout_fails(rig, problem):
    cfg = rig.configure(enabled=True, retention_days=14)
    rig.set_state(
        remote=[RemoteFile(NEW, 1, None)], remote_listed_at=iso(NOW), folder_status="ok",
        cli_version="0.9.0", last_error="old", issues=[{"name": NEW, "kind": "failed", "detail": "x"}],
        last_result="error", failing_since=iso(NOW - timedelta(hours=30)),
    )
    if problem == "logout_fails":
        rig.cli.logout_error = CloudCliError("failed", "network down")
    else:
        rig.vault.unlock_errors.append(VaultError(UNLOCK_FAILED_MESSAGE))

    rig.svc.disconnect()

    assert "wipe" in rig.vault.calls and "create" not in rig.vault.calls
    assert not rig.vault.secrets_dir.exists()
    state = rig.store.load_state()
    assert state.connection == "disconnected"
    assert state.remote == [] and state.remote_listed_at is None and state.issues == []
    assert state.folder_status is None and state.cli_version is None and state.last_error is None
    assert state.failing_since is None and rig.svc.status().stalled is False
    assert rig.store.load_config() == cfg
    assert rig.cli.count("logout") == (1 if problem == "logout_fails" else 0)
    assert rig.cli.outside_vault_calls == 0 and not locked(rig.svc)


def test_disconnect_refuses_while_a_run_holds_the_lock(rig, gated_run):
    gate, _ = gated_run
    rig.configure()
    rig.set_state()
    rig.svc.start_run("manual")
    assert rig.svc.status().busy is True

    with pytest.raises(CloudBusy):
        rig.svc.disconnect()
    assert "wipe" not in rig.vault.calls

    gate.set()
    assert rig.svc.wait_idle(5)
    rig.svc.disconnect()
    assert rig.store.load_state().connection == "disconnected"


def test_disconnect_cancels_a_pending_sign_in(rig):
    proc = rig.cli.queue_login()
    rig.svc.start_sign_in()

    rig.svc.disconnect()

    assert proc.terminated and "wipe" in rig.vault.calls
    assert rig.svc.status().signin_pending is False and not locked(rig.svc)


def test_disconnect_reports_session_files_it_could_not_remove(rig):
    rig.set_state()
    rig.vault.wipe_removes = False
    with pytest.raises(CloudActionError, match="^Could not remove the stored session files from this server.$"):
        rig.svc.disconnect()
    assert not locked(rig.svc)


# --- settings, folder check, create folder ---


def test_save_settings_forgets_the_listing_when_the_folder_changes(rig):
    rig.configure()
    rig.set_state(
        remote=[RemoteFile(NEW, 1, "aa")], remote_listed_at=iso(NOW), folder_status="ok",
        issues=[{"name": NEW, "kind": "conflict", "detail": "d"}],
        next_auto_attempt=iso(NOW + timedelta(hours=1)), last_success=iso(NOW), cli_version="0.9.0",
        failing_since=iso(NOW - timedelta(hours=2)),
    )

    rig.svc.save_settings(CloudConfig(enabled=True, folder=FOLDER + "/", retention_days=10))  # same folder

    state = rig.store.load_state()
    assert state.remote and state.folder_status == "ok" and state.issues and state.next_auto_attempt
    assert state.failing_since
    assert rig.store.load_config().retention_days == 10

    rig.svc.save_settings(CloudConfig(enabled=True, folder="/my-files/Other"))

    state = rig.store.load_state()
    assert state.remote == [] and state.remote_listed_at is None and state.folder_status is None
    assert state.issues == [] and state.next_auto_attempt is None and state.failing_since is None
    assert state.connection == "connected" and state.last_success == iso(NOW) and state.cli_version == "0.9.0"

    with pytest.raises(ValueError):
        rig.svc.save_settings(CloudConfig(enabled=True, folder="/elsewhere"))
    assert rig.store.load_config().folder == "/my-files/Other"


def test_run_finishing_after_a_folder_change_keeps_its_listing_out(rig, gated_run):
    gate, _ = gated_run
    rig.configure()
    rig.set_state()
    rig.svc.start_run("manual")
    rig.svc.save_settings(CloudConfig(folder="/my-files/Other"))
    gate.set()
    assert rig.svc.wait_idle(5)
    state = rig.store.load_state()
    assert state.last_result == "ok"  # the run itself is recorded...
    assert state.remote == [] and state.folder_status is None  # ...but not the old folder's listing


@pytest.mark.parametrize(
    "answer, expected",
    [
        (RemoteEntry("WoW Backups", "folder", "u", None, None), {"folder_status": "ok"}),
        (RemoteEntry("WoW Backups", "file", "u", 3, None), {"folder_status": "not_folder"}),
        (CloudCliError("not_found", "Node not found: WoW Backups"), {"folder_status": "missing"}),
        (
            CloudCliError("auth", "You need to login first"),
            {"connection": "disconnected", "last_error": "You need to login first"},
        ),
        (CloudCliError("timeout", "Proton Drive did not answer"), {"last_error": "Proton Drive did not answer"}),
        (VaultError(UNLOCK_FAILED_MESSAGE), {"connection": "vault_error", "last_error": UNLOCK_FAILED_MESSAGE}),
    ],
)
def test_check_folder_records_what_it_finds(rig, answer, expected):
    rig.configure()
    rig.set_state(folder_status=None)
    if isinstance(answer, VaultError):
        rig.vault.unlock_errors.append(answer)
    elif isinstance(answer, Exception):
        rig.cli.info_error = answer
    else:
        rig.cli.info_node = answer

    rig.svc.check_folder()

    state = rig.store.load_state()
    base = {"connection": "connected", "folder_status": None, "last_error": None}
    assert {key: getattr(state, key) for key in base} == {**base, **expected}
    assert "create" not in rig.vault.calls and "wipe" not in rig.vault.calls
    assert rig.cli.outside_vault_calls == 0 and not locked(rig.svc)


STALE_ISSUE = {"name": D27, "kind": "failed", "detail": "Upload failed."}
STALE_ERROR = "The Drive folder was not found. Create it in Proton Drive or choose another folder."


def stale_problems(rig: Rig, **fields) -> None:
    """A failed run's leftovers: red error text, a back-off, an issue and a log line."""
    rig.set_state(
        last_result="error", last_error=STALE_ERROR, next_auto_attempt=iso(NOW + timedelta(minutes=50)),
        issues=[STALE_ISSUE], **fields,
    )
    rig.store.append_log(log_entry(iso(NOW - timedelta(minutes=10)), "error"))


def assert_stale_problems_cleared(rig: Rig) -> None:
    state = rig.store.load_state()
    assert state.last_error is None and state.next_auto_attempt is None
    # What the last run did stays on record.
    assert state.last_result == "error" and state.issues == [STALE_ISSUE]
    assert [entry.result for entry in rig.store.read_log()] == ["error"]


@pytest.mark.parametrize("action", ["check_folder", "create_folder"])
def test_folder_fix_clears_a_stale_error_and_back_off(rig, action):
    rig.configure()
    stale_problems(rig, folder_status="missing")

    getattr(rig.svc, action)()

    assert rig.store.load_state().folder_status == "ok"
    assert_stale_problems_cleared(rig)


@pytest.mark.parametrize(
    "answer",
    [RemoteEntry("WoW Backups", "file", "u", 3, None), CloudCliError("not_found", "Node not found: WoW Backups")],
)
def test_folder_check_that_finds_a_problem_keeps_the_error(rig, answer):
    rig.configure()
    stale_problems(rig)
    if isinstance(answer, Exception):
        rig.cli.info_error = answer
    else:
        rig.cli.info_node = answer

    rig.svc.check_folder()

    state = rig.store.load_state()
    assert state.folder_status in ("not_folder", "missing")
    assert state.last_error == STALE_ERROR and state.next_auto_attempt == iso(NOW + timedelta(minutes=50))


@pytest.mark.parametrize("folder", [FOLDER, ""])
def test_sign_in_clears_a_stale_error_and_back_off(rig, folder):
    rig.configure(folder=folder)
    stale_problems(rig, connection="disconnected")
    proc = rig.cli.queue_login()
    rig.svc.start_sign_in()

    proc.finish(0)
    assert rig.svc.wait_idle(5)

    assert rig.store.load_state().connection == "connected"
    assert_stale_problems_cleared(rig)


def test_check_folder_does_nothing_without_connection_or_folder(rig):
    rig.configure()
    rig.set_state(connection="disconnected")
    rig.svc.check_folder()
    rig.store.save_config(CloudConfig())
    rig.set_state()
    rig.svc.check_folder()
    assert rig.cli.calls == [] and "unlock" not in rig.vault.calls


def test_create_folder_marks_the_folder_ok(rig):
    rig.configure()
    rig.set_state(folder_status="missing")
    rig.svc.create_folder()
    assert rig.cli.calls == [("create_folder", FOLDER)] and rig.cli.outside_vault_calls == 0
    assert rig.store.load_state().folder_status == "ok" and not locked(rig.svc)


@pytest.mark.parametrize(
    "error, message, connection",
    [
        (ValueError("A folder name cannot start with '-'"), "A folder name cannot start with '-'", "connected"),
        (CloudCliError("failed", "Parent not writable"), "Parent not writable", "connected"),
        (CloudCliError("auth", "You need to login first"), "You need to login first", "disconnected"),
        (VaultError(UNLOCK_FAILED_MESSAGE), UNLOCK_FAILED_MESSAGE, "vault_error"),
    ],
)
def test_create_folder_reports_errors(rig, error, message, connection):
    rig.configure()
    rig.set_state(folder_status="missing")
    if isinstance(error, VaultError):
        rig.vault.unlock_errors.append(error)
    else:
        rig.cli.create_error = error

    with pytest.raises(CloudActionError) as err:
        rig.svc.create_folder()

    assert str(err.value) == message
    state = rig.store.load_state()
    assert state.connection == connection and state.folder_status == "missing"
    assert "create" not in rig.vault.calls and "wipe" not in rig.vault.calls and not locked(rig.svc)


# --- runs ---


@pytest.mark.parametrize(
    "connection, folder, message",
    [
        ("disconnected", FOLDER, "Not connected to Proton Drive"),
        ("vault_error", FOLDER, UNLOCK_FAILED_MESSAGE),
        ("connected", "", "Choose a Drive folder first"),
    ],
)
def test_run_and_create_folder_refuse_without_connection_or_folder(rig, gated_run, connection, folder, message):
    _, calls = gated_run
    rig.configure(folder=folder)
    rig.set_state(connection=connection)
    for call in (lambda: rig.svc.start_run("manual"), rig.svc.create_folder):
        with pytest.raises(CloudActionError) as err:
            call()
        assert str(err.value) == message
    assert calls == [] and rig.cli.calls == [] and not locked(rig.svc)


def test_run_records_state_log_and_backoff(rig, monkeypatch):
    rig.configure()
    rig.set_state(last_success=iso(NOW - timedelta(days=1)))
    seen: list[dict] = []

    def partial_run(**kw):
        seen.append(kw)
        rig.clock.advance(minutes=5)
        return outcome(
            "partial", uploaded=[NEW], conflicts=[D27], failed=[D26],
            failure_details={D26: "Upload failed: quota exceeded"},
            remote=[RemoteFile(NEW, 3, "aa"), RemoteFile(D27, 9, "bb")],
        )

    monkeypatch.setattr(cloud_backup, "run_sync", partial_run)
    rig.svc.start_run("manual")
    assert rig.svc.wait_idle(5)

    (kw,) = seen
    assert kw["today"] == NOW.date() and type(kw["today"]) is dt.date
    assert kw["cli"] is rig.cli and kw["vault"] is rig.vault and kw["backups_dir"] == rig.backups
    assert kw["cfg"].folder == FOLDER
    end = NOW + timedelta(minutes=5)
    state = rig.store.load_state()
    assert state.last_attempt == iso(NOW) and state.last_result == "partial"
    assert state.last_success == iso(NOW - timedelta(days=1))  # a partial run is no success
    assert state.next_auto_attempt == iso(end + timedelta(minutes=60))
    assert state.failing_since == iso(end)
    assert state.last_error == f"{D26}: Upload failed: quota exceeded"
    assert [f.name for f in state.remote] == [NEW, D27] and state.remote_listed_at == iso(end)
    assert state.folder_status == "ok" and state.connection == "connected"
    assert state.issues == [
        {"name": D27, "kind": "conflict", "detail": CONFLICT_DETAIL},
        {"name": D26, "kind": "failed", "detail": "Upload failed: quota exceeded"},
    ]
    (entry,) = rig.store.read_log()
    assert (entry.trigger, entry.result, entry.started, entry.finished) == ("manual", "partial", iso(NOW), iso(end))
    assert (entry.uploaded, entry.conflicts, entry.failed) == ([NEW], [D27], [D26])
    assert entry.error == f"{D26}: Upload failed: quota exceeded"
    assert not locked(rig.svc)

    # A warning run (conflicts only) is a success: back-off cleared. With no listing and no
    # folder status learned, the previous listing and folder status stay.
    def warning_run(**kw):
        rig.clock.advance(minutes=1)
        return outcome("warning", conflicts=[D27], folder_status=None, listed=False, remote=[])

    monkeypatch.setattr(cloud_backup, "run_sync", warning_run)
    rig.svc.start_run("auto")
    assert rig.svc.wait_idle(5)
    end2 = end + timedelta(minutes=1)
    state = rig.store.load_state()
    assert state.last_success == iso(end2) and state.next_auto_attempt is None
    assert state.last_result == "warning" and state.last_error is None and state.failing_since is None
    assert [f.name for f in state.remote] == [NEW, D27] and state.remote_listed_at == iso(end)
    assert state.folder_status == "ok"
    assert state.issues == [{"name": D27, "kind": "conflict", "detail": CONFLICT_DETAIL}]
    assert [e.trigger for e in rig.store.read_log()] == ["auto", "manual"]


def test_aborted_run_keeps_known_issues_and_backs_off(rig, monkeypatch):
    rig.configure()
    issue = {"name": D27, "kind": "conflict", "detail": CONFLICT_DETAIL}
    rig.set_state(issues=[issue], remote=[RemoteFile(D27, 1, "x")])

    def aborted(**kw):
        return outcome("error", connection="disconnected", folder_status=None, listed=False,
                       error="You need to login first")

    monkeypatch.setattr(cloud_backup, "run_sync", aborted)
    rig.svc.start_run("auto")
    assert rig.svc.wait_idle(5)
    state = rig.store.load_state()
    assert state.connection == "disconnected" and state.last_error == "You need to login first"
    assert state.issues == [issue] and [f.name for f in state.remote] == [D27]
    assert state.next_auto_attempt == iso(NOW + timedelta(minutes=60))


def test_unexpected_run_failure_is_logged_backs_off_and_releases(rig, monkeypatch, caplog):
    rig.configure()
    rig.set_state(remote=[RemoteFile(D27, 1, "x")])

    def broken(**kw):
        raise RuntimeError("a bug")

    monkeypatch.setattr(cloud_backup, "run_sync", broken)
    rig.svc.start_run("auto")
    assert rig.svc.wait_idle(5)

    state = rig.store.load_state()
    message = "The cloud run failed unexpectedly — see the admin log."
    assert state.last_result == "error" and state.last_error == message
    assert state.next_auto_attempt == iso(NOW + timedelta(minutes=60)) and state.failing_since == iso(NOW)
    assert state.connection == "connected" and [f.name for f in state.remote] == [D27]
    (entry,) = rig.store.read_log()
    assert entry.result == "error" and entry.error == message
    assert not locked(rig.svc)
    assert "a bug" in caplog.text  # the traceback goes to the admin's own log


def test_run_uploads_with_the_real_sync_and_marks_in_drive(rig):
    rig.local(NEW, b"tonight's archive")
    rig.configure()
    rig.set_state()

    rig.svc.start_run("manual")
    assert rig.svc.wait_idle(5)

    assert rig.cli.names("upload") == [NEW] and rig.cli.outside_vault_calls == 0
    view = rig.svc.status()
    assert view.in_drive == frozenset({NEW})
    assert view.state.last_result == "ok" and view.state.folder_status == "ok"
    assert view.log[0].uploaded == [NEW] and view.log[0].trigger == "manual"


def test_stalled_after_26_hours_without_success(rig):
    rig.configure(enabled=True)

    def stalled(**fields) -> bool:
        rig.set_state(**fields)
        return rig.svc.status().stalled

    old, recent = iso(NOW - timedelta(hours=27)), iso(NOW - timedelta(hours=25))
    # Last success 27 h ago and failing now.
    assert stalled(last_result="error", last_success=old, failing_since=iso(NOW)) is True
    assert stalled(last_result="partial", last_success=old) is True
    # Never a success: counted from the first failure.
    assert stalled(last_result="error", failing_since=old) is True
    assert stalled(last_result="error", failing_since=recent) is False
    assert stalled(last_result="error", last_success="not a time", failing_since=old) is True
    # A success inside the window means not stalled.
    assert stalled(last_result="error", last_success=recent) is False
    # Nothing to count from (unset or unreadable) means not stalled.
    assert stalled(last_result="error") is False
    assert stalled(last_result="error", last_success="not a time", failing_since="garbage") is False
    assert stalled(last_result="warning", last_success=old) is False
    assert stalled(last_result=None, last_success=None) is False
    rig.configure(enabled=False)
    assert stalled(last_result="error", last_success=old, failing_since=old) is False


def test_first_failure_is_not_stalled_but_26_hours_of_failures_are(rig, monkeypatch):
    rig.configure(enabled=True)
    rig.set_state()
    results = iter(["error", "partial", "ok"])

    def scripted(**kw):
        rig.clock.advance(minutes=5)
        return outcome(next(results), folder_status=None, listed=False)

    monkeypatch.setattr(cloud_backup, "run_sync", scripted)

    rig.svc.start_run("auto")
    assert rig.svc.wait_idle(5)
    first_end = iso(NOW + timedelta(minutes=5))
    state = rig.store.load_state()
    assert state.failing_since == first_end and state.last_success is None
    assert rig.svc.status().stalled is False  # a fresh setup's first failure is not "stalled"

    rig.clock.advance(hours=27)
    rig.svc.start_run("manual")
    assert rig.svc.wait_idle(5)
    assert rig.store.load_state().failing_since == first_end  # still counted from the first failure
    assert rig.svc.status().stalled is True

    rig.svc.start_run("manual")
    assert rig.svc.wait_idle(5)
    state = rig.store.load_state()
    assert state.last_result == "ok" and state.failing_since is None
    assert rig.svc.status().stalled is False


# --- when to run ---


def test_due_respects_enabled_connection_backoff_and_known_remote(rig):
    rig.local(NEW)
    rig.local(D27)
    rig.configure(enabled=True)
    rig.set_state()
    svc = rig.svc
    assert svc.due(NOW) is True

    rig.configure(enabled=False)
    assert svc.due(NOW) is False
    rig.configure(enabled=True)

    for connection in ("disconnected", "vault_error"):
        rig.set_state(connection=connection)
        assert svc.due(NOW) is False

    rig.set_state(next_auto_attempt=iso(NOW + timedelta(minutes=1)))
    assert svc.due(NOW) is False
    rig.set_state(next_auto_attempt=iso(NOW - timedelta(minutes=1)))
    assert svc.due(NOW) is True
    rig.set_state(next_auto_attempt="garbage")  # unreadable = unset
    assert svc.due(NOW) is True

    rig.set_state(remote=[RemoteFile(NEW, 1, "a"), RemoteFile(D27, 1, "b")])
    assert svc.due(NOW) is False  # everything is known to be in the Drive
    rig.set_state(remote=[RemoteFile(NEW, 1, "a")])
    assert svc.due(NOW) is True  # D27 is not

    with svc._lock:
        assert svc.due(NOW) is False  # busy

    rig.vault.tpm = False
    svc.probe()
    assert svc.due(NOW) is False
    rig.vault.tpm = True
    svc.probe()

    (rig.backups / NEW).unlink()
    (rig.backups / D27).unlink()
    rig.local(daily("2026-10-01"))  # past retention: never a candidate
    rig.set_state()
    assert svc.due(NOW) is False


def test_tick_starts_an_auto_run_only_when_due(rig, monkeypatch):
    runs: list[dict] = []

    def fake_run(**kw):
        runs.append(kw)
        return outcome("ok", remote=[RemoteFile(NEW, 1, "a")])

    monkeypatch.setattr(cloud_backup, "run_sync", fake_run)
    rig.local(NEW)
    rig.configure(enabled=True)
    rig.set_state()

    rig.svc.tick()
    assert rig.svc.wait_idle(5)
    assert len(runs) == 1 and rig.store.read_log()[0].trigger == "auto"

    rig.svc.tick()
    assert rig.svc.wait_idle(5)
    assert len(runs) == 1  # NEW is now known to be in the Drive


def test_state_write_failure_does_not_stop_loop(rig, monkeypatch, caplog):
    runs: list[dict] = []

    def fake_run(**kw):
        runs.append(kw)
        return outcome("ok")

    def disk_full(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(cloud_backup, "run_sync", fake_run)
    rig.local(NEW)
    rig.configure(enabled=True)
    rig.set_state()
    monkeypatch.setattr(rig.store, "save_state", disk_full)
    monkeypatch.setattr(rig.store, "append_log", disk_full)

    rig.svc.start_run("manual")  # no exception escapes
    assert rig.svc.wait_idle(5)
    assert len(runs) == 1 and not locked(rig.svc)

    rig.svc.tick()  # the loop body returns, and still runs
    assert rig.svc.wait_idle(5)
    assert len(runs) == 2 and not locked(rig.svc)
    assert "No space left on device" in caplog.text


async def test_loop_probes_on_a_thread_ticks_and_stop_cancels_a_sign_in(rig, monkeypatch):
    monkeypatch.setattr(cloud_backup, "WAKE_SECONDS", 0.01)
    svc = rig.service(probe=False)
    ticks: list[threading.Thread] = []
    real_tick = svc.tick

    def counting_tick() -> None:
        ticks.append(threading.current_thread())
        real_tick()

    monkeypatch.setattr(svc, "tick", counting_tick)

    await svc.start()
    await svc.start()  # a second start does not start a second loop
    for _ in range(500):
        if len(ticks) >= 3:
            break
        await asyncio.sleep(0.01)

    assert len(ticks) >= 3
    assert svc.status().available is True
    assert rig.vault.probe_threads[-1] is not threading.current_thread()  # probe ran off the event loop
    assert all(t is not threading.current_thread() for t in ticks)

    proc = rig.cli.queue_login()
    assert await asyncio.to_thread(svc.start_sign_in) == URL
    await svc.stop()

    assert proc.terminated and svc.status().signin_pending is False
    count = len(ticks)
    await asyncio.sleep(0.05)
    assert len(ticks) == count  # the loop is gone
    await svc.stop()  # stopping twice is harmless


# --- secrets ---


def test_status_and_errors_never_expose_session_material(rig, monkeypatch):
    secret = json.dumps(
        {"userKeyPassword": "ukp", "session": {"accessToken": "at", "refreshToken": "rt"}}
    )
    messages: list[str] = []

    # 1. A first line carrying session fields instead of a link.
    rig.cli.queue_login(secret + "\n")
    with pytest.raises(CloudActionError) as err:
        rig.svc.start_sign_in()
    messages.append(str(err.value))

    # 2. A sign-in that prints session material to stdout and stderr, then fails.
    proc = rig.cli.queue_login()
    rig.svc.start_sign_in()
    proc.say(secret + "\n")
    proc.complain(f"Error: rejected {secret}\n")
    proc.finish(1)
    assert rig.svc.wait_idle(5)
    messages.append(rig.store.load_state().last_error)

    # 3. A run whose CLI errors quote session fields.
    rig.configure()
    rig.set_state()
    monkeypatch.setattr(
        cloud_backup, "run_sync",
        lambda **kw: outcome("partial", failed=[NEW], failure_details={NEW: f"upload failed: {secret}"}),
    )
    rig.svc.start_run("manual")
    assert rig.svc.wait_idle(5)

    # 4. Folder check and create.
    rig.cli.info_error = CloudCliError("failed", f"boom {secret}")
    rig.svc.check_folder()
    messages.append(rig.store.load_state().last_error)
    rig.cli.create_error = CloudCliError("failed", f"boom {secret}")
    with pytest.raises(CloudActionError) as err:
        rig.svc.create_folder()
    messages.append(str(err.value))

    shown = " ".join(
        [
            str(rig.svc.status()),
            *messages,
            json.dumps([e.to_dict() for e in rig.store.read_log()]),
            rig.store.state_path.read_text(),
        ]
    ).lower()
    for word in SECRET_WORDS:
        assert word.lower() not in shown


# --- wiring ---


def test_get_service_is_one_wired_instance(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_DATA_DIR", str(tmp_path / "admin-data"))
    monkeypatch.setenv("AC_STACK_DIR", str(tmp_path / "ac"))
    monkeypatch.setenv("PROTON_DRIVE_BIN", str(tmp_path / "bin" / "proton-drive"))
    monkeypatch.setattr(cloud_backup, "_service", None)

    svc = cloud_backup.get_service()

    assert cloud_backup.get_service() is svc
    paths = svc.cli.paths
    assert paths.binary == tmp_path / "bin" / "proton-drive"
    assert paths.backups_dir == tmp_path / "ac" / "backups"
    assert paths.backups_dir is svc.backups_dir  # the wrapper compares archive paths exactly
    vault = svc.vault
    assert vault.secrets_dir == tmp_path / "admin-data" / "cloud" / "secrets" == svc.store.secrets_dir
    assert (paths.cache_dir, paths.gnupg_home, paths.store_dir) == (vault.cli_dir, vault.gnupg_home, vault.store_dir)
    assert svc.status().available is None  # nothing probed yet
    assert not (tmp_path / "admin-data").exists()  # building it touches no files
