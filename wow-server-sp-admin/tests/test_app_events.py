import json
import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

from app.logging_config import JSONFormatter
from app.services.app_events import EventStore, record_exception


class MutableClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


def deterministic_ids():
    next_id = 0

    def make_id() -> str:
        nonlocal next_id
        next_id += 1
        return f"EVT-{next_id:04d}"

    return make_id


def make_store(*, capacity: int = 200) -> tuple[EventStore, MutableClock]:
    clock = MutableClock()
    return (
        EventStore(capacity=capacity, clock=clock, id_factory=deterministic_ids()),
        clock,
    )


def test_capacity_evicts_oldest_distinct_event() -> None:
    store, clock = make_store()

    for number in range(201):
        store.record(
            severity="error",
            component="database_stats",
            summary=f"Failure {number}",
        )
        clock.advance(1)

    snapshot = store.snapshot()
    assert len(snapshot) == 200
    assert {event.incident_id for event in snapshot} == {
        f"EVT-{number:04d}" for number in range(2, 202)
    }


def test_snapshot_orders_newest_last_seen_first() -> None:
    store, clock = make_store()
    first, _ = store.record(
        severity="warning", component="backups", summary="First warning."
    )
    clock.advance(10)
    second, _ = store.record(
        severity="error", component="database_stats", summary="Second error."
    )
    clock.advance(10)
    updated_first, created = store.record(
        severity="warning", component="backups", summary="First warning."
    )

    assert created is False
    assert updated_first.incident_id == first.incident_id
    assert [event.incident_id for event in store.snapshot()] == [
        first.incident_id,
        second.incident_id,
    ]


def test_snapshot_filters_warning_and_error_severities() -> None:
    store, _ = make_store()
    warning, _ = store.record(
        severity="warning", component="backups", summary="Backup warning."
    )
    error, _ = store.record(
        severity="error", component="database_stats", summary="Database error."
    )

    assert store.snapshot(severity="warning") == (warning,)
    assert store.snapshot(severity="error") == (error,)


def test_identical_events_coalesce_for_five_minutes_from_last_seen() -> None:
    store, clock = make_store()
    first, created = store.record(
        severity="error",
        component="database_stats",
        summary="Database statistics could not be loaded.",
    )
    assert created is True
    assert first.incident_id == "EVT-0001"
    assert first.occurrences == 1

    clock.advance(300)
    repeated, created = store.record(
        severity="error",
        component="database_stats",
        summary="Database statistics could not be loaded.",
    )
    assert created is False
    assert repeated.incident_id == first.incident_id
    assert repeated.first_seen == first.first_seen
    assert repeated.last_seen == clock.now
    assert repeated.occurrences == 2

    clock.advance(301)
    later, created = store.record(
        severity="error",
        component="database_stats",
        summary="Database statistics could not be loaded.",
    )
    assert created is True
    assert later.incident_id == "EVT-0002"
    assert later.occurrences == 1
    assert len(store.snapshot()) == 2


def test_new_event_store_instance_starts_empty() -> None:
    populated, _ = make_store()
    populated.record(severity="warning", component="backups", summary="Warning.")

    fresh, _ = make_store()

    assert populated.snapshot()
    assert fresh.snapshot() == ()


def test_record_exception_stores_sanitized_summary_and_logs_details_once() -> None:
    store, _ = make_store()
    logger = Mock(spec=logging.Logger)
    try:
        raise RuntimeError("password=secret /opt/private")
    except RuntimeError as exc:
        caught_exc = exc

    first = record_exception(
        logger,
        "database_stats",
        "Database statistics could not be loaded.",
        caught_exc,
        store=store,
    )
    repeated = record_exception(
        logger,
        "database_stats",
        "Database statistics could not be loaded.",
        caught_exc,
        store=store,
    )

    assert first.summary == "Database statistics could not be loaded."
    assert "password=secret" not in repr(first)
    assert "/opt/private" not in repr(first)
    assert repeated.occurrences == 2
    logger.error.assert_called_once()
    args, kwargs = logger.error.call_args
    assert args == ("%s [%s]", first.summary, first.incident_id)
    assert kwargs["exc_info"] == (
        RuntimeError,
        caught_exc,
        caught_exc.__traceback__,
    )
    assert kwargs["exc_info"][2] is not None
    assert kwargs["extra"] == {
        "incident_id": first.incident_id,
        "component": "database_stats",
    }


def test_json_formatter_includes_app_event_extras() -> None:
    record = logging.LogRecord(
        name="app.test",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="safe summary",
        args=(),
        exc_info=None,
    )
    record.incident_id = "EVT-0001"
    record.component = "database_stats"

    payload = json.loads(JSONFormatter().format(record))

    assert payload["incident_id"] == "EVT-0001"
    assert payload["component"] == "database_stats"
