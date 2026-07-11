import datetime as dt
import asyncio
from unittest.mock import Mock

import pytest

from app.services.maintenance import (
    MaintenanceConfig,
    DueJob,
    MaintenanceLogEntry,
    MaintenanceScheduler,
    MaintenanceStore,
)
from app.services.actions import ActionResult


UTC = dt.timezone.utc


def test_default_config_is_safe_and_utc_hours(tmp_path):
    store = MaintenanceStore(tmp_path)

    cfg = store.load_config()

    assert cfg.restart_enabled is False
    assert cfg.restart_hour_utc == 4
    assert cfg.window_enabled is False
    assert cfg.window_stop_hour_utc == 3
    assert cfg.window_start_hour_utc == 8
    assert cfg.last_runs == {}


def test_config_round_trips_to_json(tmp_path):
    store = MaintenanceStore(tmp_path)
    cfg = MaintenanceConfig(
        restart_enabled=True,
        restart_hour_utc=5,
        window_enabled=True,
        window_stop_hour_utc=6,
        window_start_hour_utc=7,
        last_runs={"restart": "2026-06-12T05"},
    )

    store.save_config(cfg)

    assert store.load_config() == cfg


def test_validate_rejects_bad_hours_and_reversed_window(tmp_path):
    store = MaintenanceStore(tmp_path)

    with pytest.raises(ValueError, match="restart hour"):
        store.save_config(MaintenanceConfig(restart_hour_utc=24))

    with pytest.raises(ValueError, match="start hour must be after stop hour"):
        store.save_config(
            MaintenanceConfig(
                window_enabled=True,
                window_stop_hour_utc=8,
                window_start_hour_utc=8,
            )
        )


def test_validate_allows_reversed_window_when_disabled(tmp_path):
    store = MaintenanceStore(tmp_path)
    cfg = MaintenanceConfig(
        window_enabled=False,
        window_stop_hour_utc=8,
        window_start_hour_utc=3,
    )
    store.save_config(cfg)
    assert store.load_config() == cfg


def test_log_job_field_uses_job_name_not_action(tmp_path):
    store = MaintenanceStore(tmp_path)
    store.save_config(
        MaintenanceConfig(
            window_enabled=True,
            window_stop_hour_utc=3,
            window_start_hour_utc=8,
        )
    )
    runner = Mock()
    runner.start.side_effect = RuntimeError("another action already running")
    scheduler = MaintenanceScheduler(store, runner=runner)

    scheduler.tick(dt.datetime(2026, 6, 12, 3, 0, 0, tzinfo=UTC))

    logs = store.read_log()
    assert logs[0].job == "window_stop"
    assert logs[0].action == "stop"


def test_due_jobs_do_not_catch_up_after_missed_hour(tmp_path):
    store = MaintenanceStore(tmp_path)
    store.save_config(MaintenanceConfig(restart_enabled=True, restart_hour_utc=4))
    scheduler = MaintenanceScheduler(store, runner=Mock())

    due = scheduler.due_jobs(dt.datetime(2026, 6, 12, 6, 0, tzinfo=UTC))

    assert due == []


def test_due_jobs_fire_only_during_first_minute_and_once_per_utc_hour(tmp_path):
    store = MaintenanceStore(tmp_path)
    store.save_config(MaintenanceConfig(restart_enabled=True, restart_hour_utc=4))
    runner = Mock()
    scheduler = MaintenanceScheduler(store, runner=runner)

    due = scheduler.due_jobs(dt.datetime(2026, 6, 12, 4, 0, 30, tzinfo=UTC))
    scheduler.mark_attempted(due[0], dt.datetime(2026, 6, 12, 4, 0, 30, tzinfo=UTC))
    due_again = scheduler.due_jobs(dt.datetime(2026, 6, 12, 4, 0, 45, tzinfo=UTC))
    too_late = scheduler.due_jobs(dt.datetime(2026, 6, 13, 4, 1, 0, tzinfo=UTC))

    assert [job.name for job in due] == ["restart"]
    assert due_again == []
    assert too_late == []


def test_tick_dispatches_due_restart_through_runner(tmp_path):
    store = MaintenanceStore(tmp_path)
    store.save_config(MaintenanceConfig(restart_enabled=True, restart_hour_utc=4))
    runner = Mock()
    runner.start.return_value = type("Record", (), {"id": "abc"})()
    restart = Mock(return_value="ok")
    scheduler = MaintenanceScheduler(
        store,
        runner=runner,
        run_restart=restart,
    )

    scheduler.tick(dt.datetime(2026, 6, 12, 4, 0, 0, tzinfo=UTC))

    assert runner.start.call_args.args[0] == "maintenance_restart"
    assert store.load_config().last_runs["restart"] == "2026-06-12T04"
    logs = store.read_log()
    assert logs[0].job == "restart"
    assert logs[0].status == "started"


def test_runner_wrapper_logs_final_action_result(tmp_path):
    store = MaintenanceStore(tmp_path)
    runner = Mock()
    restart = Mock(return_value=ActionResult.OK)
    scheduler = MaintenanceScheduler(
        store,
        runner=runner,
        run_restart=restart,
    )
    run = scheduler._build_runner_func(  # noqa: SLF001 - pins wrapper behavior
        DueJob("restart", "restart"),
    )

    result = run(lambda *_: None)

    assert result == ActionResult.OK
    logs = store.read_log()
    assert logs[0].status == "ok"
    assert logs[0].message == "scheduled action finished: ok"


def test_tick_skips_and_logs_when_another_action_is_running(tmp_path):
    store = MaintenanceStore(tmp_path)
    store.save_config(MaintenanceConfig(restart_enabled=True, restart_hour_utc=4))
    runner = Mock()
    runner.start.side_effect = RuntimeError("another action already running")
    scheduler = MaintenanceScheduler(store, runner=runner)

    scheduler.tick(dt.datetime(2026, 6, 12, 4, 0, 0, tzinfo=UTC))

    assert store.load_config().last_runs["restart"] == "2026-06-12T04"
    logs = store.read_log()
    assert logs[0] == MaintenanceLogEntry(
        timestamp_utc="2026-06-12 04:00 UTC",
        job="restart",
        action="restart",
        status="skipped",
        message="another action already running",
    )


def test_log_is_trimmed_to_limit(tmp_path):
    store = MaintenanceStore(tmp_path, log_limit=3)

    for idx in range(5):
        store.append_log(
            MaintenanceLogEntry(
                timestamp_utc=f"2026-06-12 0{idx}:00 UTC",
                job="restart",
                action="restart",
                status="ok",
                message=str(idx),
            )
        )

    assert [entry.message for entry in store.read_log()] == ["4", "3", "2"]


@pytest.mark.asyncio
async def test_same_hour_jobs_run_sequentially(tmp_path):
    from app.services.runner import ActionRunner

    store = MaintenanceStore(tmp_path)
    store.save_config(MaintenanceConfig(
        restart_enabled=True, restart_hour_utc=4,
        window_enabled=True, window_stop_hour_utc=4, window_start_hour_utc=8,
    ))
    order = []
    runner = ActionRunner()
    scheduler = MaintenanceScheduler(
        store, runner=runner,
        run_restart=lambda **_kwargs: (order.append("restart"), ActionResult.OK)[1],
        run_stop=lambda **_kwargs: (order.append("stop"), ActionResult.OK)[1],
    )
    scheduler.tick(dt.datetime(2026, 6, 12, 4, 0, tzinfo=UTC))
    await asyncio.gather(*scheduler._pending_jobs)  # noqa: SLF001 - lifecycle contract
    assert runner.current() is not None
    await runner.current().wait()
    assert order == ["restart", "stop"]


@pytest.mark.asyncio
async def test_scheduler_loop_continues_after_tick_exception(tmp_path, monkeypatch):
    scheduler = MaintenanceScheduler(MaintenanceStore(tmp_path), interval_seconds=0)
    calls = 0

    def tick():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("temporary disk failure")
        if calls == 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(scheduler, "tick", tick)
    task = asyncio.create_task(scheduler._run_loop())
    for _ in range(10):
        if calls >= 2:
            break
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls >= 2


@pytest.mark.asyncio
async def test_scheduler_start_is_idempotent_and_stop_cancels_task(tmp_path):
    scheduler = MaintenanceScheduler(MaintenanceStore(tmp_path), interval_seconds=3600)
    scheduler.start()
    task = scheduler._task  # noqa: SLF001 - lifecycle contract
    scheduler.start()
    assert scheduler._task is task
    await scheduler.stop()
    assert scheduler._task is None
