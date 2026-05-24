from app.logging_config import configure as _configure_logging
_configure_logging()

import asyncio
import hashlib
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.gzip import GZipMiddleware as _GZipMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import datetime as dt
import re

from app.services import backups as backups_svc
from app.services import db_stats
from app.services import docker_client
from app.services import logs as logs_svc
from app.state import db_credentials, get_state, init_state, list_keys_resolved

APP_DIR = Path(__file__).resolve().parent
_AC_STACK = Path(os.environ.get("AC_STACK_DIR", "/ac"))
_SNAPSHOTS = Path(os.environ.get("ADMIN_SNAPSHOTS_DIR", "/admin-snapshots"))
log = logging.getLogger(__name__)


def _file_hash(rel: str) -> str:
    """Return an 8-char MD5 fingerprint of a static file for cache-busting."""
    try:
        return hashlib.md5((APP_DIR / "static" / rel).read_bytes()).hexdigest()[:8]
    except OSError:
        return "0"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Guard: tests call init_state() directly before TestClient; skip
    # re-init in the lifespan so the test's fixture paths are preserved.
    import app.state as _state_mod
    if _state_mod._state is None:
        init_state(
            dist_dir=Path("/app/dist"),
            admin_yml=_AC_STACK / "docker-compose.admin.yml",
            override_yml=_AC_STACK / "docker-compose.override.yml",
            configs_dir=_AC_STACK / "configs",
            snapshots_dir=_SNAPSHOTS,
        )
    # GC any admin.yml.bak.* snapshots older than 7 days.
    if _state_mod._state is not None:
        removed = _state_mod._state.admin.gc_old_snapshots(keep_days=7)
        if removed:
            log.info("gc'd %d old admin.yml snapshots", removed)
    yield


class _GZipExcludeSSE:
    """GZip middleware that bypasses compression for the SSE stream endpoint.

    Starlette's GZipMiddleware compresses text/event-stream responses when the
    browser sends Accept-Encoding: gzip (which EventSource always does). Some
    browsers cannot decode gzip-encoded SSE streams and silently drop all
    events. The /api/action/stream endpoint must be uncompressed; everything
    else benefits from normal GZip compression.
    """

    def __init__(self, app, minimum_size: int = 1024) -> None:
        self._gzip = _GZipMiddleware(app, minimum_size=minimum_size)
        self._app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http" and scope.get("path") == "/api/action/stream":
            await self._app(scope, receive, send)
        else:
            await self._gzip(scope, receive, send)


app = FastAPI(title="azerothcore-admin", lifespan=lifespan)
app.add_middleware(_GZipExcludeSSE, minimum_size=1024)
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))
templates.env.globals["css_ver"] = _file_hash("app.css")
templates.env.globals["js_ver"] = _file_hash("settings.js")


@app.exception_handler(HTTPException)
async def http_handler(request: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
    )


@app.exception_handler(Exception)
async def unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
    import traceback
    log.error("unhandled: %s\n%s", exc, traceback.format_exc())
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal error — see admin logs."},
    )


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {"title": "azerothcore-admin"},
    )


@app.get("/api/keys")
async def api_keys() -> list[dict]:
    return list_keys_resolved()


@app.get("/api/status", response_class=HTMLResponse)
async def api_status(request: Request) -> HTMLResponse:
    info = docker_client.inspect_worldserver()
    return templates.TemplateResponse(
        "partials/status.html",
        {
            "request": request,
            "status": info.status,
            "started_at_human": _format_started_at(info.started_at),
            "exit_code": info.exit_code,
        },
    )


def _humanize_bytes(b: int) -> int:
    return round(b / (1024 * 1024))


def _normalize_docker_ts(s: str) -> str:
    # Docker timestamps use nanosecond precision; fromisoformat only handles
    # up to microseconds. Truncate any extra sub-second digits.
    return re.sub(r'(\.\d{6})\d+', r'\1', s).replace('Z', '+00:00')


def _humanize_uptime(started_at: str | None) -> str:
    if not started_at:
        return "—"
    try:
        started = dt.datetime.fromisoformat(_normalize_docker_ts(started_at))
    except ValueError:
        return "—"
    delta = dt.datetime.now(dt.timezone.utc) - started
    hours, rem = divmod(int(delta.total_seconds()), 3600)
    minutes = rem // 60
    return f"{hours}h {minutes}m"


def _format_started_at(s: str | None) -> str:
    if not s:
        return "—"
    try:
        started = dt.datetime.fromisoformat(_normalize_docker_ts(s))
        return started.strftime("%Y-%m-%d %H:%M UTC")
    except (ValueError, AttributeError):
        return "—"


@app.get("/api/stats", response_class=HTMLResponse)
async def api_stats(request: Request) -> HTMLResponse:
    info = docker_client.inspect_worldserver()
    raw = docker_client.stats_worldserver()
    stats = None
    if raw is not None:
        stats = {
            "cpu_percent": raw.cpu_percent,
            "memory_used_mb": _humanize_bytes(raw.memory_used_bytes),
            "memory_limit_mb": _humanize_bytes(raw.memory_limit_bytes),
        }
    return templates.TemplateResponse(
        "partials/stats.html",
        {
            "request": request,
            "stats": stats,
            "uptime": _humanize_uptime(info.started_at),
        },
    )


@app.get("/api/players", response_class=HTMLResponse)
async def api_players(request: Request) -> HTMLResponse:
    counts = None
    try:
        creds = db_credentials()
        counts = db_stats.count_online(**creds)
    except Exception:  # noqa: BLE001 — DB may be down; UI surfaces None
        counts = None
    return templates.TemplateResponse(
        "partials/players.html",
        {"request": request, "counts": counts},
    )


@app.get("/api/logs", response_class=HTMLResponse)
async def api_logs(request: Request) -> HTMLResponse:
    ac = Path(os.environ.get("AC_STACK_DIR", "/ac"))
    logs_dir = ac / "logs"
    return templates.TemplateResponse(
        "partials/logs.html",
        {
            "request": request,
            "errors_size": logs_svc.file_size(logs_dir / "Errors.log"),
            "errors_lines": logs_svc.tail_filtered(logs_dir / "Errors.log", n=40),
            "server_lines": logs_svc.tail_filtered(logs_dir / "Server.log", n=40),
            "pb_lines": logs_svc.tail_filtered(logs_dir / "Playerbots.log", n=40),
        },
    )


@app.get("/api/backups", response_class=HTMLResponse)
async def api_backups(request: Request) -> HTMLResponse:
    ac = Path(os.environ.get("AC_STACK_DIR", "/ac"))
    s = backups_svc.backup_status(
        backups_dir=ac / "backups",
        log_path=ac / "logs" / "backup.log",
    )
    human = None
    if s.last_backup_unix:
        human = dt.datetime.fromtimestamp(
            s.last_backup_unix, tz=dt.timezone.utc
        ).strftime("%Y-%m-%d %H:%M UTC")
    return templates.TemplateResponse(
        "partials/backups.html",
        {
            "request": request,
            "last_backup_human": human,
            "last_error": s.last_error,
        },
    )


from sse_starlette.sse import EventSourceResponse

from app.services.actions import run_force_stop, run_restart, run_start, run_stop, verify_env_vars_bound, ActionResult
from app.services.config_policy import BLOCKED_KEYS
from app.services.runner import ActionRecord, runner


def _esc(s: str) -> str:
    return (
        (s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _render_progress(step: str, msg: str) -> str:
    # All four entities (& < > ") must be escaped: step is interpolated
    # into a quoted attribute (class="step-…"), and msg can carry
    # ampersands or angle brackets from arbitrary subprocess stderr.
    safe_step = _esc(step)
    safe_msg = _esc(msg)
    return f'<li class="step step-{safe_step}"><b>{safe_step}</b>: {safe_msg}</li>'


def _render_done(record: ActionRecord) -> str:
    css = "ok" if record.status == "ok" else "error"
    verify_html = ""
    if record.verify_failed:
        items = []
        for vf in record.verify_failed:
            key_html = (
                f' <span class="vf-key">({_esc(vf.config_key)})</span>'
                if vf.config_key else ""
            )
            items.append(
                f'<li data-key="{_esc(vf.config_key or "")}">'
                f'<code>{_esc(vf.env_var)}</code>{key_html}'
                f' — {_esc(vf.reason)}</li>'
            )
        verify_html = f'<ul class="verify-failed">{"".join(items)}</ul>'
    return (
        f'<div class="action-done action-{css}" data-status="{_esc(record.status)}">'
        f"action <b>{_esc(record.name)}</b> finished: <b>{_esc(record.status)}</b>"
        f"{verify_html}"
        f"</div>"
    )


async def _sse_stream(record: ActionRecord):
    q = record.subscribe()
    try:
        while True:
            kind, a, b = await q.get()
            if kind == "progress":
                yield {"event": "progress", "data": _render_progress(a, b)}
            elif kind == "done":
                yield {"event": "done", "data": _render_done(record)}
                return
    finally:
        record.unsubscribe(q)


def _kick(name: str, fn) -> ActionRecord:
    try:
        return runner.start(name, fn)
    except RuntimeError as e:
        from fastapi import HTTPException
        raise HTTPException(status_code=409, detail=str(e))


@app.post("/api/action/stop")
async def post_stop():
    record = _kick("stop", lambda cb: run_stop(on_progress=cb))
    return {"id": record.id, "status": "running"}


@app.post("/api/action/start")
async def post_start():
    record = _kick("start", lambda cb: run_start(on_progress=cb))
    return {"id": record.id, "status": "running"}


@app.post("/api/action/restart")
async def post_restart():
    record = _kick("restart", lambda cb: run_restart(on_progress=cb))
    return {"id": record.id, "status": "running"}


@app.post("/api/action/force-stop")
async def post_force_stop():
    record = _kick("force_stop", lambda cb: run_force_stop(on_progress=cb))
    return {"id": record.id, "status": "running"}


@app.get("/api/action/stream")
async def stream_action(id: str | None = None):
    """Subscribe to progress for an action.

    With an `id`, streams that specific record (or an idle event if unknown).

    With no `id`, streams a persistent live feed: if an action is currently
    running it is streamed immediately (with full history replay for late
    joiners).  When no action is running, heartbeat events are emitted every
    second so the EventSource connection stays open.  As soon as the next
    action starts it is streamed automatically — no reconnect lag.
    """
    if id is not None:
        record = runner.get(id)
        if record is None:
            async def _idle_once():
                yield {"event": "idle", "data": '<p class="idle">No action found.</p>'}
            return EventSourceResponse(_idle_once())
        return EventSourceResponse(_sse_stream(record))

    async def _live():
        last_streamed_id: str | None = None
        while True:
            record = runner.current() or runner.last()
            if record is not None and record.id != last_streamed_id:
                last_streamed_id = record.id
                async for item in _sse_stream(record):
                    yield item
            else:
                # No action running (or same completed action) — heartbeat.
                yield {"event": "heartbeat", "data": ""}
                await asyncio.sleep(1)

    return EventSourceResponse(_live())


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "title": "azerothcore-admin · settings",
        },
    )


from pydantic import BaseModel

class ApplyPayload(BaseModel):
    pending: dict[str, str]


@app.post("/api/settings/rollback")
async def rollback_settings():
    from fastapi import HTTPException

    state = get_state()

    # Fast-fail if an action is already in flight. The real single-flight
    # guarantee comes from runner.start's lock below; this just avoids the
    # confusing "found snapshot, then 409" path.
    if runner.current() is not None:
        raise HTTPException(409, "another action already running")

    snapshots = state.admin.list_snapshots()
    if not snapshots:
        raise HTTPException(404, "no admin.yml snapshots to roll back to")
    most_recent = snapshots[0]
    # Read the chosen snapshot's content into memory so the pre-hook
    # operates on a stable payload even if the snapshot dir changes
    # between selection and lock acquisition.
    payload = most_recent.read_text()

    def _pre():
        # Snapshot current state first so the operator can roll *forward*
        # again after a rollback. Then overwrite admin.yml in place with
        # the chosen snapshot's content. Both happen under runner._lock
        # so a concurrent apply cannot interleave a partial write.
        state.admin.snapshot()
        with state.admin.path.open("w", encoding="utf-8") as f:
            f.write(payload)

    try:
        record = runner.start(
            "rollback",
            lambda cb: _run_apply_then_verify(state, cb),
            pre=_pre,
        )
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return {
        "id": record.id,
        "status": "running",
        "restored_from": most_recent.name,
    }


@app.post("/api/settings/apply")
async def apply_settings(payload: ApplyPayload):
    from fastapi import HTTPException

    state = get_state()

    # 1. Fast-fail if an action is in flight. (The authoritative single-
    #    flight gate is runner.start's lock; the snapshot+write happens
    #    INSIDE the locked action below, so a race here does not orphan
    #    a write.)
    if runner.current() is not None:
        raise HTTPException(409, "another action already running")

    # 2. Blocklist (closes the silent-drop trap for installer-managed keys).
    blocked = [k for k in payload.pending if k in BLOCKED_KEYS]
    if blocked:
        raise HTTPException(
            400,
            f"refusing to write installer-managed keys: {blocked}",
        )

    # 3. Validate keys exist in the index — typo guard.
    unknown = [k for k in payload.pending if k not in state.key_index]
    if unknown:
        raise HTTPException(400, f"unknown keys: {unknown}")

    # 4. Build the new env dict here (cheap, pure) so the runner action
    #    only has to do I/O. Resolve pending edits against the *current*
    #    admin.yml; the action will re-read at execution time to catch
    #    any concurrent change, but in practice the single-flight lock
    #    prevents that.
    current = state.admin.read_env()
    for key, value in payload.pending.items():
        env_var = state.key_index[key].env_var
        if value == "" and env_var in current:
            del current[env_var]
        elif value != "":
            current[env_var] = value
    new_env = current

    # 5. Snapshot + write happen under runner._lock via the pre-hook so
    #    the single-flight lock fully covers the snapshot+write sequence
    #    (no orphaned writes; no concurrent apply can interleave a
    #    half-written admin.yml).
    def _pre():
        state.admin.snapshot()
        state.admin.write_env(new_env)

    try:
        record = runner.start(
            "apply",
            lambda cb: _run_apply_then_verify(state, cb),
            pre=_pre,
        )
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return {"id": record.id, "status": "running"}


def _run_apply_then_verify(state, on_progress) -> ActionResult:
    result = run_restart(on_progress=on_progress)
    if result != ActionResult.OK:
        return result

    # Build expected from the just-written admin.yml. This excludes pending
    # deletes (their env vars are absent from the file) so the verifier
    # never marks a deleted key as failed.
    expected = state.admin.read_env()

    # Build the reverse map env_var -> dist-file key so VerifyFailure
    # rows can name the human-facing key the operator was editing.
    env_var_to_key = {
        entry.env_var: key
        for key, entry in state.key_index.items()
        if entry.env_var in expected
    }

    failed = verify_env_vars_bound(
        expected,
        env_var_to_key=env_var_to_key,
        on_progress=on_progress,
    )

    # Stash the failure list on the current ActionRecord so the SSE
    # `done` event renderer (_render_done) can display it.
    current = runner.current()
    if current is not None:
        current.verify_failed = failed

    # A bind failure does NOT downgrade the action's overall status to
    # error — the restart itself was successful and the UI surfaces the
    # bind failure separately via the verify_failed list.
    return ActionResult.OK
