from app.logging_config import configure as _configure_logging
_configure_logging()

import asyncio
from dataclasses import asdict
import hashlib
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.gzip import GZipMiddleware as _GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

import datetime as dt
import json
import re
import uuid

from app.services import backups as backups_svc
from app.services import app_events
from app.services import db_stats
from app.services import docker_client
from app.services import logs as logs_svc
from app.services import maintenance as maintenance_svc
from app.services import players as players_svc
from app.services import progression as progression_svc
from app.services import wow_reference as wow_ref
from app.services.config_index import validate_value
from app.services.stats_cache import refresher as stats_refresher
from app.state import db_credentials, get_state, init_state, list_keys_resolved

APP_DIR = Path(__file__).resolve().parent
_AC_STACK = Path(os.environ.get("AC_STACK_DIR", "/ac"))
_SNAPSHOTS = Path(os.environ.get("ADMIN_SNAPSHOTS_DIR", "/admin-snapshots"))
_MAX_IMPORT_BYTES = int(os.environ.get("ADMIN_MAX_IMPORT_BYTES", str(8 * 1024 ** 3)))
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
    try:
        progression_removed = progression_svc._prune_progression_audit_records(
            _state_mod._state.snapshots_dir / progression_svc.PROGRESSION_AUDIT_DIRNAME
        )
        if progression_removed:
            log.info("gc'd %d old progression audit records", progression_removed)
    except Exception:
        log.warning("could not prune progression audit records at startup", exc_info=True)
    # Load the last stats snapshot from disk so a restart serves it instantly.
    stats_refresher.load_from_disk()
    maintenance_scheduler = maintenance_svc.MaintenanceScheduler(
        maintenance_svc.store_from_env()
    )
    maintenance_scheduler.start()
    app.state.maintenance_scheduler = maintenance_scheduler
    try:
        yield
    finally:
        await maintenance_scheduler.stop()


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
templates.env.globals["backups_js_ver"] = _file_hash("backups.js")
templates.env.globals["stats_js_ver"] = _file_hash("stats.js")
templates.env.globals["last_online"] = wow_ref.relative_last_online


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
    info = await asyncio.to_thread(docker_client.inspect_worldserver)
    return templates.TemplateResponse(
        request,
        "partials/status.html",
        {
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
    info, raw = await asyncio.gather(
        asyncio.to_thread(docker_client.inspect_worldserver),
        asyncio.to_thread(docker_client.stats_worldserver),
    )
    stats = None
    if raw is not None:
        stats = {
            "cpu_percent": raw.cpu_percent,
            "memory_used_mb": _humanize_bytes(raw.memory_used_bytes),
            "memory_limit_mb": _humanize_bytes(raw.memory_limit_bytes),
        }
    return templates.TemplateResponse(
        request,
        "partials/stats.html",
        {
            "stats": stats,
            "uptime": _humanize_uptime(info.started_at),
        },
    )


def _stats_last_refreshed(snap) -> str | None:
    if snap is None:
        return None
    return dt.datetime.fromtimestamp(
        snap.fetched_at, tz=dt.timezone.utc
    ).strftime("%Y-%m-%d %H:%M UTC")


@app.get("/api/stats/data", response_class=HTMLResponse)
async def api_stats_data(request: Request) -> HTMLResponse:
    snap = stats_refresher.get()
    # Auto-kick a refresh if the cache is stale/absent and none is running.
    if stats_refresher.is_stale() and stats_refresher.status != "refreshing":
        try:
            stats_refresher.refresh_async(db_credentials())
        except Exception:  # noqa: BLE001 — DB creds/thread issues must not 500 the page
            pass
    return templates.TemplateResponse(
        request,
        "partials/stats_page.html",
        {
            "snap": snap,
            "status": stats_refresher.status,
            "error": stats_refresher.error,
            "last_refreshed": _stats_last_refreshed(snap),
        },
    )


@app.post("/api/stats/refresh")
async def api_stats_refresh() -> dict:
    try:
        started = stats_refresher.refresh_async(db_credentials())
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"could not start refresh: {e}")
    return {"status": "refreshing" if started else "already_running"}


@app.get("/api/players", response_class=HTMLResponse)
async def api_players(request: Request) -> HTMLResponse:
    counts = None
    context = {"counts": counts}
    try:
        creds = db_credentials()
        counts = await asyncio.to_thread(db_stats.count_online, **creds)
        context["counts"] = counts
    except Exception as exc:  # noqa: BLE001 — DB may be down; UI surfaces an incident
        event = app_events.record_exception(
            log,
            "database_stats",
            "Database statistics could not be loaded.",
            exc,
        )
        context["incident_id"] = event.incident_id
    return templates.TemplateResponse(
        request,
        "partials/players.html",
        context,
    )


@app.get("/api/logs", response_class=HTMLResponse)
async def api_logs(request: Request) -> HTMLResponse:
    ac = Path(os.environ.get("AC_STACK_DIR", "/ac"))
    logs_dir = ac / "logs"
    recent_events = [
        {
            "incident_id": event.incident_id,
            "first_seen": event.first_seen.astimezone(dt.timezone.utc).strftime(
                "%Y-%m-%d %H:%M UTC"
            ),
            "last_seen": event.last_seen.astimezone(dt.timezone.utc).strftime(
                "%Y-%m-%d %H:%M UTC"
            ),
            "severity": event.severity,
            "component": event.component,
            "summary": event.summary,
            "occurrences": event.occurrences,
        }
        for event in app_events.events.snapshot()
    ]
    return templates.TemplateResponse(
        request,
        "partials/logs.html",
        {
            "errors_size": logs_svc.file_size(logs_dir / "Errors.log"),
            "errors_lines": logs_svc.tail_filtered(logs_dir / "Errors.log", n=40),
            "server_lines": logs_svc.tail_filtered(logs_dir / "Server.log", n=40),
            "pb_lines": logs_svc.tail_filtered(logs_dir / "Playerbots.log", n=40),
            "app_events": recent_events,
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
        request,
        "partials/backups.html",
        {
            "last_backup_human": human,
            "last_error": s.last_error,
        },
    )


@app.get("/backups", response_class=HTMLResponse)
async def backups_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "backups.html",
        {"title": "azerothcore-admin · backups"},
    )


def _maintenance_store() -> maintenance_svc.MaintenanceStore:
    return maintenance_svc.store_from_env()


@app.get("/maintenance", response_class=HTMLResponse)
async def maintenance_page(request: Request) -> HTMLResponse:
    store = _maintenance_store()
    config = store.load_config()
    return templates.TemplateResponse(
        request,
        "maintenance.html",
        {
            "title": "azerothcore-admin · maintenance",
            "config": config,
            "log": store.read_log(),
            "hours": list(range(24)),
            "error": request.query_params.get("error"),
            "diagnostic": store.degradation_diagnostic(),
        },
    )


@app.get("/api/maintenance")
async def api_maintenance() -> dict:
    store = _maintenance_store()
    config = store.load_config()
    return {
        "config": asdict(config),
        "log": [asdict(entry) for entry in store.read_log()],
        "diagnostic": store.degradation_diagnostic(),
    }


@app.post("/api/maintenance")
async def post_maintenance(
    restart_enabled: str | None = Form(None),
    restart_hour_utc: int = Form(...),
    window_enabled: str | None = Form(None),
    window_stop_hour_utc: int = Form(...),
    window_start_hour_utc: int = Form(...),
):
    store = _maintenance_store()
    current = store.load_config()
    cfg = maintenance_svc.MaintenanceConfig(
        restart_enabled=restart_enabled is not None,
        restart_hour_utc=restart_hour_utc,
        window_enabled=window_enabled is not None,
        window_stop_hour_utc=window_stop_hour_utc,
        window_start_hour_utc=window_start_hour_utc,
        last_runs=current.last_runs,
    )
    try:
        store.save_config(cfg)
    except (OSError, ValueError) as e:
        from urllib.parse import quote
        return RedirectResponse(f"/maintenance?error={quote(str(e))}", status_code=303)
    return RedirectResponse("/maintenance", status_code=303)


def _humanize_gb(num_bytes: int) -> str:
    return f"{num_bytes / (1024 ** 3):.1f}"


@app.get("/api/backups/summary", response_class=HTMLResponse)
async def api_backups_summary(request: Request) -> HTMLResponse:
    ac = Path(os.environ.get("AC_STACK_DIR", "/ac"))
    error = None
    try:
        summary = backups_svc.backups_summary(backups_dir=ac / "backups")
    except backups_svc.BackupListingError as exc:
        error = str(exc)
        summary = backups_svc.BackupsSummary(None, 0, 0)
    last_human = None
    if summary.last_backup_unix:
        last_human = dt.datetime.fromtimestamp(
            summary.last_backup_unix, tz=dt.timezone.utc
        ).strftime("%Y-%m-%d %H:%M UTC")
    return templates.TemplateResponse(
        request,
        "partials/backups_summary.html",
        {
            "total_count": summary.total_count,
            "disk_gb": _humanize_gb(summary.disk_used_bytes),
            "last_human": last_human,
            "error": error,
        },
    )


@app.get("/api/backups/list", response_class=HTMLResponse)
async def api_backups_list(request: Request) -> HTMLResponse:
    ac = Path(os.environ.get("AC_STACK_DIR", "/ac"))
    error = None
    try:
        rows = backups_svc.list_backups(backups_dir=ac / "backups")
    except backups_svc.BackupListingError as exc:
        error = str(exc)
        rows = []
    return templates.TemplateResponse(
        request,
        "partials/backups_list.html",
        {
            "rows": [
                {
                    "filename": r.filename,
                    "label": r.label,
                    "created": r.created.strftime("%Y-%m-%d %H:%M UTC"),
                    "size_mb": round(r.size_bytes / (1024 * 1024)),
                }
                for r in rows
            ],
            "error": error,
        },
    )


@app.get("/api/backups/download/{archive_name}")
async def download_backup(archive_name: str) -> FileResponse:
    if (
        "/" in archive_name
        or ".." in archive_name
        or not archive_name.startswith("azerothcore-backup-")
        or not archive_name.endswith(".tar.gz")
    ):
        raise HTTPException(status_code=400, detail="invalid archive name")
    archive = Path(os.environ.get("AC_STACK_DIR", "/ac")) / "backups" / archive_name
    if not archive.is_file():
        raise HTTPException(status_code=404, detail="archive not found")
    return FileResponse(archive, media_type="application/gzip", filename=archive.name)


from sse_starlette.sse import EventSourceResponse

from app.services.actions import run_clear_bots, run_force_stop, run_restart, run_reset_bots, run_start, run_stop, verify_env_vars_bound, ActionResult
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


def _render_progress(step: str, msg: str, timestamp: dt.datetime) -> str:
    # All four entities (& < > ") must be escaped: step is interpolated
    # into a quoted attribute (class="step-…"), and msg can carry
    # ampersands or angle brackets from arbitrary subprocess stderr.
    safe_step = _esc(step)
    safe_msg = _esc(msg)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=dt.timezone.utc)
    ts = timestamp.astimezone(dt.timezone.utc).strftime("%d %b %H:%M")
    return f'<li class="step step-{safe_step}"><span class="log-ts">[{ts}]</span> <b>{safe_step}</b>: {safe_msg}</li>'


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
        f'<li class="action-done action-{css}" data-status="{_esc(record.status)}">'
        f"action <b>{_esc(record.name)}</b> finished: <b>{_esc(record.status)}</b>"
        f"{verify_html}"
        f"</li>"
    )


async def _sse_stream(record: ActionRecord):
    q = record.subscribe()
    try:
        while True:
            item = await q.get()
            kind = item[0]
            if kind == "progress":
                _, timestamp, step, msg = item
                yield {"event": "progress", "data": _render_progress(step, msg, timestamp)}
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


@app.post("/api/action/reset-bots")
async def post_reset_bots():
    record = _kick("reset_bots", lambda cb: run_reset_bots(on_progress=cb))
    return {"id": record.id, "status": "running"}


@app.post("/api/action/clear-bots")
async def post_clear_bots():
    record = _kick("clear_bots", lambda cb: run_clear_bots(on_progress=cb))
    return {"id": record.id, "status": "running"}


@app.post("/api/action/backup")
async def post_backup():
    from app.services.actions import run_backup_manual

    record = _kick("backup", lambda cb: run_backup_manual(on_progress=cb))
    return {"id": record.id, "status": "running"}


class RestorePayload(BaseModel):
    archive: str


@app.post("/api/action/restore")
async def post_restore(payload: RestorePayload):
    from app.services.actions import run_restore

    name = payload.archive
    if (
        "/" in name
        or ".." in name
        or not name.startswith("azerothcore-backup-")
        or not name.endswith(".tar.gz")
    ):
        raise HTTPException(status_code=400, detail="invalid archive name")
    record = _kick("restore", lambda cb: run_restore(name, on_progress=cb))
    return {"id": record.id, "status": "running"}


@app.post("/api/action/import-restore")
async def post_import_restore(file: UploadFile = File(...)):
    from app.services.actions import validate_canonical_backup, run_restore

    if not (file.filename or "").endswith(".tar.gz"):
        raise HTTPException(400, "file must be a .tar.gz archive")

    ac_stack = Path(os.environ.get("AC_STACK_DIR", "/ac"))
    backups_dir = ac_stack / "backups"
    stamp = int(dt.datetime.now().timestamp())
    archive_name = f"azerothcore-backup-imported-{stamp}-{uuid.uuid4().hex}.tar.gz"
    dest = backups_dir / archive_name
    staged = backups_dir / f".{archive_name}.upload"

    if file.size is not None and file.size > _MAX_IMPORT_BYTES:
        raise HTTPException(413, "uploaded archive exceeds the configured size limit")
    try:
        total = 0
        backups_dir.mkdir(parents=True, exist_ok=True)
        with staged.open("xb") as out:
            while chunk := await file.read(1024 * 1024):
                total += len(chunk)
                if total > _MAX_IMPORT_BYTES:
                    out.close()
                    staged.unlink(missing_ok=True)
                    raise HTTPException(413, "uploaded archive exceeds the configured size limit")
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
    except OSError as e:
        staged.unlink(missing_ok=True)
        raise HTTPException(500, f"could not save upload: {e}")

    validation_error = await asyncio.to_thread(validate_canonical_backup, staged)
    if validation_error is not None:
        staged.unlink(missing_ok=True)
        raise HTTPException(400, f"invalid restore archive — {validation_error}")
    try:
        os.replace(staged, dest)
    except OSError as e:
        staged.unlink(missing_ok=True)
        raise HTTPException(500, f"could not publish upload: {e}")

    try:
        record = _kick("restore", lambda cb: run_restore(archive_name, on_progress=cb))
    except HTTPException as e:
        if e.status_code == 409:
            dest.unlink(missing_ok=True)
        raise
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


@app.get("/players", response_class=HTMLResponse)
async def players_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "players.html", {"title": "azerothcore-admin · players"},
    )


def _players_last_refreshed(snap) -> str | None:
    if snap is None:
        return None
    return dt.datetime.fromtimestamp(
        snap.fetched_at, tz=dt.timezone.utc
    ).strftime("%Y-%m-%d %H:%M UTC")


@app.get("/api/players/data", response_class=HTMLResponse)
async def api_players_data(request: Request) -> HTMLResponse:
    snap = None
    err = None
    try:
        snap = await asyncio.to_thread(players_svc.collect_players, **db_credentials())
    except Exception as e:  # noqa: BLE001 — DB down must not 500 the page
        err = str(e)
    return templates.TemplateResponse(
        request,
        "partials/players_page.html",
        {"snap": snap, "error": err, "last_refreshed": _players_last_refreshed(snap)},
    )


@app.get("/stats", response_class=HTMLResponse)
async def stats_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "stats.html", {"title": "azerothcore-admin · stats"},
    )


class ProgressionApplyPayload(BaseModel):
    guid: int
    target_expansion: str


@app.get("/progression", response_class=HTMLResponse)
async def progression_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "progression.html",
        {"title": "azerothcore-admin · progression"},
    )


@app.get("/api/progression/characters", response_class=HTMLResponse)
async def api_progression_characters(request: Request) -> HTMLResponse:
    rows = ()
    err = None
    cfg = progression_svc.config_from_resolved_keys(list_keys_resolved())
    try:
        rows = await asyncio.to_thread(progression_svc.collect_characters, **db_credentials())
    except Exception as e:  # noqa: BLE001
        err = str(e)
    rows_json = json.dumps([
        {"guid": r.guid, "account": r.account, "name": r.name,
         "level": r.level, "online": r.online, "progression": r.progression,
         "expansion": r.expansion}
        for r in rows
    ])
    return templates.TemplateResponse(
        request,
        "partials/progression_page.html",
        {
            "rows": rows,
            "rows_json": rows_json,
            "config": cfg,
            "error": err,
            "labels": progression_svc.EXPANSION_LABELS,
            "icons": progression_svc.EXPANSION_ICONS,
        },
    )


@app.post("/api/progression/apply")
async def api_progression_apply(payload: ProgressionApplyPayload) -> dict:
    cfg = progression_svc.config_from_resolved_keys(list_keys_resolved())
    if not runner.try_acquire_mutation():
        raise HTTPException(status_code=409, detail="another destructive action is already running")
    try:
        result = await asyncio.to_thread(progression_svc.apply_progression,
            guid=payload.guid,
            target_expansion=payload.target_expansion,
            config=cfg,
            snapshots_dir=_SNAPSHOTS,
            **db_credentials(),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        runner.release_mutation()
    return {
        "status": result.status,
        "target_state": result.target_state,
        "effective_state": result.effective_state,
        "reason": result.reason,
        "message": result.message,
    }


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "title": "azerothcore-admin · settings",
        },
    )


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

    invalid = {
        key: error
        for key, value in payload.pending.items()
        if (error := validate_value(state.key_index[key], value)) is not None
    }
    if invalid:
        raise HTTPException(400, f"invalid setting values: {invalid}")

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

    if failed:
        on_progress("verify", "post-apply env-var verification FAILED")
        return ActionResult.ERROR
    return ActionResult.OK
