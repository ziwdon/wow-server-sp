"""Process-wide singleton for the admin app's runtime state."""

from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path

import yaml

from app.services.compose_admin import AdminCompose
from app.services.config_index import KeyEntry, build_key_index
from app.services.resolver import EffectiveValue, resolve_effective


@dataclass
class _State:
    dist_dir: Path
    admin_yml: Path
    override_yml: Path
    configs_dir: Path
    snapshots_dir: Path
    key_index: dict[str, KeyEntry]
    admin: AdminCompose
    _lock: threading.Lock
    _mtimes: dict[Path, float]


_state: _State | None = None


def init_state(
    *,
    dist_dir: Path,
    admin_yml: Path,
    override_yml: Path,
    configs_dir: Path,
    snapshots_dir: Path,
) -> None:
    global _state
    index = build_key_index(dist_dir)
    _state = _State(
        dist_dir=dist_dir,
        admin_yml=admin_yml,
        override_yml=override_yml,
        configs_dir=configs_dir,
        snapshots_dir=snapshots_dir,
        key_index=index,
        admin=AdminCompose(admin_yml, snapshots_dir=snapshots_dir),
        _lock=threading.Lock(),
        _mtimes={},
    )


def get_state() -> _State:
    if _state is None:
        raise RuntimeError("admin state not initialized; call init_state()")
    return _state


def _parse_override_env(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text()) or {}
    env = (
        (data.get("services") or {})
        .get("ac-worldserver", {})
        .get("environment")
        or {}
    )
    if isinstance(env, list):
        out: dict[str, str] = {}
        for item in env:
            if "=" in item:
                k, _, v = item.partition("=")
                out[k] = v
        return out
    return {str(k): str(v) for k, v in env.items()}


def _parse_conf_values(configs_dir: Path) -> dict[str, str]:
    """Best-effort: scan .conf files for Key = Value lines."""
    kv_re = re.compile(r"^([A-Za-z][A-Za-z0-9_.]*)\s*=\s*(.*?)\s*$")
    values: dict[str, str] = {}
    if not configs_dir.exists():
        return values
    for path in configs_dir.rglob("*.conf"):
        for raw in path.read_text(errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            m = kv_re.match(line)
            if m:
                key, value = m.groups()
                if value.startswith('"') and value.endswith('"') and len(value) >= 2:
                    value = value[1:-1]
                values.setdefault(key, value)
    return values


def list_keys_resolved() -> list[dict]:
    """Return serializable list of all indexed keys with effective values."""
    s = get_state()
    override_env = _parse_override_env(s.override_yml)
    admin_env = s.admin.read_env()
    conf_values = _parse_conf_values(s.configs_dir)
    out: list[dict] = []
    for key, entry in s.key_index.items():
        ev = resolve_effective(
            key=key,
            env_var=entry.env_var,
            dist_default=entry.default,
            conf_value=conf_values.get(key),
            override_env=override_env,
            admin_env=admin_env,
        )
        out.append(
            {
                "key": key,
                "env_var": entry.env_var,
                "effective_value": ev.value,
                "source": ev.source,
                "default": entry.default,
                "inferred_type": entry.inferred_type,
                "comment": entry.comment,
                "source_file": entry.source_file,
            }
        )
    return out


def db_credentials() -> dict[str, str | int]:
    """Read DB credentials from /<AC_STACK_DIR>/.env (DOCKER_DB_ROOT_PASSWORD)."""
    ac_stack = Path(os.environ.get("AC_STACK_DIR", "/ac"))
    env_file = ac_stack / ".env"
    creds: dict[str, str | int] = {
        "host": "ac-database",
        "port": 3306,
        "user": "root",
        "password": "",
    }
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if line.startswith("DOCKER_DB_ROOT_PASSWORD="):
                creds["password"] = (
                    line.split("=", 1)[1].strip().strip('"').strip("'")
                )
                break
    return creds
