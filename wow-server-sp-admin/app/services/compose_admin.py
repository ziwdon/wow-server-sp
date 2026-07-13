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
from collections.abc import Collection
from pathlib import Path

import yaml

from app.services.config_policy import BLOCKED_KEYS
from app.services.env_var import config_key_to_ac_env_var

SERVICE = "ac-worldserver"
_EXPECTED_TOP_LEVEL_KEYS = frozenset({"services"})
_EXPECTED_SERVICE_KEYS = frozenset({"environment"})
_BLOCKED_ENV_VARS = frozenset(config_key_to_ac_env_var(key) for key in BLOCKED_KEYS)


def validate_restored_overlay(path: Path, *, allowed_env_vars: Collection[str]) -> str | None:
    """Return an error unless a restored overlay matches the admin-only contract.

    The overlay is an untrusted archive member during restore.  It may only
    describe the worldserver environment that the settings UI itself can
    produce; accepting other Compose constructs would let an archive change
    container topology or override unrelated runtime settings.
    """
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return "restored admin overlay is malformed"

    if not isinstance(data, dict) or set(data) != _EXPECTED_TOP_LEVEL_KEYS:
        return "restored admin overlay has unsupported top-level keys"
    services = data["services"]
    if not isinstance(services, dict):
        return "restored admin overlay services must be a mapping"
    if not services:
        return None
    if set(services) != {SERVICE}:
        return "restored admin overlay contains extra services"
    worldserver = services[SERVICE]
    if not isinstance(worldserver, dict) or set(worldserver) != _EXPECTED_SERVICE_KEYS:
        return "restored admin overlay has unsupported worldserver settings"
    env = worldserver["environment"]
    if not isinstance(env, dict):
        return "restored admin overlay environment must be a mapping"

    allowed = set(allowed_env_vars)
    for key, value in env.items():
        if not isinstance(key, str) or not isinstance(value, str):
            return "restored admin overlay environment entries must be strings"
        if key in _BLOCKED_ENV_VARS:
            return f"restored admin overlay contains blocked key: {key}"
        if key not in allowed:
            return f"restored admin overlay key is not approved: {key}"
    return None


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
