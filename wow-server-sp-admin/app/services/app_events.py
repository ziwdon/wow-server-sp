"""Bounded process-local storage for sanitized application events."""

from __future__ import annotations

import logging
import threading
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone


DEFAULT_CAPACITY = 200
DEFAULT_COALESCING_SECONDS = 300


@dataclass(frozen=True)
class AppEvent:
    incident_id: str
    first_seen: datetime
    last_seen: datetime
    severity: str
    component: str
    summary: str
    occurrences: int


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _incident_id() -> str:
    return f"EVT-{uuid.uuid4().hex[:12].upper()}"


class EventStore:
    def __init__(
        self,
        *,
        capacity: int = DEFAULT_CAPACITY,
        coalescing_seconds: int = DEFAULT_COALESCING_SECONDS,
        clock: Callable[[], datetime] = _utc_now,
        id_factory: Callable[[], str] = _incident_id,
    ) -> None:
        self._events: deque[AppEvent] = deque(maxlen=capacity)
        self._coalescing_window = timedelta(seconds=coalescing_seconds)
        self._clock = clock
        self._id_factory = id_factory
        self._lock = threading.RLock()

    def record(
        self, *, severity: str, component: str, summary: str
    ) -> tuple[AppEvent, bool]:
        with self._lock:
            now = self._clock()
            for index, existing in enumerate(self._events):
                if (
                    existing.severity == severity
                    and existing.component == component
                    and existing.summary == summary
                    and timedelta(0) <= now - existing.last_seen <= self._coalescing_window
                ):
                    updated = replace(
                        existing,
                        last_seen=now,
                        occurrences=existing.occurrences + 1,
                    )
                    self._events[index] = updated
                    return updated, False

            event = AppEvent(
                incident_id=self._id_factory(),
                first_seen=now,
                last_seen=now,
                severity=severity,
                component=component,
                summary=summary,
                occurrences=1,
            )
            self._events.append(event)
            return event, True

    def snapshot(self, severity: str | None = None) -> tuple[AppEvent, ...]:
        with self._lock:
            selected = (
                tuple(self._events)
                if severity is None
                else tuple(event for event in self._events if event.severity == severity)
            )
        return tuple(sorted(selected, key=lambda event: event.last_seen, reverse=True))


events = EventStore()


def record_exception(
    logger: logging.Logger,
    component: str,
    summary: str,
    exc: BaseException,
    *,
    store: EventStore | None = None,
) -> AppEvent:
    event, created = (store or events).record(
        severity="error",
        component=component,
        summary=summary,
    )
    if created:
        logger.error(
            "%s [%s]",
            summary,
            event.incident_id,
            exc_info=(type(exc), exc, exc.__traceback__),
            extra={"incident_id": event.incident_id, "component": component},
        )
    return event
