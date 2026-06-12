import datetime as dt
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
        DueJob("restart", "restart", 4),
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
