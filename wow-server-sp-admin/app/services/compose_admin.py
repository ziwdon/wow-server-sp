"""Read/write the admin-managed docker-compose.admin.yml overlay.

Two production-environment constraints drive the I/O shape here:

1. `docker-compose.admin.yml` is itself a bind mount source, not a path
   inside a writable directory. `rename(2)` over a bind mount fails
   with EBUSY (the destination inode is the mount point), so we cannot
   use the usual atomic tmp+rename trick -- writes are in place
   (open + truncate + write).
2. The file's parent directory (`/ac/`) is mounted read-only, so
   sibling-file snapshots like `/ac/docker-compose.admin.yml.bak.<ts>`
   would hit EROFS. Snapshots live in a separate rw mount
   (`/admin-snapshots/` in the container, `/opt/stacks/azerothcore-admin/
   snapshots/` on the host) passed in via `snapshots_dir`.

Snapshot-before-write is the crash-safety boundary: if the in-place
write tears, the most recent snapshot is the recovery target.
"""

from __future__ import annotations

import time
from pathlib import Path

import yaml

SERVICE = "ac-worldserver"


class AdminCompose:
    def __init__(self, path: Path, *, snapshots_dir: Path) -> None:
        self.path = path
        self.snapshots_dir = snapshots_dir

    def _load(self) -> dict:
        if not self.path.exists():
            return {"services": {SERVICE: {"environment": {}}}}
        data = yaml.safe_load(self.path.read_text()) or {}
        data.setdefault("services", {})
        data["services"].setdefault(SERVICE, {})
        data["services"][SERVICE].setdefault("environment", {})
        return data

    def read_env(self) -> dict[str, str]:
        data = self._load()
        env = data["services"][SERVICE].get("environment") or {}
        # Compose accepts dict or list form; normalize to dict-of-str.
        if isinstance(env, list):
            out: dict[str, str] = {}
            for item in env:
                if "=" in item:
                    k, _, v = item.partition("=")
                    out[k] = v
            return out
        return {str(k): str(v) for k, v in env.items()}

    def write_env(self, env: dict[str, str]) -> None:
        """In-place write -- see module docstring for why rename is unsafe.
        Callers must `snapshot()` first; a torn write is recoverable
        only via that snapshot."""
        data = self._load()
        # Sort for stable diffs.
        data["services"][SERVICE]["environment"] = {
            k: str(env[k]) for k in sorted(env)
        }
        serialized = yaml.safe_dump(data, sort_keys=False, default_flow_style=False)
        # Open with truncation; this preserves the bind-mounted inode so
        # the host file's identity is unchanged.
        with self.path.open("w", encoding="utf-8") as f:
            f.write(serialized)
            f.flush()

    def snapshot(self) -> Path:
        """Write a timestamped copy of admin.yml to snapshots_dir.
        Returns the snapshot path."""
        self.snapshots_dir.mkdir(parents=True, exist_ok=True)
        backup = self.snapshots_dir / f"{self.path.name}.bak.{int(time.time())}"
        backup.write_text(self.path.read_text())
        return backup

    def list_snapshots(self) -> list[Path]:
        if not self.snapshots_dir.exists():
            return []
        return sorted(
            self.snapshots_dir.glob(f"{self.path.name}.bak.*"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )

    def gc_old_snapshots(self, keep_days: int = 7) -> int:
        cutoff = time.time() - (keep_days * 86400)
        removed = 0
        for p in self.list_snapshots():
            if p.stat().st_mtime < cutoff:
                p.unlink()
                removed += 1
        return removed
