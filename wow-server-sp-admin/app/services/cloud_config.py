"""Settings, folder rules and the small JSON stores for the Proton Drive backup copy.

Everything lives in ``<ADMIN_DATA_DIR>/cloud/`` (mode 700): ``config.json``
(what the owner chose), ``state.json`` (what the last run saw) and
``log.jsonl`` (the last 20 runs). All three are mode 600 and written with
tmp + ``os.replace``; an unreadable or invalid file is treated as defaults
with a warning, so a bad file can never wedge the Backups page. The
``secrets/`` sub-directory is created and filled by the vault code, never here.

The folder rules are the confinement rules of the CLI wrapper (spec 6.4): the
configured path is the only place the app will ever list, upload to or trash
from, so it is validated on save and again before every run.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from contextlib import suppress
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# The kinds of archive backups.py recognises, in the order the UI lists them.
ALL_LABELS: tuple[str, ...] = (
    "daily", "manual", "prerestore", "preclear", "preupgrade", "prerollback", "imported",
)
# Rollback points that retention never removes (backup.sh never prunes them either).
PROTECTED_LABELS: frozenset[str] = frozenset({"preupgrade", "prerollback"})

RETENTION_MIN = 1
RETENTION_MAX = 90
DEFAULT_RETENTION_DAYS = 7

FOLDER_ROOTS = ("/my-files/", "/shared-with-me/")
MAX_SEGMENT_BYTES = 255

CONNECTION_STATES = frozenset({"disconnected", "connected", "vault_error"})
FOLDER_STATUSES = frozenset({None, "ok", "missing", "not_folder"})
ISSUE_KINDS = frozenset({"conflict", "failed"})

# The Proton CLI resolves a path segment of this shape as a node UID instead of
# a name (cli/src/cli/paths.ts, isNodeUid), which would let a configured path
# jump out of the intended folder. Such a segment is never a valid folder name.
_NODE_UID_PART = r"(?:[a-zA-Z0-9=_-]{88,108}|[a-zA-Z0-9_-]{22})"
_NODE_UID_RE = re.compile(_NODE_UID_PART + "~" + _NODE_UID_PART)

_FOLDER_HINT = (
    "Use a path like /my-files/WoW Backups (it must start with /my-files/ or "
    "/shared-with-me/ and name a folder inside it)."
)


# --- folder rules ---


def normalize_folder(raw: str) -> str:
    """Return the canonical form of a Drive folder path, or raise ``ValueError``.

    Must start with ``/my-files/`` or ``/shared-with-me/`` and have at least one
    more segment (never the section root itself). Segments are non-empty, not
    ``.``/``..``, free of backslashes and control characters, at most 255 UTF-8
    bytes, and not shaped like a node UID. One trailing ``/`` is dropped.
    """
    if not isinstance(raw, str):
        raise ValueError("The Drive folder must be text. " + _FOLDER_HINT)
    path = raw[:-1] if raw.endswith("/") else raw
    prefix = next((p for p in FOLDER_ROOTS if path.startswith(p)), None)
    if prefix is None:
        raise ValueError("That is not a usable Drive folder. " + _FOLDER_HINT)
    for segment in path[len(prefix):].split("/"):
        _check_segment(segment)
    return path


def _check_segment(segment: str) -> None:
    if segment == "":
        raise ValueError("The Drive folder path has an empty part (a doubled or trailing slash).")
    if segment in (".", ".."):
        raise ValueError("The Drive folder path cannot contain '.' or '..'.")
    if "\\" in segment:
        raise ValueError("The Drive folder path cannot contain a backslash.")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in segment):
        raise ValueError("The Drive folder path cannot contain control characters.")
    try:
        too_long = len(segment.encode("utf-8")) > MAX_SEGMENT_BYTES
    except UnicodeEncodeError:
        raise ValueError("The Drive folder path contains characters that cannot be stored.") from None
    if too_long:
        raise ValueError(f"A folder name in the path is longer than {MAX_SEGMENT_BYTES} bytes.")
    if _NODE_UID_RE.fullmatch(segment):
        raise ValueError(
            "A folder name in the path looks like an internal Proton ID; pick a normal folder name."
        )


def split_folder(folder: str) -> tuple[str, str]:
    """Split a folder path into (parent, leaf), e.g. for ``fs create-folder``."""
    parent, _, leaf = normalize_folder(folder).rpartition("/")
    return parent, leaf


# --- settings ---


def _canonical_labels(labels: Any) -> tuple[str, ...]:
    return tuple(label for label in ALL_LABELS if label in labels)


def _is_int(value: Any) -> bool:
    # bool is an int subclass; "True days" is never what anyone meant.
    return isinstance(value, int) and not isinstance(value, bool)


@dataclass(frozen=True)
class CloudConfig:
    enabled: bool = False
    folder: str = ""
    labels: tuple[str, ...] = ALL_LABELS
    retention_days: int = DEFAULT_RETENTION_DAYS

    @classmethod
    def from_dict(cls, raw: Any) -> "CloudConfig":
        """Parse settings from JSON-shaped data; raises ``ValueError`` on anything off.

        Types, labels and the folder are checked here; ``validate()`` adds the
        range check and the "folder required when enabled" rule.
        """
        if not isinstance(raw, dict):
            raise ValueError("cloud backup settings must be an object")
        enabled = raw.get("enabled", False)
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be true or false")
        folder = raw.get("folder", "")
        if not isinstance(folder, str):
            raise ValueError("folder must be text")
        labels = raw.get("labels", list(ALL_LABELS))
        if not isinstance(labels, (list, tuple)) or not all(isinstance(x, str) for x in labels):
            raise ValueError("labels must be a list of backup kinds")
        retention = raw.get("retention_days", DEFAULT_RETENTION_DAYS)
        if not _is_int(retention):
            raise ValueError("retention_days must be a whole number")
        cfg = cls(enabled=enabled, folder=folder, labels=tuple(labels), retention_days=retention)
        cfg._check_labels()
        return cfg.canonical()

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready form in canonical shape (raises ``ValueError`` if the folder is invalid)."""
        cfg = self.canonical()
        return {
            "enabled": cfg.enabled,
            "folder": cfg.folder,
            "labels": list(cfg.labels),
            "retention_days": cfg.retention_days,
        }

    def canonical(self) -> "CloudConfig":
        """Same settings with the folder normalised and labels deduplicated in ``ALL_LABELS`` order."""
        return replace(
            self,
            folder=normalize_folder(self.folder) if self.folder else "",
            labels=_canonical_labels(self.labels),
        )

    def validate(self) -> None:
        """Raise ``ValueError`` (plain-language message) if these settings cannot be saved."""
        if not isinstance(self.enabled, bool):
            raise ValueError("enabled must be true or false")
        if not isinstance(self.folder, str):
            raise ValueError("folder must be text")
        if self.folder:
            normalize_folder(self.folder)
        elif self.enabled:
            raise ValueError("Choose a Proton Drive folder before turning automatic uploads on.")
        self._check_labels()
        if not _is_int(self.retention_days) or not RETENTION_MIN <= self.retention_days <= RETENTION_MAX:
            raise ValueError(
                f"Keep-for days must be a whole number from {RETENTION_MIN} to {RETENTION_MAX}."
            )

    def _check_labels(self) -> None:
        if not isinstance(self.labels, (list, tuple)):
            raise ValueError("labels must be a list of backup kinds")
        for label in self.labels:
            if label not in ALL_LABELS:
                raise ValueError(f"Unknown backup kind: {label!r}")


# --- state (what the last run saw) ---


def _opt_str(raw: dict[str, Any], key: str) -> str | None:
    value = raw.get(key)
    if value is not None and not isinstance(value, str):
        raise ValueError(f"{key} must be text or null")
    return value


@dataclass
class RemoteFile:
    name: str
    size: int | None = None
    sha1: str | None = None

    @classmethod
    def from_dict(cls, raw: Any) -> "RemoteFile":
        if not isinstance(raw, dict):
            raise ValueError("remote entry must be an object")
        name = raw.get("name")
        if not isinstance(name, str):
            raise ValueError("remote entry needs a name")
        size = raw.get("size")
        if size is not None and not _is_int(size):
            raise ValueError("remote size must be a whole number or null")
        return cls(name=name, size=size, sha1=_opt_str(raw, "sha1"))


def _issue_from_dict(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise ValueError("issue must be an object")
    name, kind, detail = raw.get("name"), raw.get("kind"), raw.get("detail")
    if not isinstance(name, str) or not isinstance(detail, str) or kind not in ISSUE_KINDS:
        raise ValueError("issue needs a name, a kind (conflict or failed) and a detail")
    return {"name": name, "kind": kind, "detail": detail}


@dataclass
class CloudState:
    connection: str = "disconnected"
    last_attempt: str | None = None
    last_success: str | None = None
    last_result: str | None = None
    last_error: str | None = None
    next_auto_attempt: str | None = None
    folder_status: str | None = None
    remote: list[RemoteFile] = field(default_factory=list)
    remote_listed_at: str | None = None
    issues: list[dict] = field(default_factory=list)
    cli_version: str | None = None

    @classmethod
    def from_dict(cls, raw: Any) -> "CloudState":
        if not isinstance(raw, dict):
            raise ValueError("cloud state must be an object")
        connection = raw.get("connection", "disconnected")
        if connection not in CONNECTION_STATES:
            raise ValueError("unknown connection state")
        folder_status = raw.get("folder_status")
        if folder_status not in FOLDER_STATUSES:
            raise ValueError("unknown folder status")
        remote = raw.get("remote", [])
        issues = raw.get("issues", [])
        if not isinstance(remote, list) or not isinstance(issues, list):
            raise ValueError("remote and issues must be lists")
        return cls(
            connection=connection,
            last_attempt=_opt_str(raw, "last_attempt"),
            last_success=_opt_str(raw, "last_success"),
            last_result=_opt_str(raw, "last_result"),
            last_error=_opt_str(raw, "last_error"),
            next_auto_attempt=_opt_str(raw, "next_auto_attempt"),
            folder_status=folder_status,
            remote=[RemoteFile.from_dict(item) for item in remote],
            remote_listed_at=_opt_str(raw, "remote_listed_at"),
            issues=[_issue_from_dict(item) for item in issues],
            cli_version=_opt_str(raw, "cli_version"),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --- run log ---


def _str_list(raw: dict[str, Any], key: str) -> list[str]:
    value = raw.get(key)
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise ValueError(f"{key} must be a list of names")
    return list(value)


@dataclass(frozen=True)
class CloudLogEntry:
    started: str
    finished: str
    trigger: str
    result: str
    uploaded: list[str]
    conflicts: list[str]
    failed: list[str]
    trashed: list[str]
    error: str | None

    @classmethod
    def from_dict(cls, raw: Any) -> "CloudLogEntry":
        if not isinstance(raw, dict):
            raise ValueError("log entry must be an object")
        text = {}
        for key in ("started", "finished", "trigger", "result"):
            value = raw.get(key)
            if not isinstance(value, str):
                raise ValueError(f"{key} must be text")
            text[key] = value
        return cls(
            uploaded=_str_list(raw, "uploaded"),
            conflicts=_str_list(raw, "conflicts"),
            failed=_str_list(raw, "failed"),
            trashed=_str_list(raw, "trashed"),
            error=_opt_str(raw, "error"),
            **text,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --- the store ---


class CloudStore:
    """``cloud/`` under ADMIN_DATA_DIR. Regular files, so tmp + ``os.replace`` is safe."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.root = data_dir / "cloud"
        self.secrets_dir = self.root / "secrets"
        self.config_path = self.root / "config.json"
        self.state_path = self.root / "state.json"
        self.log_path = self.root / "log.jsonl"
        # Reentrant: append_log holds it across its read-modify-write, which ends in a write.
        self._lock = threading.RLock()

    # config

    def load_config(self) -> CloudConfig:
        try:
            cfg = CloudConfig.from_dict(self._read_json(self.config_path))
            cfg.validate()
            return cfg
        except FileNotFoundError:
            return CloudConfig()
        except (OSError, ValueError, TypeError):
            log.warning("cloud backup config %s unreadable or invalid; using defaults", self.config_path)
            return CloudConfig()

    def save_config(self, cfg: CloudConfig) -> None:
        cfg.validate()
        self._write_private(self.config_path, _dump(cfg.to_dict()))

    # state

    def load_state(self) -> CloudState:
        try:
            return CloudState.from_dict(self._read_json(self.state_path))
        except FileNotFoundError:
            return CloudState()
        except (OSError, ValueError, TypeError):
            log.warning("cloud backup state %s unreadable or invalid; using defaults", self.state_path)
            return CloudState()

    def save_state(self, state: CloudState) -> None:
        self._write_private(self.state_path, _dump(state.to_dict()))

    # run log (JSON lines, oldest first on disk)

    def append_log(self, entry: CloudLogEntry, limit: int = 20) -> None:
        with self._lock:
            rows = self._read_log_chronological()
            rows.append(entry)
            rows = rows[-max(limit, 1):]
            text = "".join(json.dumps(row.to_dict(), sort_keys=True) + "\n" for row in rows)
            self._write_private(self.log_path, text)

    def read_log(self) -> list[CloudLogEntry]:
        """Newest first."""
        return list(reversed(self._read_log_chronological()))

    def _read_log_chronological(self) -> list[CloudLogEntry]:
        try:
            lines = self.log_path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            return []
        entries: list[CloudLogEntry] = []
        skipped = 0
        for line in lines:
            if not line.strip():
                continue
            try:
                entries.append(CloudLogEntry.from_dict(json.loads(line)))
            except ValueError:
                skipped += 1
        if skipped:
            log.warning("skipped %d unreadable line(s) in %s", skipped, self.log_path)
        return entries

    # files

    @staticmethod
    def _read_json(path: Path) -> Any:
        return json.loads(path.read_text(encoding="utf-8"))

    def _ensure_root(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        # mkdir's mode is masked by the umask and ignored for an existing dir.
        os.chmod(self.root, 0o700)

    def _write_private(self, path: Path, text: str) -> None:
        with self._lock:
            self._ensure_root()
            tmp = path.with_name(path.name + ".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    os.fchmod(fh.fileno(), 0o600)  # also fixes a stale tmp left with another mode
                    fh.write(text)
                os.replace(tmp, path)
            except BaseException:
                with suppress(OSError):
                    tmp.unlink()
                raise


def _dump(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2, sort_keys=True) + "\n"


def store_from_env() -> CloudStore:
    return CloudStore(Path(os.environ.get("ADMIN_DATA_DIR", "/admin-data")))
