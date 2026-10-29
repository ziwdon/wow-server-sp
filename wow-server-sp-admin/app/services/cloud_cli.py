"""The one place the admin app runs the Proton Drive command-line tool.

``ProtonDriveCli`` can only express the invocations in spec 6.4: sign in/out,
version, ``fs info``, ``fs list``, ``fs upload -f skip``, ``fs trash`` of one
backup-named file, and ``fs create-folder``. There is no generic "run this"
method, and the private runner refuses any other command, so downloads,
deletes, ``empty-trash``, renames and conflict strategies other than ``skip``
cannot be reached from the rest of the code.

Everything the CLI is told comes from validated values: folders go through
``cloud_config.normalize_folder``, an uploaded file must be a regular backup
archive directly inside the backups directory (``resolve_backup_archive``), and
a trashed name must pass ``cloud_plan.is_backup_name``. The environment is
built fresh (the app's own is never inherited) and stdin is ``/dev/null``.

Output is read as JSON (spec 2a). Anything unreadable fails closed as a
``"parse"`` error instead of being guessed at; ``CloudCliError.message`` is
short, single-line and free of control characters, so it is safe to show.
"""

from __future__ import annotations

import json
import os
import pwd
import re
import subprocess
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.services.backups import resolve_backup_archive
from app.services.cloud_config import normalize_folder, split_folder
from app.services.cloud_plan import RemoteEntry, is_backup_name

DEFAULT_BINARY = "/usr/local/bin/proton-drive"
SHORT_TIMEOUT_SECONDS = 120
UPLOAD_TIMEOUT_SECONDS = 3600
MAX_MESSAGE_CHARS = 300
CHILD_UMASK = 0o077  # spec 6.2: the CLI's cache, logs and session files are created private

# The first one or two argv words of every command the app may run (spec 6.4).
_ALLOWED_COMMANDS = frozenset(
    {
        ("auth", "login"),
        ("auth", "logout"),
        ("version",),
        ("fs", "info"),
        ("fs", "list"),
        ("fs", "upload"),
        ("fs", "trash"),
        ("fs", "create-folder"),
    }
)

_AUTH_TEXT = "You need to login first"  # cli/src/cli/errors.ts, AuthRequiredError
_NOT_FOUND_TEXT = "node not found"  # "Node not found: <name>" / "Root node not found" (cli/src/cli/paths.ts)

# Terminal escapes (CSI and OSC) that can ride along in a message built from a file name.
_ESCAPE_SEQUENCES = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?")
_WHITESPACE_CONTROLS = frozenset("\t\n\r\x0b\x0c")
_PARSE_MESSAGE = "Proton Drive returned an answer the app could not read."


# --- errors ---


def _clean(text: Any) -> str:
    """One line, no control characters (escape sequences included), at most 300 characters."""
    text = _ESCAPE_SEQUENCES.sub("", str(text))
    kept = []
    for ch in text:
        if ch in _WHITESPACE_CONTROLS:
            kept.append(" ")
        elif unicodedata.category(ch) not in ("Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"):
            kept.append(ch)
    cleaned = " ".join("".join(kept).split())
    if len(cleaned) > MAX_MESSAGE_CHARS:
        cleaned = cleaned[: MAX_MESSAGE_CHARS - 1].rstrip() + "…"
    return cleaned


class CloudCliError(Exception):
    """A CLI run that did not give a usable answer.

    ``kind``: ``"auth"`` (not signed in), ``"not_found"`` (a Drive path does not
    exist), ``"timeout"``, ``"parse"`` (output was not what the CLI documents) or
    ``"failed"`` (anything else, including a binary that cannot run).
    """

    def __init__(self, kind: str, message: str) -> None:
        self.kind = kind
        self.message = _clean(message) or "Proton Drive reported an error."
        super().__init__(self.message)


# --- value types ---


@dataclass(frozen=True)
class CliPaths:
    binary: Path
    cache_dir: Path
    gnupg_home: Path
    store_dir: Path
    backups_dir: Path


@dataclass(frozen=True)
class UploadSummary:
    transferred: int
    skipped: int
    failed: int
    failures: list[str]


def default_binary() -> Path:
    return Path(os.environ.get("PROTON_DRIVE_BIN") or DEFAULT_BINARY)


# --- output parsing ---


def _load_json(text: str | None) -> Any:
    try:
        return json.loads(text or "")
    except (ValueError, RecursionError):
        raise CloudCliError("parse", _PARSE_MESSAGE) from None


def _opt_int(value: Any) -> int | None:
    # bool is an int subclass; a "size" of true is not a size.
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _opt_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def parse_node(raw: Any) -> RemoteEntry:
    """Turn one node object from ``fs list``/``fs info``/``fs create-folder`` into a ``RemoteEntry``.

    A name that is not ``{"ok": true, "value": <text>}`` is ``None`` (the CLI
    could not decrypt it); a missing revision, size or digest is ``None``. A
    value of the wrong type is treated as missing, never as a crash.
    """
    if not isinstance(raw, dict):
        raise CloudCliError("parse", _PARSE_MESSAGE)
    name_field = _dict(raw.get("name"))
    name = _opt_str(name_field.get("value")) if name_field.get("ok") is True else None
    node_type = raw.get("type")
    revision = _dict(raw.get("activeRevision"))
    return RemoteEntry(
        name=name,
        type=node_type if isinstance(node_type, str) and node_type else "unknown",
        uid=_opt_str(raw.get("uid")) or "",
        size=_opt_int(revision.get("claimedSize")),
        sha1=_opt_str(_dict(revision.get("claimedDigests")).get("sha1")),
    )


def _read_upload_summary(stdout: str | None) -> UploadSummary | None:
    """The upload's one-line JSON summary, or ``None`` if it is missing or not shaped as documented."""
    try:
        data = json.loads(stdout or "")
    except (ValueError, RecursionError):
        return None
    if not isinstance(data, dict):
        return None
    counts = {}
    for key in ("transferredItems", "skippedItems", "failedItems"):
        value = _opt_int(data.get(key))
        if value is None or value < 0:
            return None
        counts[key] = value
    if _opt_int(data.get("transferredBytes")) is None:
        return None
    failures = data.get("failures")
    if not isinstance(failures, list):
        return None
    # "<ErrName>: <message>" per failed item; a malformed item is skipped, not fatal.
    errors = [item["error"] for item in failures if isinstance(item, dict) and isinstance(item.get("error"), str)]
    return UploadSummary(
        transferred=counts["transferredItems"],
        skipped=counts["skippedItems"],
        failed=counts["failedItems"],
        failures=errors,
    )


def _last_line(text: str | None) -> str:
    for line in reversed((text or "").replace("\r", "\n").split("\n")):
        cleaned = _clean(line)
        if cleaned:
            return cleaned
    return ""


def _exit_error(proc: subprocess.CompletedProcess) -> CloudCliError:
    stderr, stdout = proc.stderr or "", proc.stdout or ""
    combined = f"{stderr}\n{stdout}"
    if _AUTH_TEXT in combined:
        kind = "auth"
    elif _NOT_FOUND_TEXT in combined.lower():
        kind = "not_found"
    else:
        kind = "failed"
    message = _last_line(stderr) or f"Proton Drive command failed (exit status {proc.returncode})."
    return CloudCliError(kind, message)


def _home_dir(fallback: Path) -> str:
    home = os.environ.get("HOME")
    if home:
        return home
    try:
        return pwd.getpwuid(os.getuid()).pw_dir
    except KeyError:
        # The container may run as a uid with no passwd entry; use a directory the app owns.
        return str(fallback)


# --- the wrapper ---


class ProtonDriveCli:
    """Allowlisting wrapper around the ``proton-drive`` binary (see the module docstring)."""

    def __init__(
        self,
        paths: CliPaths,
        run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        popen: Callable[..., subprocess.Popen] = subprocess.Popen,
    ) -> None:
        self.paths = paths
        self._run = run
        self._popen = popen

    def env(self) -> dict[str, str]:
        """A fresh, minimal environment for the CLI; nothing is inherited from the app."""
        p = self.paths
        return {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": _home_dir(p.cache_dir),
            "LANG": "C.UTF-8",
            "PROTON_DRIVE_CREDENTIALS_STORE": "pass",
            "PROTON_DRIVE_CACHE_DIR": str(p.cache_dir),
            "GNUPGHOME": str(p.gnupg_home),
            "PASSWORD_STORE_DIR": str(p.store_dir),
            "PASSWORD_STORE_GPG_OPTS": "--batch --pinentry-mode error",
            "PROTON_DRIVE_LOG_LEVEL": "INFO",
            "TPM2TOOLS_TCTI": "device:/dev/tpmrm0",
        }

    # --- commands ---

    def version(self) -> str:
        """The first non-empty line of ``proton-drive version``."""
        proc = self._ok(["version"], SHORT_TIMEOUT_SECONDS)
        for raw in (proc.stdout or "").splitlines():
            line = _clean(raw)
            if line:
                return line
        raise CloudCliError("parse", "Proton Drive did not report a version.")

    def info(self, folder: str) -> RemoteEntry:
        proc = self._ok(["fs", "info", "--json", normalize_folder(folder)], SHORT_TIMEOUT_SECONDS)
        return parse_node(_load_json(proc.stdout))

    def list_folder(self, folder: str) -> list[RemoteEntry]:
        """Everything directly inside ``folder`` (the folder only, never a parent)."""
        proc = self._ok(["fs", "list", "--json", normalize_folder(folder)], SHORT_TIMEOUT_SECONDS)
        items = _load_json(proc.stdout)
        if not isinstance(items, list):
            raise CloudCliError("parse", _PARSE_MESSAGE)
        return [parse_node(item) for item in items]

    def upload(self, archive: Path, folder: str) -> UploadSummary:
        """Copy one local backup archive into ``folder``; never replaces a same-named file.

        Raises ``ValueError`` unless ``archive`` is a regular backup archive
        sitting directly in the backups directory (no symlink, no other spelling
        of the path). Failed items raise ``CloudCliError("failed")``.
        """
        target = normalize_folder(folder)
        self._require_local_archive(archive)
        proc = self._invoke(
            ["fs", "upload", "--json", "-f", "skip", "-t", str(archive), target], UPLOAD_TIMEOUT_SECONDS
        )
        # The summary is printed even when items failed (exit 1), so read it before the exit status.
        summary = _read_upload_summary(proc.stdout)
        if summary is not None and summary.failed > 0:
            detail = f"{summary.failed} item(s) failed to upload"
            raise CloudCliError("failed", f"{detail}: {summary.failures[0]}" if summary.failures else detail)
        if proc.returncode != 0:
            raise _exit_error(proc)
        if summary is None:
            raise CloudCliError("parse", _PARSE_MESSAGE)
        return summary

    def trash(self, folder: str, name: str) -> None:
        """Move one backup-named file of ``folder`` to the Drive trash (it can be restored there)."""
        if not is_backup_name(name):
            raise ValueError("Only backup archive files can be moved to the Drive trash.")
        path = f"{normalize_folder(folder)}/{name}"
        proc = self._ok(["fs", "trash", "--json", path], SHORT_TIMEOUT_SECONDS)
        results = _load_json(proc.stdout)
        if not isinstance(results, list):
            raise CloudCliError("parse", _PARSE_MESSAGE)
        # Exit status is 0 even when an item is not ok, so the result itself must say so.
        if len(results) != 1 or not isinstance(results[0], dict) or results[0].get("ok") is not True:
            raise CloudCliError("failed", f"Proton Drive did not move {name} to the trash.")

    def create_folder(self, folder: str) -> RemoteEntry:
        parent, leaf = split_folder(folder)
        if leaf.startswith("-"):
            # The CLI would read it as an option, not a name.
            raise ValueError("A folder name cannot start with '-'; create it in Proton Drive instead.")
        proc = self._ok(["fs", "create-folder", "--json", parent, leaf], SHORT_TIMEOUT_SECONDS)
        return parse_node(_load_json(proc.stdout))

    def logout(self) -> None:
        """Delete the CLI's local session (it does not end the session at Proton)."""
        self._ok(["auth", "logout"], SHORT_TIMEOUT_SECONDS)

    def start_login(self) -> subprocess.Popen:
        """Start ``auth login --json`` and hand back the running process.

        Its first stdout line is ``{"signInUrl": ...}``; it then waits (up to
        10 minutes) for the owner to approve the sign-in. The caller reads and
        eventually reaps the process.
        """
        args = ["auth", "login", "--json"]
        _check_allowed(args)
        try:
            return self._popen(
                [str(self.paths.binary), *args],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=self.env(),
                start_new_session=True,
                umask=CHILD_UMASK,
            )
        except OSError:
            raise _cannot_run() from None

    # --- internals ---

    def _require_local_archive(self, archive: Path) -> None:
        if not isinstance(archive, Path) or not archive.is_absolute() or not is_backup_name(archive.name):
            raise ValueError("Only a backup archive file can be uploaded.")
        resolved = resolve_backup_archive(backups_dir=self.paths.backups_dir, archive_name=archive.name)
        if resolved is None or resolved != archive:
            raise ValueError("That file is not a backup archive in the backups folder.")

    def _invoke(self, args: list[str], timeout: int) -> subprocess.CompletedProcess:
        """Run one allowed command; spawn failures and timeouts become ``CloudCliError``."""
        _check_allowed(args)
        try:
            return self._run(
                [str(self.paths.binary), *args],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                env=self.env(),
                umask=CHILD_UMASK,
            )
        except subprocess.TimeoutExpired:
            raise CloudCliError(
                "timeout", f"Proton Drive did not answer within {max(timeout // 60, 1)} minute(s)."
            ) from None
        except OSError:
            raise _cannot_run() from None

    def _ok(self, args: list[str], timeout: int) -> subprocess.CompletedProcess:
        proc = self._invoke(args, timeout)
        if proc.returncode != 0:
            raise _exit_error(proc)
        return proc


def _check_allowed(args: list[str]) -> None:
    if tuple(args[:2]) not in _ALLOWED_COMMANDS:
        raise RuntimeError("blocked: that Proton Drive command is not on the allowlist")


def _cannot_run() -> CloudCliError:
    return CloudCliError("failed", "The Proton Drive command-line tool is missing or cannot be run.")
