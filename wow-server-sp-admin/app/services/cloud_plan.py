"""Pure decisions for the Proton Drive backup copy: what to upload, what is already
there, and what the Drive may drop.

No I/O and no clock: callers pass ``today`` (UTC) and the listings. Everything
here is about *names*. A name is only trusted after ``is_backup_name`` -- the
upload and trash paths are built as ``<folder>/<name>`` later, and
``backups._ARCHIVE_RE``'s ``(.+)`` alone would let a ``/`` through.

Retention mirrors ``backup.sh`` (daily mode prunes with ``find -mtime +7``): the
age is counted in days from the date in the *file name*, so it can be decided
from the Drive listing alone, and ``preupgrade``/``prerollback`` are never aged
out. Whenever a decision cannot be made with confidence the answer is "keep".
"""

from __future__ import annotations

import datetime as dt
import re
from collections import Counter
from dataclasses import dataclass

from app.services.backups import _ARCHIVE_RE, BackupInfo
from app.services.cloud_config import PROTECTED_LABELS, CloudConfig

# Stamps as written by scripts/backup.sh (`date +%F` for daily, `date +%FT%H-%M-%S`
# for every other label) and app/main.py (`int(now().timestamp())` + `uuid4().hex`).
# [0-9], not \d: \d would also accept non-ASCII digits.
_DAILY_STAMP = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})")
_TIMED_STAMP = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2})-([0-9]{2})-([0-9]{2})")
_IMPORTED_STAMP = re.compile(r"([0-9]{1,12})-[0-9a-fA-F]+")

_UTC = dt.timezone.utc


@dataclass(frozen=True)
class RemoteEntry:
    """One item of a Drive folder listing. ``name is None`` = the CLI could not decrypt it."""

    name: str | None
    type: str  # "file" or "folder"
    uid: str
    size: int | None
    sha1: str | None


def is_backup_name(name: str | None) -> bool:
    """True only for a plain backup archive file name that is safe inside a Drive path."""
    if not isinstance(name, str) or _ARCHIVE_RE.fullmatch(name) is None:
        return False
    if "/" in name or "\\" in name or ".." in name:
        return False
    return not any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in name)


def _safe_match(name: str | None) -> re.Match[str] | None:
    return _ARCHIVE_RE.fullmatch(name) if isinstance(name, str) and is_backup_name(name) else None


def archive_label(name: str | None) -> str | None:
    """The backup kind in an archive name (``daily``, ``manual``, ...), else ``None``."""
    match = _safe_match(name)
    return match.group(1) if match else None


def archive_stamp_date(name: str | None) -> dt.date | None:
    """The calendar date a backup name says it was made on (UTC), or ``None`` if unreadable.

    ``daily-YYYY-MM-DD``, ``<label>-YYYY-MM-DDTHH-MM-SS`` and
    ``imported-<epoch>-<hex>`` (epoch read as UTC). An impossible date such as
    2026-02-30, or any other shape, gives ``None``.
    """
    match = _safe_match(name)
    if match is None:
        return None
    label, stamp = match.group(1), match.group(2)
    try:
        if label == "daily":
            parts = _DAILY_STAMP.fullmatch(stamp)
            return dt.date(*map(int, parts.groups())) if parts else None
        if label == "imported":
            parts = _IMPORTED_STAMP.fullmatch(stamp)
            if parts is None:
                return None
            return dt.datetime.fromtimestamp(int(parts.group(1)), tz=_UTC).date()
        parts = _TIMED_STAMP.fullmatch(stamp)
        if parts is None:
            return None
        year, month, day, hour, minute, second = map(int, parts.groups())
        return dt.datetime(year, month, day, hour, minute, second).date()
    except (ValueError, OverflowError, OSError):
        return None


def _created_date(info: BackupInfo) -> dt.date:
    created = info.created
    if created.tzinfo is not None:
        created = created.astimezone(_UTC)
    return created.date()


def select_candidates(local: list[BackupInfo], cfg: CloudConfig, today: dt.date) -> list[BackupInfo]:
    """Local archives worth having in the Drive, newest first.

    A candidate has a selected label and is either a protected rollback point
    (any age) or within ``retention_days`` -- the same cut-off ``plan_retention``
    uses, so nothing is uploaded only to be trashed straight away. Age comes
    from the name's stamp, else from the file's modification date.
    """
    selected = set(cfg.labels)
    out: list[BackupInfo] = []
    for info in local:
        label = archive_label(info.filename)
        if label is None or label not in selected:
            continue
        if label not in PROTECTED_LABELS:
            stamp = archive_stamp_date(info.filename) or _created_date(info)
            if (today - stamp).days > cfg.retention_days:
                continue
        out.append(info)
    out.sort(key=lambda b: b.created, reverse=True)
    return out


def classify(candidate: BackupInfo, remote_by_name: dict[str, RemoteEntry], local_sha1: str) -> str:
    """``missing`` (not in the Drive), ``present`` (same content) or ``conflict`` (same name, other content).

    Content is compared by SHA-1 when the Drive reports one, else by size. A
    size that cannot be compared, or a folder of that name, is a conflict:
    "present" is only claimed when it is proven.
    """
    remote = remote_by_name.get(candidate.filename)
    if remote is None:
        return "missing"
    if remote.type != "file":
        return "conflict"
    if remote.sha1 is not None:
        return "present" if remote.sha1.lower() == local_sha1.lower() else "conflict"
    if remote.size is not None and remote.size == candidate.size_bytes:
        return "present"
    return "conflict"


def plan_retention(entries: list[RemoteEntry], cfg: CloudConfig, today: dt.date) -> list[str]:
    """Names in the Drive listing that retention may move to the trash, oldest first.

    Only plain backup files are considered (never folders, undecryptable or
    foreign names, protected labels, or names whose date cannot be read). An
    entry goes when its name's date is more than ``retention_days`` before
    ``today``, except the newest remaining archive (and anything sharing its
    date), so the Drive is never emptied by a long outage. A name that appears
    more than once in the listing is never trashed: ``<folder>/<name>`` would
    not say which one.
    """
    seen = Counter(e.name for e in entries if e.name is not None)
    eligible: list[tuple[dt.date, str]] = []
    for entry in entries:
        name = entry.name
        if entry.type != "file" or name is None:
            continue
        label = archive_label(name)
        if label is None or label in PROTECTED_LABELS:
            continue
        stamp = archive_stamp_date(name)
        if stamp is None:
            continue
        eligible.append((stamp, name))
    if not eligible:
        return []
    newest = max(stamp for stamp, _ in eligible)
    doomed = {
        (stamp, name)
        for stamp, name in eligible
        if stamp < newest and (today - stamp).days > cfg.retention_days and seen[name] == 1
    }
    return [name for _, name in sorted(doomed)]
