"""Single-action-at-a-time runner with broadcast SSE progress.

Design choices:
  - POSTs are fire-and-forget: kick off the action on a background
    task and return the action id immediately. The browser doesn't
    block for 40-60 s on Stop or several minutes on Restart.
  - ActionRecord stores an append-only list of (step, msg) tuples,
    so reconnecting or late-joining SSE clients can replay history
    before subscribing to new events.
  - Each SSE consumer gets its own asyncio.Queue; the runner fans
    out progress events to every queue. Multiple browser tabs can
    watch the same action.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from app.services.actions import ActionResult


@dataclass
class ActionRecord:
    id: str
    name: str
    status: str = "running"  # 'running' | 'ok' | 'error' | 'timeout' | 'already'
    steps: list[tuple[str, str]] = field(default_factory=list)
    # Verification metadata populated by Apply flow (Task 25); empty
    # otherwise. Each entry is an actions.VerifyFailure with the env
    # var, the originating dist-file key (if known), and a reason.
    # Typed loosely as `list` here to avoid an import cycle between
    # runner.py and actions.py.
    verify_failed: list = field(default_factory=list)
    _subscribers: list[asyncio.Queue] = field(default_factory=list)
    _done: bool = False

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        # Replay history so a late-joining client sees everything that
        # happened before it connected.
        for step, msg in self.steps:
            q.put_nowait(("progress", step, msg))
        if self._done:
            q.put_nowait(("done", self.status, ""))
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        try:
            self._subscribers.remove(q)
        except ValueError:
            pass

    def _broadcast(self, item: tuple[str, str, str]) -> None:
        for q in list(self._subscribers):
            try:
                q.put_nowait(item)
            except asyncio.QueueFull:  # default Queue is unbounded; defensive
                pass


class ActionRunner:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._current: ActionRecord | None = None
        self._last: ActionRecord | None = None

    def current(self) -> ActionRecord | None:
        return self._current

    def last(self) -> ActionRecord | None:
        return self._last

    def get(self, action_id: str) -> ActionRecord | None:
        for r in (self._current, self._last):
            if r is not None and r.id == action_id:
                return r
        return None

    def start(
        self,
        name: str,
        func: Callable[[Callable[[str, str], None]], ActionResult],
        *,
        pre: Callable[[], None] | None = None,
    ) -> ActionRecord:
        """Register a new action and kick it off on a background task.

        Returns the ActionRecord immediately. Raises if another action
        is already in flight.

        `pre` runs SYNCHRONOUSLY under the single-flight lock, after the
        current-action check and before the record is published. Use it
        for I/O that MUST be covered by the single-flight guarantee
        (e.g. apply's snapshot+write of admin.yml): a torn write at this
        point is impossible because no other apply can interleave, and
        the resulting record always has a corresponding background task.
        If `pre` raises, no record is registered and no task is spawned.
        """
        with self._lock:
            if self._current is not None:
                raise RuntimeError("another action already running")
            if pre is not None:
                pre()
            record = ActionRecord(id=str(uuid.uuid4()), name=name)
            self._current = record

        loop = asyncio.get_running_loop()

        def _commit(step: str, msg: str) -> None:
            # Runs on the event loop: append + broadcast atomically with
            # respect to subscribe(). If we appended on the worker thread
            # and broadcast separately, a late subscribe() could replay
            # the just-appended step AND receive the queued broadcast,
            # duplicating the event.
            record.steps.append((step, msg))
            record._broadcast(("progress", step, msg))

        def on_progress(step: str, msg: str) -> None:
            loop.call_soon_threadsafe(_commit, step, msg)

        async def _run() -> None:
            try:
                result = await asyncio.to_thread(func, on_progress)
                record.status = result.value
            except Exception as e:  # noqa: BLE001
                record.status = "error"
                on_progress("exception", str(e))
            finally:
                record._done = True
                record._broadcast(("done", record.status, ""))
                with self._lock:
                    self._last = record
                    self._current = None

        asyncio.create_task(_run())
        return record


runner = ActionRunner()
