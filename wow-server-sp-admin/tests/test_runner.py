import asyncio
import datetime as dt

import pytest

from app.services.actions import ActionResult
from app.services.runner import ActionRunner


@pytest.mark.asyncio
async def test_action_record_replays_recorded_progress_timestamp(monkeypatch):
    first_event_at = dt.datetime(2026, 6, 19, 5, 0, tzinfo=dt.timezone.utc)
    replay_at = dt.datetime(2026, 6, 19, 11, 1, tzinfo=dt.timezone.utc)
    clock_values = [first_event_at, replay_at]

    monkeypatch.setattr(
        "app.services.runner._utcnow",
        lambda: clock_values.pop(0),
        raising=False,
    )
    runner = ActionRunner()

    record = runner.start(
        "restart",
        lambda on_progress: (
            on_progress("wait_init", "waiting for World initialized line"),
            ActionResult.OK,
        )[1],
    )

    while runner.current() is not None:
        await asyncio.sleep(0)

    q = record.subscribe()

    assert q.get_nowait() == (
        "progress",
        first_event_at,
        "wait_init",
        "waiting for World initialized line",
    )
