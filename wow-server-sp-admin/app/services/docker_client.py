"""Thin docker SDK wrapper for the AC containers we manage by name."""

from __future__ import annotations

from dataclasses import dataclass

import docker
import docker.errors
from requests.exceptions import ConnectionError, ReadTimeout

WORLDSERVER = "ac-worldserver"
DATABASE = "ac-database"


@dataclass(frozen=True)
class ContainerInfo:
    status: str  # 'running' | 'exited' | 'paused' | 'restarting' | 'missing'
    started_at: str | None
    exit_code: int | None
    image: str | None


def _client():
    return docker.from_env()


def inspect_worldserver() -> ContainerInfo:
    try:
        c = _client().containers.get(WORLDSERVER)
    except docker.errors.NotFound:
        return ContainerInfo(status="missing", started_at=None, exit_code=None, image=None)
    except (docker.errors.APIError, ConnectionError, ReadTimeout):
        return ContainerInfo(status="unknown", started_at=None, exit_code=None, image=None)
    state = c.attrs.get("State", {})
    return ContainerInfo(
        status=state.get("Status", "unknown"),
        started_at=state.get("StartedAt"),
        exit_code=state.get("ExitCode"),
        image=c.attrs.get("Config", {}).get("Image"),
    )


@dataclass(frozen=True)
class ContainerStats:
    cpu_percent: float
    memory_used_bytes: int
    memory_limit_bytes: int


def stats_worldserver() -> ContainerStats | None:
    """One-shot non-streaming stats. Returns None if container is not running."""
    try:
        c = _client().containers.get(WORLDSERVER)
    except (docker.errors.NotFound, docker.errors.APIError, ConnectionError, ReadTimeout):
        return None
    try:
        if c.status != "running":
            return None
        s = c.stats(stream=False)
    except (docker.errors.APIError, ConnectionError, ReadTimeout):
        return None
    cpu_stats = s.get("cpu_stats", {})
    precpu = s.get("precpu_stats", {})
    cpu_delta = (
        cpu_stats.get("cpu_usage", {}).get("total_usage", 0)
        - precpu.get("cpu_usage", {}).get("total_usage", 0)
    )
    system_delta = (
        cpu_stats.get("system_cpu_usage", 0) - precpu.get("system_cpu_usage", 0)
    )
    cpu_percent = (
        (cpu_delta / system_delta) * 100.0
        if system_delta > 0
        else 0.0
    )
    mem = s.get("memory_stats", {})
    used = mem.get("usage", 0) - mem.get("stats", {}).get("cache", 0)
    limit = mem.get("limit", 0)
    return ContainerStats(
        cpu_percent=round(cpu_percent, 1),
        memory_used_bytes=int(used),
        memory_limit_bytes=int(limit),
    )
