"""One upload-and-retention run for the Proton Drive backup copy (spec 6.6).

``run_sync`` composes the pieces that already hold the rules -- candidate
selection, content comparison and retention planning (``cloud_plan``), the
allowlisted CLI wrapper (``cloud_cli``) and the session vault (``cloud_vault``)
-- and turns one pass over the Drive folder into a ``RunOutcome``. It decides
nothing about *when* to run, keeps no state and writes nothing to disk; the
caller owns the lock, the schedule, ``state.json`` and the run log.

The run, with the vault open only for its duration:

1. ``fs info`` the folder (must exist and be a folder);
2. ``fs list`` it;
3. upload every candidate the listing lacks, newest first, then (only if an
   upload was attempted) list again and judge each candidate by that listing;
4. only if the newest candidate is confirmed present, trash what retention
   allows from the final listing, then (if any trash call was made) list once
   more: a file the CLI reported trashed but that is still listed was not
   trashed (spec 2a: "check ok, then re-list to confirm");
5. report.

Rules that keep this safe:

* A pre-existing same-name file with other content is a *conflict*: reported,
  never replaced, never trashed to make room.
* Only the listing decides ``present``/``conflict``; what the CLI claims about
  an upload (even a clean exit) proves nothing. A failed upload that the
  listing shows complete counts as uploaded.
* A problem with one file never stops the others. A problem that makes the
  whole run untrustworthy (not signed in, folder gone, a listing that cannot
  be read, the local backups unreadable) aborts it, and an abort never reaches
  the trash step.
* Unexpected exceptions (bugs) are not caught here; the vault is locked again
  on the way out and the caller sees the exception.

Error text in an outcome is always a ``CloudCliError.message``, a
``VaultError`` message or a plain sentence written here -- never an exception
repr and never anything read from the session.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from app.services.backups import BackupListingError, list_backups, resolve_backup_archive
from app.services.cloud_cli import CloudCliError, ProtonDriveCli
from app.services.cloud_config import CloudConfig, RemoteFile, normalize_folder
from app.services.cloud_plan import RemoteEntry, classify, is_backup_name, plan_retention, select_candidates
from app.services.cloud_vault import SessionVault, VaultError

_CHUNK_BYTES = 1024 * 1024

_FOLDER_MISSING = "The Drive folder was not found. Create it in Proton Drive or choose another folder."
_NOT_A_FOLDER = "That Drive path is a file, not a folder. Choose a folder."
_BACKUPS_UNREADABLE = "The server's backup folder could not be read, so nothing was copied to the Drive."
_UNREADABLE_FILE = "It could not be read from the server's backup folder (check who owns the file)."
_NOT_REGULAR_FILE = "It is not a readable regular file in the server's backup folder."
_UPLOAD_REFUSED = "The app refused to upload it: it is not a regular backup archive in the backups folder."
_UPLOAD_NOT_LISTED = "Proton Drive reported the upload as done, but the file is not in the Drive folder."
_GONE = "The file is no longer in the Drive folder."
_TRASH_REFUSED = "The app refused to move it to the trash."
_STILL_LISTED = "Proton Drive said it was moved to the trash, but it is still in the Drive folder."


@dataclass(frozen=True)
class RunOutcome:
    """What one run did and learned.

    ``result``: ``ok``, ``warning`` (conflicts only), ``partial`` (some upload or
    trash failed) or ``error`` (the run was aborted).

    ``connection`` is ``connected`` unless this run found evidence otherwise
    (``disconnected`` after a "login first" answer, ``vault_error`` when the
    stored session could not be unlocked): a run only starts while connected, and
    a timeout or an API change says nothing about the sign-in. ``folder_status``
    is ``None`` when the run did not learn it, else ``ok``/``missing``/``not_folder``.

    ``uploaded`` / ``conflicts`` / ``failed`` (newest first) are filled once the
    final listing is known, so an aborted run before that reports none of them;
    ``uploaded`` holds only files the listing shows complete. ``trashed`` holds
    the files the CLI reported moved to the trash and that the listing taken
    afterwards no longer shows; if that last listing could not be taken (result
    ``partial``) or the run was aborted during the trash step, it is only what the
    CLI reported. ``failure_details`` maps each ``failed`` name to a short reason.

    ``remote`` is the plain backup files (``type == "file"``, a backup-archive
    name) of the last successful listing of the run -- the one taken after the
    trash calls when there were any. If that listing is missing (it failed, or
    the run aborted after the trash calls), names the CLI reported trashed are
    left out. ``listed`` says whether any listing succeeded (``remote == []``
    alone cannot tell an empty folder from a run that never saw it). On an abort
    after the first listing it is that first listing. ``error`` is the abort
    reason, or, for a ``partial`` run, what went wrong while trashing (it names
    the files; upload problems are in ``failure_details``); ``None`` otherwise.
    """

    result: str
    connection: str
    folder_status: str | None
    uploaded: list[str]
    conflicts: list[str]
    failed: list[str]
    trashed: list[str]
    remote: list[RemoteFile]
    error: str | None
    failure_details: dict[str, str] = field(default_factory=dict)
    listed: bool = False


def file_sha1(path: Path) -> str:
    """SHA-1 of a file, read in 1 MiB chunks (the Drive reports SHA-1 digests)."""
    digest = hashlib.sha1(usedforsecurity=False)
    with open(path, "rb") as handle:
        while chunk := handle.read(_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


# --- internals ---


class _Stop(Exception):
    """The run cannot go on; ``args[0]`` is the plain-language reason."""


@dataclass
class _Run:
    connection: str = "connected"
    folder_status: str | None = None
    error: str | None = None
    uploaded: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    failure_details: dict[str, str] = field(default_factory=dict)
    trashed: list[str] = field(default_factory=list)
    trash_problems: list[str] = field(default_factory=list)  # "<name>: <reason>", one per file not trashed
    trash_unconfirmed: str | None = None  # why the listing after the trash calls could not be taken
    trash_checked: bool = False  # `listing` was taken after the trash calls
    listing: list[RemoteEntry] = field(default_factory=list)
    listed: bool = False

    def fail(self, name: str, reason: str) -> None:
        self.failed.append(name)
        self.failure_details[name] = reason

    def outcome(self) -> RunOutcome:
        if self.error is not None:
            result, error = "error", self.error
        elif self.failed or self.trash_problems or self.trash_unconfirmed:
            result, error = "partial", self._trash_note()
        elif self.conflicts:
            result, error = "warning", None
        else:
            result, error = "ok", None
        # Only a listing that predates the trash calls needs the reported names taken out.
        gone = set() if self.trash_checked else set(self.trashed)
        remote = [
            RemoteFile(name=e.name, size=e.size, sha1=e.sha1)
            for e in self.listing
            if e.type == "file" and e.name not in gone and is_backup_name(e.name)
        ]
        return RunOutcome(
            result=result,
            connection=self.connection,
            folder_status=self.folder_status,
            uploaded=self.uploaded,
            conflicts=self.conflicts,
            failed=self.failed,
            trashed=self.trashed,
            remote=remote,
            error=error,
            failure_details=self.failure_details,
            listed=self.listed,
        )

    def _trash_note(self) -> str | None:
        parts = []
        if self.trash_problems:
            count = len(self.trash_problems)
            what = "1 old backup" if count == 1 else f"{count} old backups"
            parts.append(f"Could not move {what} to the Drive trash. First problem: {self.trash_problems[0]}")
        if self.trash_unconfirmed:
            parts.append(
                "The Drive folder could not be checked after moving old backups to the trash, "
                f"so the move is not confirmed: {self.trash_unconfirmed}"
            )
        return " ".join(parts) or None


def run_sync(
    *,
    cli: ProtonDriveCli,
    vault: SessionVault,
    cfg: CloudConfig,
    backups_dir: Path,
    today: date,
    sha1_of: Callable[[Path], str] = file_sha1,
) -> RunOutcome:
    """Run one upload-and-retention pass; see the module docstring.

    ``today`` is the UTC date, captured once by the caller; it feeds both the
    candidate cut-off and the retention cut-off so they always agree.
    """
    run = _Run()
    try:
        normalize_folder(cfg.folder)
    except ValueError as exc:
        # Checked again here (not only on save): a hand-edited config must not reach the CLI.
        run.error = str(exc)
        return run.outcome()
    try:
        with vault.unlocked():
            try:
                _sync(run, cli, cfg, backups_dir, today, sha1_of)
            except _Stop as stop:
                run.error = stop.args[0]
            except CloudCliError as exc:
                run.error = exc.message
                if exc.kind == "auth":
                    run.connection = "disconnected"
    except VaultError as exc:
        # Raised while opening the vault, before the body ran: no CLI call was made.
        run.connection = "vault_error"
        run.error = str(exc)
    return run.outcome()


def _list_folder(run: _Run, cli: ProtonDriveCli, folder: str) -> list[RemoteEntry]:
    try:
        entries = cli.list_folder(folder)
    except CloudCliError as exc:
        if exc.kind == "not_found":
            run.folder_status = "missing"
            raise _Stop(_FOLDER_MISSING) from None
        raise
    run.listing = entries
    run.listed = True
    return entries


def _group_by_name(entries: list[RemoteEntry]) -> dict[str, list[RemoteEntry]]:
    groups: dict[str, list[RemoteEntry]] = {}
    for entry in entries:
        if entry.name is not None:
            groups.setdefault(entry.name, []).append(entry)
    return groups


def _verdicts(candidates, local_sha1: dict[str, str], entries: list[RemoteEntry]) -> dict[str, str]:
    """``missing`` / ``present`` / ``conflict`` for every candidate whose SHA-1 is known.

    A name listed more than once is judged against each copy and is a conflict if
    any copy differs: ``<folder>/<name>`` would not say which one is meant.
    """
    groups = _group_by_name(entries)
    out: dict[str, str] = {}
    for info in candidates:
        name = info.filename
        if name not in local_sha1:
            continue
        copies = groups.get(name)
        if not copies:
            out[name] = "missing"
            continue
        judged = {classify(info, {name: copy}, local_sha1[name]) for copy in copies}
        out[name] = "conflict" if "conflict" in judged else "present"
    return out


def _sync(run: _Run, cli: ProtonDriveCli, cfg: CloudConfig, backups_dir: Path, today: date, sha1_of: Callable[[Path], str]) -> None:
    folder = cfg.folder

    # 1. The folder must exist and be a folder.
    try:
        node = cli.info(folder)
    except CloudCliError as exc:
        if exc.kind == "not_found":
            run.folder_status = "missing"
            raise _Stop(_FOLDER_MISSING) from None
        raise
    if node.type != "folder":
        run.folder_status = "not_folder"
        raise _Stop(_NOT_A_FOLDER)
    run.folder_status = "ok"

    # 2. What is in it now. An unreadable listing aborts: it must never read as "empty".
    first = _list_folder(run, cli, folder)

    try:
        local = list_backups(backups_dir=backups_dir)
    except BackupListingError:
        raise _Stop(_BACKUPS_UNREADABLE) from None
    candidates = select_candidates(local, cfg, today)  # newest first

    # Everything about a candidate that needs the local file, once, before anything is sent.
    paths: dict[str, Path] = {}
    local_sha1: dict[str, str] = {}
    unreadable: dict[str, str] = {}
    for info in candidates:
        name = info.filename
        path = resolve_backup_archive(backups_dir=backups_dir, archive_name=name)
        if path is None:
            unreadable[name] = _NOT_REGULAR_FILE
            continue
        try:
            local_sha1[name] = sha1_of(path)
        except (OSError, ValueError):
            unreadable[name] = _UNREADABLE_FILE
            continue
        paths[name] = path

    # 3. Upload what is missing, newest first, one at a time. A conflict is left alone.
    first_verdicts = _verdicts(candidates, local_sha1, first)
    to_upload = [info.filename for info in candidates if first_verdicts.get(info.filename) == "missing"]
    upload_errors: dict[str, str] = {}
    for name in to_upload:
        try:
            cli.upload(paths[name], folder)
        except CloudCliError as exc:
            if exc.kind == "auth":
                raise  # the session is gone; every further call would fail the same way
            upload_errors[name] = exc.message
        except ValueError:
            upload_errors[name] = _UPLOAD_REFUSED

    # Only a fresh listing can say whether the uploads worked.
    final = _list_folder(run, cli, folder) if to_upload else first
    verdicts = _verdicts(candidates, local_sha1, final) if to_upload else first_verdicts

    attempted = set(to_upload)
    present: set[str] = set()
    for info in candidates:
        name = info.filename
        if name in unreadable:
            run.fail(name, unreadable[name])
            continue
        verdict = verdicts[name]
        if verdict == "present":
            present.add(name)
            if name in attempted:
                run.uploaded.append(name)
        elif verdict == "conflict":
            run.conflicts.append(name)
        else:
            run.fail(name, upload_errors.get(name) or (_UPLOAD_NOT_LISTED if name in attempted else _GONE))

    # 4. Retention, only once the newest candidate is proven to be in the Drive right now.
    if not candidates or candidates[0].filename not in present:
        return
    doomed = plan_retention(final, cfg, today)
    for name in doomed:
        try:
            cli.trash(folder, name)
        except CloudCliError as exc:
            if exc.kind == "auth":
                raise
            run.trash_problems.append(f"{name}: {exc.message}")
        except ValueError:
            run.trash_problems.append(f"{name}: {_TRASH_REFUSED}")
        else:
            run.trashed.append(name)  # what the CLI reported; the listing below has the last word
    if not doomed:
        return

    # The CLI exits 0 even for an item that was not moved: only the folder says what happened.
    try:
        after = cli.list_folder(folder)
    except CloudCliError as exc:
        if exc.kind == "auth":
            raise
        run.trash_unconfirmed = exc.message
        return
    run.listing = after
    run.trash_checked = True
    still_there = {e.name for e in after if e.type == "file" and e.name is not None}
    for name in [n for n in run.trashed if n in still_there]:
        run.trashed.remove(name)
        run.trash_problems.append(f"{name}: {_STILL_LISTED}")
