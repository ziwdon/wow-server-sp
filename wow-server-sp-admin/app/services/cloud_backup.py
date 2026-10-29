"""The stateful side of the Proton Drive backup copy: the cloud lock, sign-in, runs,
the background loop and the status view (spec 6.1, 6.5-6.7, 6.9).

``CloudService`` composes the pieces that already hold the rules -- the stores
(``cloud_config``), candidate selection (``cloud_plan``), the allowlisted CLI
wrapper (``cloud_cli``), the session vault (``cloud_vault``) and one sync run
(``cloud_sync``) -- and decides *when* things happen.

The cloud lock is one ``threading.Lock``. Every CLI call and every vault
create/unlock/wipe happens while it is held, because the CLI's cache and the
``pass`` entry are not safe for concurrent processes. Requests never wait for it:
they get ``CloudBusy`` (only ``disconnect`` waits briefly, for a sign-in it has
just cancelled). It is a plain ``Lock``, not an ``RLock``: a sign-in or a run is
started by a request thread and finished, lock released, by its own worker thread.

A sign-in holds the lock for up to 11 minutes while ``auth login`` waits for the
owner to approve it. Its URL, process and thread live only in memory, so a
restarted app never looks busy or "waiting for sign-in". The login's first stdout
line is read with ``selectors`` and a deadline (never a bare ``readline``), and its
stderr is drained from the start so a chatty CLI cannot fill the pipe and stall.

``status()`` reads files only (no process is ever started for a page render).
State and log writes are advisory: a failure is logged and the work carries on.
When state.json cannot be written, the newest state is kept in memory and used
(by ``status()`` and ``due()``, so a result and its back-off still count) until a
later write works; the panel says so meanwhile.

No text the service shows or stores is allowed to carry session material: the
service never reads the session, and CLI text that names a session field is
replaced before it is kept (``_scrub``).

Routes call the blocking methods on a worker thread (the state-changing ones through
``main._await_thread_completion``); ``start()``/``stop()`` run the loop as an asyncio
task like ``MaintenanceScheduler``/``PresenceAnnouncer``.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import logging
import os
import queue
import selectors
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.services.backups import BackupListingError, list_backups
from app.services.cloud_cli import CliPaths, CloudCliError, ProtonDriveCli, _last_line, default_binary
from app.services.cloud_config import CloudConfig, CloudLogEntry, CloudState, CloudStore, store_from_env
from app.services.cloud_plan import select_candidates
from app.services.cloud_sync import RunOutcome, run_sync
from app.services.cloud_vault import UNLOCK_FAILED_MESSAGE, SessionVault, VaultError

log = logging.getLogger(__name__)

# Looked up at call time (not bound as defaults), so tests can shorten them.
WAKE_SECONDS = 600  # the loop's cadence (spec 6.6: "every 10 minutes")
REPROBE_SECONDS = 3600  # while the TPM is unusable, the loop checks it again this often
FIRST_LINE_TIMEOUT_SECONDS = 30.0  # for the login's {"signInUrl": ...} line
SIGN_IN_DEADLINE_SECONDS = 11 * 60  # the CLI itself gives up after 10 minutes
SIGN_IN_SETUP_TIMEOUT_SECONDS = 180.0  # vault check/creation before the login starts (key generation)
HANDOFF_GRACE_SECONDS = 5.0
CANCEL_TERM_GRACE_SECONDS = 5.0
CANCEL_JOIN_SECONDS = 15.0
DISCONNECT_WAIT_SECONDS = 15.0
KILL_WAIT_SECONDS = 5.0
DRAIN_JOIN_SECONDS = 2.0
POLL_SECONDS = 0.2
STDERR_TAIL_BYTES = 8 * 1024
MAX_LINK_LINE_BYTES = 16 * 1024
MAX_LINK_CHARS = 2048
RETRY_AFTER_FAILURE = timedelta(minutes=60)
STALLED_AFTER = timedelta(hours=26)
STATUS_LOG_ENTRIES = 10
SIGN_IN_URL_PREFIX = "https://account.proton.me/"

# --- user-facing copy ---

MISSING_PROGRAM = "The Proton Drive program is missing from this admin image."
NO_TPM = (
    "Could not use this server's TPM security chip, which cloud backup needs to keep the Proton "
    "session encrypted."
)
PROBE_FAILED = "Could not check whether cloud backup can run on this server — see the admin log."
CHECKING = "Still checking whether this server can store the session safely — try again in a moment."
BUSY = "Cloud backup is busy with another Proton Drive task — try again in a moment."
ALREADY_CONNECTED = "Already connected — disconnect first"
NOT_CONNECTED = "Not connected to Proton Drive"
NO_FOLDER = "Choose a Drive folder first"
SIGN_IN_NO_LINK = "Proton Drive did not provide a sign-in link — try again."
SIGN_IN_BAD_LINK = "Proton Drive gave an unexpected sign-in link, so it was not used — try again."
SIGN_IN_STOPPED = "Proton Drive stopped before giving a sign-in link"
SIGN_IN_TIMED_OUT = "Sign-in timed out — try again."
SIGN_IN_FAILED = "Sign-in failed."
SIGN_IN_CANCELLED = "Sign-in cancelled."
SIGN_IN_UNEXPECTED = "The sign-in failed unexpectedly — see the admin log."
SIGN_IN_STUCK = "Starting the sign-in took too long — try again."
RUN_UNEXPECTED = "The cloud run failed unexpectedly — see the admin log."
WIPE_FAILED = "Could not remove the stored session files from this server."
CONFLICT_DETAIL = (
    "A different file with this name is already in the Drive folder. Move that copy to the "
    "Drive trash yourself if you want this server's version uploaded."
)
UPLOAD_FAILED = "Upload failed."
HIDDEN_DETAIL = "Proton Drive reported an error (details hidden because they may include session data)."

# Field names of the CLI's stored session (spec 2). Text naming one is never kept or shown.
_SESSION_WORDS = ("userkeypassword", "accesstoken", "refreshtoken", "cachepassword")

# What may hold the cloud lock while using the folder it read at its start for every CLI call
# (a run's uploads and trashes too): the folder setting cannot change under them (I-1). A
# sign-in only checks the folder (read-only, its result guarded by R-8), so it does not count.
_FOLDER_HOLDERS = frozenset({"run", "check", "create"})

# State that describes one Drive folder; reset when the folder setting changes (R-8).
_FOLDER_FIELDS = frozenset(
    {"folder_status", "remote", "remote_listed_at", "issues", "next_auto_attempt", "failing_since"}
)

# Written when a folder check finds the folder, a folder is created or a sign-in succeeds (R-16):
# the old red error goes away and the next wake-up may run. last_result, issues and the log stay.
_PROBLEM_FIXED: dict[str, Any] = {"last_error": None, "next_auto_attempt": None}


def _folder_reset() -> dict[str, Any]:
    """``_FOLDER_FIELDS`` at their ``CloudState`` defaults (fresh lists each call)."""
    defaults = CloudState()
    return {key: getattr(defaults, key) for key in sorted(_FOLDER_FIELDS)}


class CloudBusy(Exception):
    """The cloud lock is held (a run, a folder check, a sign-in...). The message is user-facing."""


class CloudUnavailable(Exception):
    """Cloud backup cannot work on this server (or that is still being checked)."""


class CloudActionError(Exception):
    """An action was refused or failed; the message is user-facing."""


@dataclass(frozen=True)
class CloudStatusView:
    available: bool | None  # None = still checking
    unavailable_reason: str | None
    connection: str
    signin_pending: bool
    signin_url: str | None
    busy: bool
    stalled: bool
    config: CloudConfig
    state: CloudState
    log: list[CloudLogEntry]
    in_drive: frozenset[str]  # in the last listing, and not a conflict
    has_stored_session: bool  # a vault is on disk (file checks only), so Disconnect has work to do
    state_unsaved: bool  # state.json could not be written; ``state`` is the newer in-memory copy


# --- small helpers ---


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse_time(text: Any) -> datetime | None:
    """A stored timestamp, or ``None`` when it is missing or unreadable (treated as unset)."""
    if not isinstance(text, str):
        return None
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


def _scrub(text: str | None) -> str | None:
    """``text`` unless it names a session field; such text is replaced, never shown or stored."""
    if text is None:
        return None
    lowered = text.lower()
    return HIDDEN_DETAIL if any(word in lowered for word in _SESSION_WORDS) else text


def _close(stream: Any) -> None:
    if stream is not None:
        with contextlib.suppress(OSError, ValueError):
            stream.close()


class _Tail:
    """The last few KiB a process wrote to stderr (thread-safe)."""

    def __init__(self) -> None:
        self._data = bytearray()
        self._mu = threading.Lock()

    def add(self, chunk: bytes) -> None:
        with self._mu:
            self._data += chunk
            if len(self._data) > STDERR_TAIL_BYTES:
                del self._data[:-STDERR_TAIL_BYTES]

    def text(self) -> str:
        with self._mu:
            return bytes(self._data).decode("utf-8", "replace")


def _drain(stream: Any, tail: _Tail) -> None:
    """Read ``stream`` until EOF into ``tail``, then close it (this thread owns it)."""
    try:
        fd = stream.fileno()
        while chunk := os.read(fd, 4096):
            tail.add(chunk)
    except (OSError, ValueError):
        pass
    finally:
        _close(stream)


def _read_first_line(stream: Any, deadline: float, cancelled: threading.Event) -> tuple[str, str | None]:
    """The first line of ``stream``: ``("line", text)``, or ``("timeout"|"eof"|"toolong"|"cancelled", None)``.

    Reads the raw fd (the file object's own buffer is never used), so ``select``
    sees exactly what is unread.
    """
    fd = stream.fileno()
    buffered = bytearray()
    with selectors.DefaultSelector() as selector:
        selector.register(fd, selectors.EVENT_READ)
        while True:
            if cancelled.is_set():
                return "cancelled", None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return "timeout", None
            if not selector.select(min(remaining, POLL_SECONDS)):
                continue
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                return "eof", None
            if not chunk:
                return "eof", None
            buffered += chunk
            line, newline, _ = buffered.partition(b"\n")
            if newline:
                return "line", bytes(line).decode("utf-8", "replace")
            if len(buffered) > MAX_LINK_LINE_BYTES:
                return "toolong", None


def _parse_sign_in_url(line: str | None) -> str | None:
    """The ``signInUrl`` of the login's first line, only if it is a plain Proton account link."""
    try:
        data = json.loads(line or "")
    except (ValueError, RecursionError):
        return None
    url = data.get("signInUrl") if isinstance(data, dict) else None
    if not isinstance(url, str) or not url.startswith(SIGN_IN_URL_PREFIX) or len(url) > MAX_LINK_CHARS:
        return None
    if any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in url):
        return None
    return url


def _wait_for_exit(proc: Any, deadline: float, cancelled: threading.Event) -> int | None:
    """Wait for the login to exit, discarding its stdout. ``None`` when the deadline passes.

    After a cancel, the canceller stops the process; give it the grace period it
    uses and then let the caller kill it, so a cancel is always bounded.
    """
    stream = proc.stdout
    selector = selectors.DefaultSelector()
    reading = False
    try:
        try:
            fd = stream.fileno()
            selector.register(fd, selectors.EVENT_READ)
            reading = True
        except (AttributeError, OSError, ValueError):
            fd = -1
        cancel_deadline: float | None = None
        while True:
            code = proc.poll()
            if code is not None:
                return code
            now = time.monotonic()
            if cancelled.is_set():
                if cancel_deadline is None:
                    cancel_deadline = now + CANCEL_TERM_GRACE_SECONDS + 1.0
                elif now >= cancel_deadline:
                    return None
            remaining = deadline - now
            if remaining <= 0:
                return None
            step = min(remaining, POLL_SECONDS)
            if reading:
                if selector.select(step):
                    try:
                        data = os.read(fd, 65536)
                    except OSError:
                        data = b""
                    if not data:
                        selector.unregister(fd)
                        reading = False
            else:
                try:
                    return proc.wait(timeout=step)
                except subprocess.TimeoutExpired:
                    pass
    finally:
        selector.close()


@dataclass(eq=False)
class _SignIn:
    """One in-progress ``auth login`` (in memory only)."""

    cancelled: threading.Event = field(default_factory=threading.Event)
    handoff: queue.Queue = field(default_factory=queue.Queue)
    tail: _Tail = field(default_factory=_Tail)
    thread: threading.Thread | None = None
    drain: threading.Thread | None = None
    proc: Any = None
    url: str | None = None
    link_given: bool = False


@dataclass
class _SignInEnd:
    result: str  # connected | failed | cancelled
    message: str | None = None
    fields: dict[str, Any] = field(default_factory=dict)
    folder: str | None = None


# --- the service ---


class CloudService:
    def __init__(
        self,
        store: CloudStore,
        *,
        backups_dir: Path,
        vault: SessionVault,
        cli: ProtonDriveCli,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        killpg: Callable[[int, int], None] = os.killpg,
    ) -> None:
        self.store = store
        self.backups_dir = backups_dir
        self.vault = vault
        self.cli = cli
        self._clock = clock
        self._killpg = killpg
        self._lock = threading.Lock()  # the cloud lock (see the module docstring)
        self._holder: str | None = None  # what holds it: signin | run | check | create | disconnect
        self._mu = threading.Lock()  # guards the in-memory fields below
        self._state_mu = threading.RLock()  # serialises read-modify-write of state.json
        self._signin: _SignIn | None = None
        self._threads: list[threading.Thread] = []
        self._available: bool | None = None
        self._unavailable_reason: str | None = None
        self._probed_at: float | None = None  # time.monotonic() of the last probe
        self._unsaved_state: CloudState | None = None  # newest state while state.json cannot be written
        self._task: asyncio.Task | None = None

    # --- availability and status ---

    def probe(self) -> None:
        """Decide whether cloud backup can work here: the CLI binary, then the TPM.

        Run at start-up, then again by ``tick`` about hourly while the answer is "no"
        for any reason but a missing binary (only a new admin image brings that back).
        """
        try:
            if not self.cli.paths.binary.exists():
                available, reason = False, MISSING_PROGRAM  # no TPM probe, so no process, without it
            elif not self.vault.tpm_available():
                available, reason = False, NO_TPM
            else:
                available, reason = True, None
        except Exception:  # noqa: BLE001 - a broken probe must not leave the card on "Checking..."
            log.exception("cloud backup availability check failed")
            available, reason = False, PROBE_FAILED
        with self._mu:
            self._available, self._unavailable_reason = available, reason
            self._probed_at = time.monotonic()
        log.info("cloud backup available: %s%s", available, f" ({reason})" if reason else "")

    def _reprobe_due(self) -> bool:
        with self._mu:
            available, reason, probed_at = self._available, self._unavailable_reason, self._probed_at
        if available is not False or reason == MISSING_PROGRAM or probed_at is None:
            return False
        return time.monotonic() - probed_at >= REPROBE_SECONDS

    def status(self) -> CloudStatusView:
        """Everything the Backups page shows. Reads files only; never starts a process."""
        cfg = self.store.load_config()
        with self._state_mu:
            state = self._load_state()
            unsaved = self._unsaved_state is not None
        with self._mu:
            signin = self._signin
            available, reason = self._available, self._unavailable_reason
        try:
            stored = self.vault.exists()
        except OSError:
            stored = True  # cannot tell: keep Disconnect reachable
        conflicts = {item.get("name") for item in state.issues if item.get("kind") == "conflict"}
        return CloudStatusView(
            available=available,
            unavailable_reason=reason,
            connection=state.connection,
            signin_pending=signin is not None,
            signin_url=signin.url if signin is not None else None,
            busy=self._holder not in (None, "signin"),
            stalled=self._stalled(cfg, state, self._clock()),
            config=cfg,
            state=state,
            log=self.store.read_log()[:STATUS_LOG_ENTRIES],
            # A conflict is a different file under that name: this server's archive is not there.
            in_drive=frozenset(item.name for item in state.remote) - conflicts,
            has_stored_session=stored,
            state_unsaved=unsaved,
        )

    @staticmethod
    def _stalled(cfg: CloudConfig, state: CloudState, now: datetime) -> bool:
        """Failing, with no success for more than 26 h (spec 6.6).

        Counted from the last success, or, if there never was one, from the first
        failure (``failing_since``): a first failure is stalled only when the last
        success is already that old. Nothing readable to count from: not stalled.
        """
        if not (cfg.enabled and state.last_result in ("partial", "error")):
            return False
        since = _parse_time(state.last_success) or _parse_time(state.failing_since)
        return since is not None and now - since > STALLED_AFTER

    # --- settings and the folder ---

    def save_settings(self, cfg: CloudConfig) -> None:
        """Validate and save. A new folder forgets what was learned about the old one (R-8).

        A new folder is refused with ``CloudBusy`` (nothing saved) while a run, a
        folder check or a folder creation holds the cloud lock: they would go on
        using the old folder (I-1). They read the folder under ``_state_mu`` after
        taking the lock, so this check and their read cannot interleave.
        """
        cfg.validate()
        new = cfg.canonical()
        with self._state_mu:
            old_folder = self.store.load_config().folder
            if new.folder != old_folder and self._holder in _FOLDER_HOLDERS:
                raise CloudBusy(BUSY)
            self.store.save_config(new)
            if new.folder != old_folder:
                self._update_state(_folder_reset())

    def check_folder(self) -> None:
        """``fs info`` the configured folder and record what it is. Errors go to ``last_error``."""
        self._acquire("check")
        try:
            with self._state_mu:  # see save_settings
                cfg = self.store.load_config()
                connected = self._load_state().connection == "connected"
            if not connected or not cfg.folder:
                return
            try:
                with self.vault.unlocked():
                    fields = self._folder_check(cfg.folder)
            except VaultError as exc:
                fields = {"connection": "vault_error", "last_error": str(exc)}
            self._update_state(fields, folder=cfg.folder)
        finally:
            self._release()

    def create_folder(self) -> None:
        """Create the configured folder's last segment once (spec 6.7). Raises ``CloudActionError``."""
        self._acquire("create")
        try:
            cfg = self._require_ready()
            try:
                with self.vault.unlocked():
                    self.cli.create_folder(cfg.folder)
            except VaultError as exc:
                self._update_state({"connection": "vault_error", "last_error": str(exc)})
                raise CloudActionError(str(exc)) from None
            except CloudCliError as exc:
                message = _scrub(exc.message)
                if exc.kind == "auth":
                    self._update_state({"connection": "disconnected", "last_error": message})
                raise CloudActionError(message) from None
            except ValueError as exc:
                raise CloudActionError(str(exc)) from None
            self._update_state({"folder_status": "ok", **_PROBLEM_FIXED}, folder=cfg.folder)
        finally:
            self._release()

    def _folder_check(self, folder: str) -> dict[str, Any]:
        """What ``fs info`` says about ``folder``, as state fields (caller holds the lock and the vault)."""
        try:
            node = self.cli.info(folder)
        except CloudCliError as exc:
            if exc.kind == "not_found":
                return {"folder_status": "missing"}
            if exc.kind == "auth":
                return {"connection": "disconnected", "last_error": _scrub(exc.message)}
            return {"last_error": _scrub(exc.message)}
        except ValueError as exc:
            return {"last_error": str(exc)}
        if node.type != "folder":
            return {"folder_status": "not_folder"}
        return {"folder_status": "ok", **_PROBLEM_FIXED}

    def _require_ready(self) -> CloudConfig:
        """The config, if a run or folder action can go ahead; else ``CloudActionError``.

        Called with the cloud lock held; reads under ``_state_mu`` (see ``save_settings``).
        """
        with self._state_mu:
            connection = self._load_state().connection
            cfg = self.store.load_config()
        if connection == "vault_error":
            raise CloudActionError(UNLOCK_FAILED_MESSAGE)
        if connection != "connected":
            raise CloudActionError(NOT_CONNECTED)
        if not cfg.folder:
            raise CloudActionError(NO_FOLDER)
        return cfg

    # --- sign-in ---

    def start_sign_in(self) -> str:
        """Start ``auth login`` and return its sign-in link; the login then runs on its own thread."""
        with self._mu:
            available, reason = self._available, self._unavailable_reason
        if available is None:
            raise CloudUnavailable(CHECKING)
        if not available:
            raise CloudUnavailable(reason or NO_TPM)
        if self._load_state().connection == "connected":
            raise CloudActionError(ALREADY_CONNECTED)
        self._acquire("signin")
        signin = _SignIn()
        try:
            signin.thread = threading.Thread(
                target=self._sign_in_worker, args=(signin,), name="cloud-sign-in", daemon=True
            )
            with self._mu:
                self._signin = signin
            self._start(signin.thread)
        except BaseException:
            with self._mu:
                self._signin = None
            self._release()
            raise
        return self._await_link(signin)

    def cancel_sign_in(self) -> None:
        """Stop a pending sign-in (SIGTERM, then SIGKILL) and wait for it to let go of the lock."""
        with self._mu:
            signin = self._signin
        if signin is not None:
            self._cancel(signin)

    def _cancel(self, signin: _SignIn) -> None:
        with self._mu:
            signin.cancelled.set()
            proc = signin.proc
        if proc is not None and proc.poll() is None:
            self._signal(proc, signal.SIGTERM)
            try:
                proc.wait(timeout=CANCEL_TERM_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                self._signal(proc, signal.SIGKILL)
        thread = signin.thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(CANCEL_JOIN_SECONDS)

    def _await_link(self, signin: _SignIn) -> str:
        """Wait for the worker to hand back the link or an error (bounded, whatever the worker does)."""
        deadline = time.monotonic() + SIGN_IN_SETUP_TIMEOUT_SECONDS
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                log.warning("Proton Drive sign-in gave no link in time; cancelling it")
                self._cancel(signin)
                raise CloudActionError(SIGN_IN_STUCK)
            try:
                kind, value = signin.handoff.get(timeout=remaining)
            except queue.Empty:
                continue
            if kind == "started":
                # The login process is running: from here it has its own 30 s deadline for the link.
                deadline = time.monotonic() + FIRST_LINE_TIMEOUT_SECONDS + HANDOFF_GRACE_SECONDS
                continue
            if kind == "url":
                return value
            raise CloudActionError(value)

    def _sign_in_worker(self, signin: _SignIn) -> None:
        end = _SignInEnd("failed", SIGN_IN_UNEXPECTED)
        try:
            end = self._sign_in(signin)
        except Exception:  # noqa: BLE001 - a bug must never leave the lock held
            log.exception("Proton Drive sign-in failed unexpectedly")
        finally:
            try:
                self._record_sign_in(end)
            except Exception:  # noqa: BLE001
                log.exception("could not record the Proton Drive sign-in result")
            finally:
                with self._mu:
                    if self._signin is signin:
                        self._signin = None
                self._release()
                # Errors are handed back last, so the caller sees them with everything cleaned up.
                if not signin.link_given:
                    signin.handoff.put(("error", end.message or SIGN_IN_FAILED))

    def _sign_in(self, signin: _SignIn) -> _SignInEnd:
        try:
            with contextlib.ExitStack() as stack:
                self._open_vault_for_sign_in(stack)
                if signin.cancelled.is_set():
                    return _SignInEnd("cancelled", SIGN_IN_CANCELLED)
                proc = self.cli.start_login()
                # Unwound before the vault: the process is gone before the agent forgets the key.
                stack.callback(self._end_process, proc, signin)
                if proc.stderr is not None:
                    signin.drain = threading.Thread(
                        target=_drain, args=(proc.stderr, signin.tail), name="cloud-sign-in-stderr", daemon=True
                    )
                    signin.drain.start()
                with self._mu:
                    signin.proc = proc
                    cancelled = signin.cancelled.is_set()
                if cancelled:
                    return _SignInEnd("cancelled", SIGN_IN_CANCELLED)
                return self._follow_login(proc, signin)
        except VaultError as exc:
            return _SignInEnd("failed", str(exc))
        except CloudCliError as exc:
            return _SignInEnd("failed", _scrub(exc.message))

    def _open_vault_for_sign_in(self, stack: contextlib.ExitStack) -> None:
        """Enter ``vault.unlocked()``, first creating a vault if there is none or it no longer unlocks.

        This is the only place a vault is ever (re)created (spec 6.5 step 2).
        """
        if self.vault.exists():
            try:
                stack.enter_context(self.vault.unlocked())
                return
            except VaultError:
                log.warning("the stored session vault no longer unlocks; setting up a new one for this sign-in")
        self.vault.create()
        stack.enter_context(self.vault.unlocked())

    def _follow_login(self, proc: Any, signin: _SignIn) -> _SignInEnd:
        started = time.monotonic()
        signin.handoff.put(("started", None))
        kind, line = _read_first_line(proc.stdout, started + FIRST_LINE_TIMEOUT_SECONDS, signin.cancelled)
        if kind == "cancelled" or signin.cancelled.is_set():
            # Also when the read saw EOF because the canceller had just stopped the process.
            return _SignInEnd("cancelled", SIGN_IN_CANCELLED)
        url = _parse_sign_in_url(line) if kind == "line" else None
        if url is None:
            self._signal(proc, signal.SIGKILL)
            if kind == "eof":
                detail = self._stderr_line(proc, signin)
                return _SignInEnd("failed", f"{SIGN_IN_STOPPED}: {detail}" if detail else SIGN_IN_NO_LINK)
            return _SignInEnd("failed", SIGN_IN_NO_LINK if kind == "timeout" else SIGN_IN_BAD_LINK)

        with self._mu:
            signin.url = url
            signin.link_given = True
        signin.handoff.put(("url", url))
        log.info("Proton Drive sign-in started; waiting for the owner to approve it")

        code = _wait_for_exit(proc, started + SIGN_IN_DEADLINE_SECONDS, signin.cancelled)
        if signin.cancelled.is_set():
            return _SignInEnd("cancelled", SIGN_IN_CANCELLED)
        if code is None:
            self._signal(proc, signal.SIGKILL)
            return _SignInEnd("failed", SIGN_IN_TIMED_OUT)
        if code != 0:
            return _SignInEnd("failed", self._stderr_line(proc, signin) or SIGN_IN_FAILED)
        return self._connected()

    def _connected(self) -> _SignInEnd:
        """After ``auth login`` exited 0 (still under the lock and the open vault)."""
        version = None
        try:
            version = self.cli.version()
        except CloudCliError as exc:
            log.info("could not read the Proton Drive CLI version: %s", exc.kind)
        fields: dict[str, Any] = {"connection": "connected", "cli_version": version, **_PROBLEM_FIXED}
        cfg = self.store.load_config()
        if cfg.folder:
            fields.update(self._folder_check(cfg.folder))
        return _SignInEnd("connected", fields=fields, folder=cfg.folder or None)

    def _stderr_line(self, proc: Any, signin: _SignIn) -> str | None:
        """The last non-empty stderr line, cleaned like the CLI wrapper's messages."""
        with contextlib.suppress(subprocess.TimeoutExpired, OSError):
            proc.wait(timeout=KILL_WAIT_SECONDS)
        if signin.drain is not None:
            signin.drain.join(DRAIN_JOIN_SECONDS)
        return _scrub(_last_line(signin.tail.text())) or None

    def _record_sign_in(self, end: _SignInEnd) -> None:
        log.info("Proton Drive sign-in ended: %s", end.result)
        if end.result == "connected":
            self._update_state(end.fields, folder=end.folder)
        elif end.result == "cancelled":
            self._update_state({"connection": "disconnected", "last_error": None})
        else:
            self._update_state({"connection": "disconnected", "last_error": end.message or SIGN_IN_FAILED})

    def _end_process(self, proc: Any, signin: _SignIn) -> None:
        """Make sure the login process is gone and reaped, and its pipes are closed."""
        if proc.poll() is None:
            self._signal(proc, signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired, OSError):
            proc.wait(timeout=KILL_WAIT_SECONDS)
        _close(proc.stdout)
        if signin.drain is not None:
            signin.drain.join(DRAIN_JOIN_SECONDS)  # the drain thread closes stderr itself...
        if signin.drain is None or not signin.drain.is_alive():
            _close(proc.stderr)  # ...unless it never ran; never close it under a live reader

    def _signal(self, proc: Any, sig: int) -> None:
        """Signal the login's process group (it runs in its own session); fall back to the process."""
        if proc.poll() is not None:
            return  # exited and reaped: its pid may already belong to someone else
        pid = getattr(proc, "pid", None)
        if isinstance(pid, int) and pid > 1:
            try:
                self._killpg(pid, sig)
                return
            except OSError:  # ProcessLookupError, PermissionError
                pass
        with contextlib.suppress(OSError):
            if sig == signal.SIGKILL:
                proc.kill()
            else:
                proc.terminate()

    # --- disconnect ---

    def disconnect(self) -> None:
        """Log out locally and delete every secret; settings are kept (spec 6.9)."""
        with self._mu:
            pending = self._signin is not None
        if pending:
            self.cancel_sign_in()
            if not self._lock.acquire(timeout=DISCONNECT_WAIT_SECONDS):
                raise CloudBusy(BUSY)
            self._holder = "disconnect"
        else:
            self._acquire("disconnect")
        try:
            if self.vault.exists():
                try:
                    with self.vault.unlocked():
                        self.cli.logout()
                except (VaultError, CloudCliError, ValueError) as exc:
                    # Works without a usable vault too: then the files are just deleted.
                    log.info("Proton Drive logout skipped (%s); deleting the local session anyway", type(exc).__name__)
            self.vault.wipe()
            if self.vault.secrets_dir.exists():
                raise CloudActionError(WIPE_FAILED)
            self._update_state(
                {**_folder_reset(), "connection": "disconnected", "cli_version": None, "last_error": None}
            )
            log.info("Proton Drive disconnected; local session deleted")
        finally:
            self._release()

    # --- runs ---

    def start_run(self, trigger: str) -> None:
        """Start one upload-and-retention run on a worker thread (``trigger``: auto or manual)."""
        if trigger not in ("auto", "manual"):
            raise ValueError(f"unknown run trigger: {trigger!r}")
        self._acquire("run")
        try:
            cfg = self._require_ready()
            self._start(
                threading.Thread(target=self._run_worker, args=(cfg, trigger), name="cloud-run", daemon=True)
            )
        except BaseException:
            self._release()
            raise

    def due(self, now: datetime) -> bool:
        """Whether the loop should start an automatic run now (spec 6.6 "When")."""
        with self._mu:
            if self._available is not True or self._signin is not None:
                return False
        if self._lock.locked():
            return False
        cfg = self.store.load_config()
        if not cfg.enabled or not cfg.folder:
            return False
        state = self._load_state()
        if state.connection != "connected":
            return False
        retry_at = _parse_time(state.next_auto_attempt)
        if retry_at is not None and now < retry_at:
            return False
        try:
            local = list_backups(backups_dir=self.backups_dir)
        except BackupListingError:
            return True  # a run reports the unreadable backups folder (and backs off)
        known = {item.name for item in state.remote}
        today = now.astimezone(timezone.utc).date()
        return any(info.filename not in known for info in select_candidates(local, cfg, today))

    def _run_worker(self, cfg: CloudConfig, trigger: str) -> None:
        try:
            started = self._clock()
            result: RunOutcome | None = None
            try:
                result = run_sync(
                    cli=self.cli,
                    vault=self.vault,
                    cfg=cfg,
                    backups_dir=self.backups_dir,
                    today=started.astimezone(timezone.utc).date(),
                )
            except Exception:  # noqa: BLE001 - recorded as an error run below
                log.exception("cloud backup run failed unexpectedly")
            self._record_run(cfg, trigger, started, self._clock(), result)
        except Exception:  # noqa: BLE001 - a bug must never leave the lock held
            log.exception("could not record the cloud backup run")
        finally:
            self._release()

    def _record_run(
        self, cfg: CloudConfig, trigger: str, started: datetime, finished: datetime, outcome: RunOutcome | None
    ) -> None:
        if outcome is None:
            result, error = "error", RUN_UNEXPECTED
            issues: list[dict] = []
        else:
            result = outcome.result
            error = _scrub(outcome.error)
            details = {name: _scrub(text) or UPLOAD_FAILED for name, text in outcome.failure_details.items()}
            if error is None and result == "partial" and details:
                name, detail = next(iter(details.items()))
                error = f"{name}: {detail}"
            issues = [{"name": name, "kind": "conflict", "detail": CONFLICT_DETAIL} for name in outcome.conflicts]
            issues += [
                {"name": name, "kind": "failed", "detail": details.get(name) or UPLOAD_FAILED}
                for name in outcome.failed
            ]

        fields: dict[str, Any] = {"last_attempt": _iso(started), "last_result": result, "last_error": error}
        first_failure: dict[str, Any] = {}
        if result in ("ok", "warning"):
            fields.update(last_success=_iso(finished), next_auto_attempt=None, failing_since=None)
        else:
            fields["next_auto_attempt"] = _iso(finished + RETRY_AFTER_FAILURE)
            first_failure["failing_since"] = _iso(finished)  # kept if already failing
        if outcome is not None:
            fields["connection"] = outcome.connection
            if outcome.folder_status is not None:
                fields["folder_status"] = outcome.folder_status
            if outcome.listed:
                fields.update(remote=list(outcome.remote), remote_listed_at=_iso(finished))
            # An aborted run usually never judged the files: it says nothing about earlier issues.
            if result != "error" or issues:
                fields["issues"] = issues
        self._update_state(fields, folder=cfg.folder, if_unset=first_failure)

        entry = CloudLogEntry(
            started=_iso(started),
            finished=_iso(finished),
            trigger=trigger,
            result=result,
            uploaded=list(outcome.uploaded) if outcome else [],
            conflicts=list(outcome.conflicts) if outcome else [],
            failed=list(outcome.failed) if outcome else [],
            trashed=list(outcome.trashed) if outcome else [],
            error=error,
        )
        try:
            self.store.append_log(entry)
        except OSError as exc:
            log.warning("could not write the cloud backup run log: %s", exc)
        except Exception:  # noqa: BLE001
            log.exception("could not write the cloud backup run log")
        log.info("cloud backup %s run: %s", trigger, result)

    # --- the loop ---

    async def start(self) -> None:
        """Start the background loop (returns at once): probe on a thread, then a tick every 10 min."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run_loop(), name="cloud-backup-loop")

    async def stop(self) -> None:
        """Stop the loop and cancel a pending sign-in. An upload in flight is left to finish (daemon thread)."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await asyncio.to_thread(self.cancel_sign_in)

    async def _run_loop(self) -> None:
        try:
            await asyncio.to_thread(self.probe)
        except Exception:  # noqa: BLE001
            log.exception("cloud backup availability check failed")
        while True:
            try:
                await asyncio.to_thread(self.tick)
            except Exception:  # noqa: BLE001
                log.exception("cloud backup tick failed")
            await asyncio.sleep(WAKE_SECONDS)

    def tick(self) -> None:
        """One wake-up: start an automatic run if one is due. Never raises.

        While the TPM is unusable, it is first checked again once ``REPROBE_SECONDS`` have passed.
        """
        try:
            if self._reprobe_due():
                self.probe()
            if self.due(self._clock()):
                self.start_run("auto")
        except (CloudBusy, CloudActionError):
            pass  # something else got the lock, or the connection changed; next wake-up
        except Exception:  # noqa: BLE001
            log.exception("cloud backup tick failed")

    def wait_idle(self, timeout: float) -> bool:
        """Join the sign-in and run threads; ``True`` when none is left (tests, shutdown)."""
        deadline = time.monotonic() + timeout
        with self._mu:
            threads = list(self._threads)
        for thread in threads:
            thread.join(max(0.0, deadline - time.monotonic()))
        with self._mu:
            self._threads = [t for t in self._threads if t.is_alive()]
            return not self._threads

    # --- internals ---

    def _acquire(self, holder: str) -> None:
        if not self._lock.acquire(blocking=False):
            raise CloudBusy(BUSY)
        self._holder = holder

    def _release(self) -> None:
        self._holder = None
        self._lock.release()

    def _start(self, thread: threading.Thread) -> None:
        with self._mu:
            self._threads = [t for t in self._threads if t.is_alive()]
            self._threads.append(thread)
        thread.start()

    def _update_state(
        self, fields: dict[str, Any], *, folder: str | None = None, if_unset: dict[str, str] | None = None
    ) -> None:
        """Apply ``fields`` to state.json (advisory: write errors are logged, never raised).

        ``folder`` is the folder the fields were learned from: if the setting has
        changed since, the folder-specific fields are dropped (R-8). ``if_unset``
        holds timestamps that are written only when the stored one is unset or unreadable.
        When the write fails, the new state is kept in memory (``_load_state`` returns
        it) until a later write succeeds (M-4).
        """
        with self._state_mu:
            state = self._load_state()
            same_folder = folder is None or self.store.load_config().folder == folder
            for key, value in fields.items():
                if key in _FOLDER_FIELDS and not same_folder:
                    continue
                setattr(state, key, value)
            for key, value in (if_unset or {}).items():
                if key in _FOLDER_FIELDS and not same_folder:
                    continue
                if _parse_time(getattr(state, key)) is None:
                    setattr(state, key, value)
            try:
                self.store.save_state(state)
            except OSError as exc:
                log.warning("could not save the cloud backup state: %s", exc)
                self._unsaved_state = state
            except Exception:  # noqa: BLE001
                log.exception("could not save the cloud backup state")
                self._unsaved_state = state
            else:
                self._unsaved_state = None

    def _load_state(self) -> CloudState:
        """The current state: the in-memory copy while state.json cannot be written, else the file."""
        with self._state_mu:
            if self._unsaved_state is not None:
                return copy.deepcopy(self._unsaved_state)
            return self.store.load_state()


# --- the process-wide instance ---

_service: CloudService | None = None
_service_lock = threading.Lock()


def get_service() -> CloudService:
    """The one ``CloudService`` of this process, wired to the live paths on first use."""
    global _service
    with _service_lock:
        if _service is None:
            store = store_from_env()
            vault = SessionVault(store.secrets_dir)
            # One Path object for both: the CLI wrapper refuses an archive path that is not
            # exactly resolve_backup_archive(paths.backups_dir, name).
            backups_dir = Path(os.environ.get("AC_STACK_DIR", "/ac")) / "backups"
            cli = ProtonDriveCli(
                CliPaths(
                    binary=default_binary(),
                    cache_dir=vault.cli_dir,
                    gnupg_home=vault.gnupg_home,
                    store_dir=vault.store_dir,
                    backups_dir=backups_dir,
                )
            )
            _service = CloudService(store, backups_dir=backups_dir, vault=vault, cli=cli)
        return _service
