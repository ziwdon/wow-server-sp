"""Invoke the bundled canonical backup script to create one consolidated archive.

The admin runs the SAME script the host cron runs (bundled into the image at
/app/scripts/backup.sh), with STACK_DIR=/ac. We never reimplement backup logic
in Python - single source of truth. See the design spec.
"""
from __future__ import annotations

import logging
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

SCRIPT = Path(os.environ.get("BACKUP_SCRIPT", "/app/scripts/backup.sh"))

ProgressCb = Callable[[str, str], None]


@dataclass
class BackupResult:
    ok: bool
    archive: str | None
    output: str


def run_backup(
    label: str,
    *,
    on_progress: ProgressCb | None = None,
    stack_dir: str | None = None,
) -> BackupResult:
    """Run `backup.sh --label <label>` and stream stdout as progress."""
    stack = stack_dir or os.environ.get("AC_STACK_DIR", "/ac")
    proc = subprocess.Popen(
        ["bash", str(SCRIPT), "--label", label],
        env={**os.environ, "STACK_DIR": stack},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    lines: list[str] = []
    archive: str | None = None
    assert proc.stdout is not None
    for raw in proc.stdout:
        line = raw.rstrip("\n")
        lines.append(line)
        if on_progress:
            on_progress("backup", line)
        if "Wrote " in line:
            archive = line.split("Wrote ", 1)[1].strip()
    proc.wait()
    ok = proc.returncode == 0
    if not ok:
        log.error("backup.sh failed (rc=%s): %s", proc.returncode, "\n".join(lines[-5:]))
    return BackupResult(ok=ok, archive=archive if ok else None, output="\n".join(lines))
