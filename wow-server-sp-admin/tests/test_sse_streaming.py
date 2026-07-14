"""Deterministic regression coverage for the action SSE stream."""

from __future__ import annotations

import asyncio
import datetime as dt
import threading

import app.main as main
import pytest

from app.services.actions import ActionResult
from app.services.runner import ActionRecord, ActionRunner


_AT = dt.datetime(2026, 7, 12, 12, 0, tzinfo=dt.timezone.utc)


def _complete_record(*steps: tuple[str, str]) -> ActionRecord:
    record = ActionRecord(id="finished", name="restart", status="ok")
    record.steps.extend((_AT, step, message) for step, message in steps)
    record._done = True
    return record


class _ObservedRecord(ActionRecord):
    """Reports each real _sse_stream subscription without timing sleeps."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.subscribed_queues: asyncio.Queue[asyncio.Queue] = asyncio.Queue()

    def subscribe(self) -> asyncio.Queue:
        queue = super().subscribe()
        self.subscribed_queues.put_nowait(queue)
        return queue


async def _asgi_get(path: str, *, query_string: bytes = b"", headers=()):
    """Run one complete finite ASGI response without a network server."""
    messages = []
    requested = False
    disconnected = asyncio.Event()

    async def receive():
        nonlocal requested
        if not requested:
            requested = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)

    await main.app(
        {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": query_string,
            "headers": list(headers),
            "client": ("testclient", 123),
            "server": ("testserver", 80),
        },
        receive,
        send,
    )
    return messages


@pytest.mark.asyncio
async def test_sse_asgi_response_is_not_gzipped_when_browser_requests_gzip(monkeypatch):
    record = _complete_record(("stop", "stopping"))
    monkeypatch.setattr(main.runner, "get", lambda action_id: record if action_id == record.id else None)

    messages = await _asgi_get(
        "/api/action/stream",
        query_string=b"id=finished",
        headers=((b"accept-encoding", b"gzip"),),
    )

    headers = dict(next(item for item in messages if item["type"] == "http.response.start")["headers"])
    body = b"".join(item.get("body", b"") for item in messages if item["type"] == "http.response.body")
    assert b"content-encoding" not in headers
    assert b"event: progress" in body
    assert b"event: done" in body


def test_late_subscriber_replays_ordered_history_once_then_done():
    record = _complete_record(("stop", "stopping"), ("wait", "waiting"))

    subscriber = record.subscribe()
    events = [subscriber.get_nowait() for _ in range(subscriber.qsize())]

    assert events == [
        ("progress", _AT, "stop", "stopping"),
        ("progress", _AT, "wait", "waiting"),
        ("done", "ok", ""),
    ]


def test_concurrent_subscribers_receive_each_new_progress_event_in_order():
    record = ActionRecord(id="running", name="restart")
    first = record.subscribe()
    second = record.subscribe()

    record._broadcast(("progress", _AT, "stop", "stopping"))
    record._broadcast(("progress", _AT, "wait", "waiting"))

    expected = [
        ("progress", _AT, "stop", "stopping"),
        ("progress", _AT, "wait", "waiting"),
    ]
    assert [first.get_nowait(), first.get_nowait()] == expected
    assert [second.get_nowait(), second.get_nowait()] == expected


def test_unsubscribed_queue_receives_no_further_runner_events():
    record = ActionRecord(id="running", name="restart")
    subscriber = record.subscribe()

    record.unsubscribe(subscriber)
    record._broadcast(("progress", _AT, "stop", "stopping"))

    assert subscriber.empty()
    assert subscriber not in record._subscribers


@pytest.mark.asyncio
async def test_concurrent_sse_iterators_replay_history_then_each_receive_live_once():
    record = ActionRecord(id="running", name="restart")
    record.steps.append((_AT, "stop", "stopping"))
    record.steps.append((_AT, "wait", "waiting"))
    first = main._sse_stream(record)

    assert await anext(first) == {
        "event": "progress",
        "data": main._render_progress("stop", "stopping", _AT),
    }

    # This subscriber arrives after the action's historical event but before
    # its next live event.  Both streams must receive that event once, after
    # their replay, without sharing a queue.
    late = main._sse_stream(record)
    assert await anext(late) == {
        "event": "progress",
        "data": main._render_progress("stop", "stopping", _AT),
    }

    expected_replay = {
        "event": "progress",
        "data": main._render_progress("wait", "waiting", _AT),
    }
    assert await anext(first) == expected_replay
    assert await anext(late) == expected_replay

    record.steps.append((_AT, "docker_stop", "stopping worldserver"))
    record._broadcast(("progress", _AT, "docker_stop", "stopping worldserver"))
    expected_live = {
        "event": "progress",
        "data": main._render_progress("docker_stop", "stopping worldserver", _AT),
    }
    assert await anext(first) == expected_live
    assert await anext(late) == expected_live

    await first.aclose()
    await late.aclose()


@pytest.mark.asyncio
async def test_closing_sse_iterator_unsubscribes_before_later_broadcast():
    record = _ObservedRecord(id="running", name="restart")
    closing_stream = main._sse_stream(record)
    surviving_stream = main._sse_stream(record)

    # Advance both generators once so _sse_stream has subscribed both queues.
    # Queue observation is an explicit synchronization point, not a sleep.
    first_next = asyncio.create_task(anext(closing_stream))
    closed_queue = await record.subscribed_queues.get()
    second_next = asyncio.create_task(anext(surviving_stream))
    surviving_queue = await record.subscribed_queues.get()
    expected_first = {
        "event": "progress",
        "data": main._render_progress("stop", "stopping", _AT),
    }
    record._broadcast(("progress", _AT, "stop", "stopping"))
    assert await first_next == expected_first
    assert await second_next == expected_first

    await closing_stream.aclose()
    assert closed_queue not in record._subscribers
    assert record._subscribers == [surviving_queue]

    record._broadcast(("progress", _AT, "wait", "waiting"))
    assert closed_queue.empty()
    assert await anext(surviving_stream) == {
        "event": "progress",
        "data": main._render_progress("wait", "waiting", _AT),
    }
    await surviving_stream.aclose()


@pytest.mark.asyncio
async def test_unknown_action_stream_emits_one_idle_event(monkeypatch):
    monkeypatch.setattr(main.runner, "get", lambda _action_id: None)

    response = await main.stream_action(id="missing")
    event = await anext(response.body_iterator)
    await response.body_iterator.aclose()

    assert event == {"event": "idle", "data": '<p class="idle">No action found.</p>'}


@pytest.mark.asyncio
async def test_live_stream_heartbeats_then_streams_the_next_completed_action(monkeypatch):
    runner = ActionRunner()
    record = _complete_record()
    monkeypatch.setattr(main, "runner", runner)

    async def next_cycle(_seconds):
        return None

    response = await main.stream_action()
    stream = response.body_iterator
    assert await anext(stream) == {"event": "heartbeat", "data": ""}

    runner._last = record
    monkeypatch.setattr(main.asyncio, "sleep", next_cycle)
    assert await anext(stream) == {"event": "done", "data": main._render_done(record)}
    await stream.aclose()


@pytest.mark.asyncio
async def test_runner_emits_exception_progress_before_terminal_done(monkeypatch):
    runner = ActionRunner()
    release_worker = threading.Event()
    monkeypatch.setattr("app.services.runner._utcnow", lambda: _AT)

    def fails_after_subscription(_on_progress):
        release_worker.wait()
        raise RuntimeError("restart failed")

    record = runner.start("restart", fails_after_subscription)
    subscriber = record.subscribe()
    release_worker.set()
    await record.wait()

    assert [subscriber.get_nowait(), subscriber.get_nowait()] == [
        ("progress", _AT, "exception", "restart failed"),
        ("done", ActionResult.ERROR.value, ""),
    ]
