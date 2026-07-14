"""Disk-backed stats snapshot cache with single-flight background refresh."""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

from app.services.players import PvpRankRow, RankRow
from app.services.stats import Bucket, StackedBucket, StackedSegment, StatsSnapshot, collect_stats


log = logging.getLogger(__name__)

DEFAULT_CACHE = (
    Path(os.environ.get("ADMIN_SNAPSHOTS_DIR", "/admin-snapshots"))
    / "stats"
    / "stats-snapshot.json"
)


class StatsRefresher:
    def __init__(self, cache_path: Path = DEFAULT_CACHE):
        self.cache_path = Path(cache_path)
        self.status = "idle"
        self.error: str | None = None
        self._snapshot: StatsSnapshot | None = None
        self._lock = threading.Lock()

    def get(self) -> StatsSnapshot | None:
        with self._lock:
            return self._snapshot

    def is_stale(self, max_age: int = 86400) -> bool:
        with self._lock:
            snapshot = self._snapshot
        if snapshot is None:
            return True
        return time.time() - snapshot.fetched_at > max_age

    def _store(self, snapshot: StatsSnapshot) -> None:
        with self._lock:
            self._snapshot = snapshot

        tmp_path = self.cache_path.parent / (self.cache_path.name + ".tmp")
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path.write_text(json.dumps(dataclasses.asdict(snapshot)), encoding="utf-8")
            os.replace(tmp_path, self.cache_path)
        except OSError as exc:
            log.warning("Failed to write stats cache %s: %s", self.cache_path, exc)

    def load_from_disk(self) -> None:
        try:
            raw = json.loads(self.cache_path.read_text(encoding="utf-8"))
            snapshot = _snapshot_from_json(raw)
        except FileNotFoundError:
            snapshot = None
        except (OSError, json.JSONDecodeError, TypeError, ValueError, KeyError) as exc:
            log.warning("Failed to load stats cache %s: %s", self.cache_path, exc)
            snapshot = None

        with self._lock:
            self._snapshot = snapshot

    def refresh_async(self, creds: dict[str, Any]) -> bool:
        with self._lock:
            if self.status == "refreshing":
                return False
            self.status = "refreshing"
            self.error = None

        thread = threading.Thread(target=self._run, args=(creds,), daemon=True)
        thread.start()
        return True

    def _run(self, creds: dict[str, Any]) -> None:
        try:
            snapshot = collect_stats(**creds)
            self._store(snapshot)
            with self._lock:
                self.error = None
        except Exception as exc:
            log.warning("Stats refresh failed: %s", exc)
            with self._lock:
                self.error = str(exc)
        finally:
            with self._lock:
                self.status = "idle"


def _buckets(value: Any) -> list[Bucket]:
    if not isinstance(value, list):
        raise TypeError("bucket field must be a list")
    buckets: list[Bucket] = []
    for item in value:
        if not isinstance(item, dict):
            raise TypeError("bucket item must be an object")
        label = item["label"]
        if not isinstance(label, str):
            raise TypeError("bucket label must be a string")
        color = item.get("color", "")
        buckets.append(Bucket(label=label, count=int(item["count"]), color=str(color)))
    return buckets


def _stacked_buckets(value: Any) -> list[StackedBucket]:
    if not isinstance(value, list):
        raise TypeError("stacked bucket field must be a list")
    buckets: list[StackedBucket] = []
    for item in value:
        if not isinstance(item, dict):
            raise TypeError("stacked bucket item must be an object")
        segments_raw = item["segments"]
        if not isinstance(segments_raw, list):
            raise TypeError("stacked bucket segments must be a list")
        segments: list[StackedSegment] = []
        for segment in segments_raw:
            if not isinstance(segment, dict):
                raise TypeError("stacked segment item must be an object")
            segments.append(
                StackedSegment(color=str(segment["color"]), count=int(segment["count"]))
            )
        buckets.append(
            StackedBucket(
                label=str(item["label"]),
                total=int(item["total"]),
                segments=tuple(segments),
            )
        )
    return buckets


def _rank_rows(value: Any) -> tuple[RankRow, ...]:
    if not isinstance(value, list):
        raise TypeError("rank field must be a list")
    rows: list[RankRow] = []
    for item in value:
        if not isinstance(item, dict):
            raise TypeError("rank item must be an object")
        avg_ilvl = item.get("avg_ilvl")
        rows.append(
            RankRow(
                rank=int(item["rank"]),
                name=str(item["name"]),
                class_name=str(item["class_name"]),
                class_color=str(item["class_color"]),
                race_name=str(item["race_name"]),
                faction=str(item["faction"]),
                faction_color=str(item["faction_color"]),
                level=int(item["level"]),
                avg_ilvl=None if avg_ilvl is None else int(avg_ilvl),
            )
        )
    return tuple(rows)


def _pvp_rank_rows(value: Any) -> tuple[PvpRankRow, ...]:
    if not isinstance(value, list):
        raise TypeError("pvp rank field must be a list")
    rows: list[PvpRankRow] = []
    for item in value:
        if not isinstance(item, dict):
            raise TypeError("pvp rank item must be an object")
        rows.append(
            PvpRankRow(
                rank=int(item["rank"]),
                name=str(item["name"]),
                class_name=str(item["class_name"]),
                class_color=str(item["class_color"]),
                race_name=str(item["race_name"]),
                faction=str(item["faction"]),
                faction_color=str(item["faction_color"]),
                honor_kills=int(item["honor_kills"]),
                honor=int(item["honor"]),
            )
        )
    return tuple(rows)


def _snapshot_from_json(raw: Any) -> StatsSnapshot:
    if not isinstance(raw, dict):
        raise TypeError("snapshot must be an object")

    data = dict(raw)
    data["fetched_at"] = float(data["fetched_at"])
    for name in (
        "bots_total",
        "bots_online",
        "players_total",
        "players_online",
        "ahbot_total",
        "ahbot_online",
        "bots_active",
        "bots_idle",
        "bots_summon_reserve",
    ):
        data[name] = int(data[name]) if name in data else 0

    for field in dataclasses.fields(StatsSnapshot):
        if field.default_factory is not dataclasses.MISSING:
            if field.name == "bots_by_bracket_stacked":
                data[field.name] = _stacked_buckets(data.get(field.name, []))
            else:
                data[field.name] = _buckets(data.get(field.name, []))

    data["top_pve"] = _rank_rows(data.get("top_pve", []))
    data["top_pvp"] = _pvp_rank_rows(data.get("top_pvp", []))

    return StatsSnapshot(**data)


refresher = StatsRefresher()
