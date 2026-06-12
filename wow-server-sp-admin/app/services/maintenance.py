from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from app.services.actions import ActionResult, run_restart, run_start, run_stop
from app.services.runner import runner as default_runner

UTC = dt.timezone.utc


@dataclass(frozen=True)
class MaintenanceLogEntry:
    timestamp_utc: str
    job: str
    action: str
    status: str
    message: str


@dataclass(frozen=True)
class MaintenanceConfig:
    restart_enabled: bool = False
    restart_hour_utc: int = 4
    window_enabled: bool = False
    window_stop_hour_utc: int = 3
    window_start_hour_utc: int = 8
    last_runs: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "MaintenanceConfig":
        return cls(
            restart_enabled=bool(raw.get("restart_enabled", False)),
            restart_hour_utc=int(raw.get("restart_hour_utc", 4)),
            window_enabled=bool(raw.get("window_enabled", False)),
            window_stop_hour_utc=int(raw.get("window_stop_hour_utc", 3)),
            window_start_hour_utc=int(raw.get("window_start_hour_utc", 8)),
            last_runs={
                str(k): str(v)
                for k, v in dict(raw.get("last_runs", {})).items()
            },
        )


@dataclass(frozen=True)
class DueJob:
    name: str
    action: str
    hour_utc: int


class MaintenanceStore:
    def __init__(self, data_dir: Path, *, log_limit: int = 20) -> None:
        self.data_dir = data_dir
        self.config_path = data_dir / "maintenance.json"
        self.log_path = data_dir / "maintenance-log.jsonl"
        self.log_limit = log_limit

    def load_config(self) -> MaintenanceConfig:
        try:
            raw = json.loads(self.config_path.read_text())
            cfg = MaintenanceConfig.from_dict(raw)
            self.validate(cfg)
            return cfg
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return MaintenanceConfig()

    def save_config(self, cfg: MaintenanceConfig) -> None:
        self.validate(cfg)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.config_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(asdict(cfg), indent=2, sort_keys=True) + "\n")
        os.replace(tmp, self.config_path)

    def validate(self, cfg: MaintenanceConfig) -> None:
        _validate_hour("restart hour", cfg.restart_hour_utc)
        _validate_hour("stop hour", cfg.window_stop_hour_utc)
        _validate_hour("start hour", cfg.window_start_hour_utc)
        if cfg.window_start_hour_utc <= cfg.window_stop_hour_utc:
            raise ValueError("start hour must be after stop hour")

    def append_log(self, entry: MaintenanceLogEntry) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        rows = self._read_log_chronological()
        rows.append(entry)
        rows = rows[-self.log_limit:]
        tmp = self.log_path.with_suffix(".jsonl.tmp")
        tmp.write_text(
            "".join(json.dumps(asdict(row), sort_keys=True) + "\n" for row in rows)
        )
        os.replace(tmp, self.log_path)

    def read_log(self) -> list[MaintenanceLogEntry]:
        return list(reversed(self._read_log_chronological()))

    def _read_log_chronological(self) -> list[MaintenanceLogEntry]:
        try:
            lines = self.log_path.read_text().splitlines()
        except OSError:
            return []
        entries: list[MaintenanceLogEntry] = []
        for line in lines:
            try:
                raw = json.loads(line)
                entries.append(MaintenanceLogEntry(**raw))
            except (TypeError, json.JSONDecodeError):
                continue
        return entries[-self.log_limit:]


def _validate_hour(label: str, value: int) -> None:
    if value < 0 or value > 23:
        raise ValueError(f"{label} must be between 0 and 23")


def _as_utc(now: dt.datetime) -> dt.datetime:
    if now.tzinfo is None:
        return now.replace(tzinfo=UTC)
    return now.astimezone(UTC)


def _hour_stamp(now: dt.datetime) -> str:
    now = _as_utc(now)
    return now.strftime("%Y-%m-%dT%H")


def _display_stamp(now: dt.datetime) -> str:
    now = _as_utc(now)
    return now.strftime("%Y-%m-%d %H:%M UTC")


def _result_status(result: ActionResult | Any) -> str:
    return result.value if isinstance(result, ActionResult) else str(result)


ActionFunc = Callable[..., ActionResult]


class MaintenanceScheduler:
    def __init__(
        self,
        store: MaintenanceStore,
        *,
        runner=default_runner,
        run_restart: ActionFunc = run_restart,
        run_stop: ActionFunc = run_stop,
        run_start: ActionFunc = run_start,
        interval_seconds: int = 30,
    ) -> None:
        self.store = store
        self.runner = runner
        self._actions = {
            "restart": run_restart,
            "stop": run_stop,
            "start": run_start,
        }
        self.interval_seconds = interval_seconds
        self._task: asyncio.Task | None = None

    def due_jobs(self, now: dt.datetime) -> list[DueJob]:
        now = _as_utc(now)
        if now.minute != 0:
            return []
        cfg = self.store.load_config()
        stamp = _hour_stamp(now)
        jobs: list[DueJob] = []
        if (
            cfg.restart_enabled
            and now.hour == cfg.restart_hour_utc
            and cfg.last_runs.get("restart") != stamp
        ):
            jobs.append(DueJob("restart", "restart", cfg.restart_hour_utc))
        if cfg.window_enabled:
            if (
                now.hour == cfg.window_stop_hour_utc
                and cfg.last_runs.get("window_stop") != stamp
            ):
                jobs.append(DueJob("window_stop", "stop", cfg.window_stop_hour_utc))
            if (
                now.hour == cfg.window_start_hour_utc
                and cfg.last_runs.get("window_start") != stamp
            ):
                jobs.append(DueJob("window_start", "start", cfg.window_start_hour_utc))
        return jobs

    def mark_attempted(self, job: DueJob, now: dt.datetime) -> None:
        cfg = self.store.load_config()
        last_runs = dict(cfg.last_runs)
        last_runs[job.name] = _hour_stamp(now)
        self.store.save_config(MaintenanceConfig(
            restart_enabled=cfg.restart_enabled,
            restart_hour_utc=cfg.restart_hour_utc,
            window_enabled=cfg.window_enabled,
            window_stop_hour_utc=cfg.window_stop_hour_utc,
            window_start_hour_utc=cfg.window_start_hour_utc,
            last_runs=last_runs,
        ))

    def tick(self, now: dt.datetime | None = None) -> None:
        now = _as_utc(now or dt.datetime.now(UTC))
        for job in self.due_jobs(now):
            self.mark_attempted(job, now)
            try:
                self.runner.start(
                    f"maintenance_{job.action}",
                    self._build_runner_func(job),
                )
            except RuntimeError as e:
                self.store.append_log(MaintenanceLogEntry(
                    timestamp_utc=_display_stamp(now),
                    job=job.action,
                    action=job.action,
                    status="skipped",
                    message=str(e),
                ))
                continue
            self.store.append_log(MaintenanceLogEntry(
                timestamp_utc=_display_stamp(now),
                job=job.action,
                action=job.action,
                status="started",
                message="scheduled action started",
            ))

    def _build_runner_func(self, job: DueJob):
        def _run(on_progress):
            try:
                result = self._actions[job.action](on_progress=on_progress)
            except Exception as e:  # noqa: BLE001
                self.store.append_log(MaintenanceLogEntry(
                    timestamp_utc=_display_stamp(dt.datetime.now(UTC)),
                    job=job.action,
                    action=job.action,
                    status="error",
                    message=str(e),
                ))
                raise
            status = _result_status(result)
            self.store.append_log(MaintenanceLogEntry(
                timestamp_utc=_display_stamp(dt.datetime.now(UTC)),
                job=job.action,
                action=job.action,
                status=status,
                message=f"scheduled action finished: {status}",
            ))
            return result

        return _run

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _run_loop(self) -> None:
        while True:
            self.tick()
            await asyncio.sleep(self.interval_seconds)


def store_from_env() -> MaintenanceStore:
    return MaintenanceStore(Path(os.environ.get("ADMIN_DATA_DIR", "/admin-data")))
