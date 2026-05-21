"""Effective-value resolution: admin > override > .conf > .dist."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Source = Literal["dist", "conf", "installer", "admin"]


@dataclass(frozen=True)
class EffectiveValue:
    value: str
    source: Source


def resolve_effective(
    *,
    key: str,
    env_var: str,
    dist_default: str,
    conf_value: str | None,
    override_env: dict[str, str],
    admin_env: dict[str, str],
) -> EffectiveValue:
    if env_var in admin_env:
        return EffectiveValue(value=admin_env[env_var], source="admin")
    if env_var in override_env:
        return EffectiveValue(value=override_env[env_var], source="installer")
    if conf_value is not None:
        return EffectiveValue(value=conf_value, source="conf")
    return EffectiveValue(value=dist_default, source="dist")
