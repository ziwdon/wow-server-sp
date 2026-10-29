from app.logging_config import configure as _configure_logging
_configure_logging()

import asyncio
from dataclasses import asdict
import hashlib
import logging
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

import anyio
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi import Path as PathParam
from fastapi.middleware.gzip import GZipMiddleware as _GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.datastructures import UploadFile as StarletteUploadFile
from pydantic import BaseModel, StrictBool

import datetime as dt
import json
import re
import uuid

from app.services import backups as backups_svc
from app.services import app_events
from app.services import armory as armory_svc
from app.services import armory_assets
from app.services import armory_resolver
from app.services import cloud_backup
from app.services import cloud_config as cloud_cfg
from app.services import customization as customization_svc
from app.services import db_stats
from app.services import dbc as dbc_svc
from app.services import docker_client
from app.services import known_errors as known_errors_svc
from app.services import logs as logs_svc
from app.services import maintenance as maintenance_svc
from app.services import players as players_svc
from app.services import presence as presence_svc
from app.services import progression as progression_svc
from app.services import raid_unlock as raid_unlock_svc
from app.services import wow_reference as wow_ref
from app.services.cloud_vault import UNLOCK_FAILED_MESSAGE
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


def _warm_dbc_store(store) -> None:
    """The warm-up thread's body: parse the armory's DBC files once, off every request (spec 6, R3)."""
    try:
        store.load()
    except Exception:  # noqa: BLE001 - a failed warm-up only means the first armory view loads them
        log.warning("armory: DBC warm-up failed; the first armory view loads the files", exc_info=True)


def _start_dbc_warmup() -> None:
    """Start the daemon thread that loads the ``DbcStore``. Never blocks and never raises."""
    try:
        store = dbc_svc.store_from_env()
        threading.Thread(
            target=_warm_dbc_store, args=(store,), name="armory-dbc-warmup", daemon=True
        ).start()
    except Exception:  # noqa: BLE001 - lazy loading on first access stays the fallback
        log.warning("armory: could not start the DBC warm-up; the first armory view loads the files", exc_info=True)


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
    _start_dbc_warmup()
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
    presence_announcer = presence_svc.PresenceAnnouncer(presence_svc.store_from_env())
    presence_announcer.start()
    app.state.presence_announcer = presence_announcer
    cloud = cloud_backup.get_service()
    await cloud.start()
    app.state.cloud_backup = cloud
    try:
        yield
    finally:
        # First: it may be waiting on a pending sign-in. A failing stop must not keep the others running.
        try:
            await cloud.stop()
        except Exception:  # noqa: BLE001
            log.exception("could not stop the cloud backup service cleanly")
        await presence_announcer.stop()
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


# The Armory page and its assets (spec 7): no third-party origin may load anything, so the
# browser never talks to Wowhead (the proxy does), and the viewer's protocol-relative
# viewer.css link is refused. Initial value; Task 13 trims it from a real browser run and
# records the final sources and the reason for each here.
ARMORY_CSP = (
    "default-src 'self'; "
    "img-src 'self' data: blob:; "
    "script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
    "style-src 'self' 'unsafe-inline'; "
    "worker-src 'self' blob:; "
    "connect-src 'self'; "
    "font-src 'self' data:"
)
_CSP_HEADER = b"content-security-policy"


def _is_armory_path(path: str) -> bool:
    """``/armory`` and everything under ``/armory/`` (page and assets); never ``/api/armory/*``."""
    return path == "/armory" or path.startswith("/armory/")


class _ArmoryCSP:
    """Pure ASGI wrapper (like ``_GZipExcludeSSE``) adding ``ARMORY_CSP`` to every armory response.

    Every response on an armory path gets it, error answers included, and any policy set
    further in is replaced, so exactly one header is sent.
    """

    def __init__(self, app) -> None:
        self._app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or not _is_armory_path(scope.get("path", "")):
            await self._app(scope, receive, send)
            return

        async def send_with_csp(message) -> None:
            if message["type"] == "http.response.start":
                headers = [
                    (name, value)
                    for name, value in message.get("headers", [])
                    if name.lower() != _CSP_HEADER
                ]
                headers.append((_CSP_HEADER, ARMORY_CSP.encode("latin-1")))
                message = {**message, "headers": headers}
            await send(message)

        await self._app(scope, receive, send_with_csp)


app = FastAPI(title="azerothcore-admin", lifespan=lifespan)
app.add_middleware(_GZipExcludeSSE, minimum_size=1024)
app.add_middleware(_ArmoryCSP)  # added last = outermost: it also covers the gzip'd answers
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))
templates.env.globals["css_ver"] = _file_hash("app.css")
templates.env.globals["js_ver"] = _file_hash("settings.js")
templates.env.globals["backups_js_ver"] = _file_hash("backups.js")
templates.env.globals["stats_js_ver"] = _file_hash("stats.js")
templates.env.globals["last_online"] = wow_ref.relative_last_online
templates.env.globals["branding"] = lambda: customization_svc.store_from_env().branding()


def _epoch_stamp(value: object) -> str:
    """A unix time as ``YYYY-MM-DD HH:MM UTC``; 0 (never logged out) or garbage is ``never``."""
    try:
        seconds = int(value)  # type: ignore[arg-type]
        if seconds <= 0:
            return "never"
        return dt.datetime.fromtimestamp(seconds, tz=dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except (TypeError, ValueError, OverflowError, OSError):
        return "never"


templates.env.filters["epoch_stamp"] = _epoch_stamp


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
        {"title": None},
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


def _presence_snapshot() -> dict[str, str | None] | None:
    """Current PresenceTracker view, or None when no announcer is wired (tests)."""
    announcer = getattr(app.state, "presence_announcer", None)
    return announcer.tracker.snapshot() if announcer is not None else None


@app.get("/api/players", response_class=HTMLResponse)
async def api_players(request: Request) -> HTMLResponse:
    counts = None
    context = {"counts": counts}
    try:
        creds = db_credentials()
        counts = await asyncio.to_thread(
            db_stats.count_online, **creds, presence=_presence_snapshot(),
        )
        context["counts"] = counts
    except Exception as exc:  # noqa: BLE001 — DB may be down; UI surfaces an incident
        try:
            event = app_events.record_exception(
                log,
                "database_stats",
                "Database statistics could not be loaded.",
                exc,
            )
            context["incident_id"] = event.incident_id
        except Exception:  # noqa: BLE001 — event recording and logging are best-effort
            pass
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
    errors_lines = logs_svc.tail_filtered(logs_dir / "Errors.log", n=40)
    return templates.TemplateResponse(
        request,
        "partials/logs.html",
        {
            "errors": known_errors_svc.summarize_errors_log(logs_dir / "Errors.log"),
            "errors_rows": [(line, known_errors_svc.match_known(line)) for line in errors_lines],
            "server_lines": logs_svc.tail_filtered(logs_dir / "Server.log", n=40),
            "pb_lines": logs_svc.tail_filtered(logs_dir / "Playerbots.log", n=40),
            "app_events": recent_events,
        },
    )


@app.get("/api/backups", response_class=HTMLResponse)
async def api_backups(request: Request) -> HTMLResponse:
    ac = Path(os.environ.get("AC_STACK_DIR", "/ac"))
    s = await asyncio.to_thread(
        backups_svc.backup_status,
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
        {"title": "Backups"},
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
            "title": "Maintenance",
            "config": config,
            "log": store.read_log(),
            "hours": list(range(24)),
            "error": request.query_params.get("error"),
            "diagnostic": store.degradation_diagnostic(),
            "presence": presence_svc.store_from_env().load_config(),
        },
    )


class PresencePayload(BaseModel):
    announce_enabled: StrictBool


@app.get("/api/presence")
async def api_presence() -> dict:
    return asdict(presence_svc.store_from_env().load_config())


@app.post("/api/presence")
async def post_presence(payload: PresencePayload) -> dict:
    cfg = presence_svc.PresenceConfig(announce_enabled=payload.announce_enabled)
    try:
        presence_svc.store_from_env().save_config(cfg)
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"could not save presence config: {e}")
    return asdict(cfg)


@app.get("/customization", response_class=HTMLResponse)
async def customization_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "customization.html",
        {
            "title": "Customization",
            "current": customization_svc.store_from_env().branding(),
            "max_name_length": customization_svc.MAX_NAME_LENGTH,
            "default_name": customization_svc.DEFAULT_NAME,
            "error": request.query_params.get("error"),
            "saved": request.query_params.get("saved") == "1",
        },
    )


@app.post("/api/customization")
async def post_customization(request: Request):
    # Read the form by hand: an empty file input may arrive as a plain "" field, not a file.
    form = await request.form()
    name = form.get("name", "")
    logo = form.get("logo")
    data = None
    if isinstance(logo, StarletteUploadFile) and logo.filename:
        # One byte over the limit is enough to refuse it; never read an oversized upload whole.
        data = await logo.read(customization_svc.MAX_LOGO_BYTES + 1)
    try:
        customization_svc.store_from_env().save(
            name=name if isinstance(name, str) else "",
            logo=data,
            remove_logo=form.get("remove_logo") is not None,
        )
    except (OSError, customization_svc.CustomizationError) as e:
        from urllib.parse import quote
        message = str(e) if isinstance(e, customization_svc.CustomizationError) else f"Could not save: {e}"
        return RedirectResponse(f"/customization?error={quote(message)}", status_code=303)
    return RedirectResponse("/customization?saved=1", status_code=303)


@app.get("/customization/logo")
async def customization_logo() -> Response:
    logo = customization_svc.store_from_env().logo()
    if logo is None:
        raise HTTPException(status_code=404, detail="no logo")
    data, media_type = logo
    return Response(
        content=data,
        media_type=media_type,
        # The URL carries ?v=<sha256 prefix>, so a changed logo gets a new URL.
        headers={"Cache-Control": "public, max-age=86400", "X-Content-Type-Options": "nosniff"},
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
            "cloud_card": _safe_cloud_card(await _cloud_status_or_none()),
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
    cloud = await _cloud_status_or_none()
    in_drive = cloud.in_drive if cloud is not None else frozenset()
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
                    "in_drive": r.filename in in_drive,
                }
                for r in rows
            ],
            "error": error,
            "cloud_folder": cloud.config.folder if cloud is not None and cloud.config.enabled else None,
            "cloud_retention": cloud.config.retention_days if cloud is not None else None,
        },
    )


@app.get("/api/backups/download/{archive_name}")
async def download_backup(archive_name: str) -> FileResponse:
    backups_dir = Path(os.environ.get("AC_STACK_DIR", "/ac")) / "backups"
    archive = backups_svc.resolve_backup_archive(
        backups_dir=backups_dir, archive_name=archive_name
    )
    if archive is None:
        raise HTTPException(status_code=404, detail="archive not found")
    return FileResponse(archive, media_type="application/gzip", filename=archive.name)


# --- Cloud backup (Proton Drive) panel on the Backups tab ---------------------------------------
# Every /api/cloud/* route answers 200 with the re-rendered panel; a problem is text inside it, so
# htmx swaps it in place. The blocking service calls run on worker threads (a sign-in start can
# wait ~30 s); the state-changing ones through _await_thread_completion, so a client that goes
# away mid-request never gets control back while the call is still working. The service owns the
# lock and the secrets; nothing here reads the session.

CLOUD_GENERIC_ERROR = "Something went wrong — see the admin log."
CLOUD_STALLED = "No fully successful cloud run for over a day"
CLOUD_SAVE_FAILED = "Could not save the settings (the admin data folder is not writable)."
CLOUD_BAD_RETENTION = (
    f"Keep-for days must be a whole number from {cloud_cfg.RETENTION_MIN} to {cloud_cfg.RETENTION_MAX}."
)
CLOUD_DISCONNECTED = (
    "Removed from this server. To end the session at Proton too, "
    "revoke it in your Proton account's session list."
)
_CLOUD_RUN_RESULTS = {
    "ok": ("OK", "ok"),
    "warning": ("Conflicts", "warn"),
    "partial": ("Partly failed", "bad"),
    "error": ("Failed", "bad"),
}


def _utc_stamp(value: object) -> str:
    """An ISO timestamp as ``YYYY-MM-DD HH:MM UTC`` (as in the backups list); unparsable text is returned as is."""
    if not isinstance(value, str):
        return ""
    try:
        moment = dt.datetime.fromisoformat(value)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=dt.timezone.utc)
        return moment.astimezone(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except (ValueError, OverflowError):  # e.g. 0001-01-01T00:00:00+14:00 does not fit in UTC
        return value


templates.env.filters["utc_stamp"] = _utc_stamp


def _cloud_text(text: str | None) -> str | None:
    """Text for the page; anything naming a stored-session field is replaced (defence in depth)."""
    return cloud_backup._scrub(text)


async def _cloud_status_or_none():
    """The service's status, or ``None`` when it cannot be read (the Backups page then shows no cloud info)."""
    try:
        return await asyncio.to_thread(cloud_backup.get_service().status)
    except Exception:  # noqa: BLE001 - cloud trouble must never break the local backups list
        log.exception("could not read the cloud backup status")
        return None


def _cloud_primary(view) -> str:
    """The one state the status line shows (spec 6.8)."""
    if view.available is None:
        return "checking"
    if view.available is False:
        return "unavailable"
    if view.signin_pending:
        return "pending"
    return view.connection if view.connection in ("connected", "vault_error") else "disconnected"


def _cloud_runs(entries) -> list[dict]:
    rows = []
    for entry in entries:
        label, tone = _CLOUD_RUN_RESULTS.get(entry.result, (entry.result, "bad"))
        parts = [
            f"{len(entry.uploaded)} uploaded" if entry.uploaded else "",
            f"{len(entry.conflicts)} conflict{'' if len(entry.conflicts) == 1 else 's'}" if entry.conflicts else "",
            f"{len(entry.failed)} failed" if entry.failed else "",
            f"{len(entry.trashed)} moved to trash" if entry.trashed else "",
        ]
        rows.append(
            {
                "started": entry.started,
                "trigger": entry.trigger,
                "label": label,
                "tone": tone,
                "counts": ", ".join(part for part in parts if part) or "nothing to do",
                "error": _cloud_text(entry.error),
            }
        )
    return rows


def _cloud_card(view) -> dict | None:
    """The "Cloud copies" summary card, or ``None`` when the status could not be read."""
    if view is None:
        return None
    state = view.state
    if not view.config.enabled:
        return {"value": "Off", "tone": "muted", "sub": None}
    if view.available is False:
        return {"value": "Unavailable", "tone": "bad", "sub": None}
    if view.available is None:
        return {"value": "Checking…", "tone": "muted", "sub": None}
    if view.signin_pending:
        return {"value": "Waiting for sign-in", "tone": "muted", "sub": None}
    if state.connection == "vault_error":
        return {"value": "Can't unlock the session", "tone": "bad", "sub": None}
    if state.connection != "connected":
        return {"value": "Not connected", "tone": "bad", "sub": None}
    if view.stalled:
        return {"value": "Stalled", "tone": "bad", "sub": CLOUD_STALLED}
    if state.last_result in ("partial", "error"):
        return {"value": "Last upload failed", "tone": "bad", "sub": None}
    sub = f"{len(view.in_drive)} in Drive"
    if not state.last_success:
        return {"value": "No uploads yet", "tone": "muted", "sub": sub}
    return {"value": _utc_stamp(state.last_success), "tone": None, "sub": sub}


def _safe_cloud_card(view) -> dict | None:
    """``_cloud_card``, but a bug or odd state data there only drops the card, never the local summary."""
    try:
        return _cloud_card(view)
    except Exception:  # noqa: BLE001
        log.exception("could not build the Cloud copies card")
        return None


def _cloud_context(view, *, error, notice, form, preserve_form) -> dict:
    ctx = {"view": view, "error": error, "notice": notice, "preserve_form": preserve_form}
    if view is None:
        return ctx
    cfg, state = view.config, view.state
    primary = _cloud_primary(view)
    connected = primary == "connected"
    last_error = _cloud_text(state.last_error) if primary in ("disconnected", "vault_error", "connected") else None
    if primary == "vault_error" and last_error == UNLOCK_FAILED_MESSAGE:
        last_error = None  # the status line already says it
    issues = state.issues if connected else []
    folder_notice = None
    if connected:
        folder_notice = state.folder_status if state.folder_status in ("missing", "not_folder") else None
        if not cfg.folder:
            folder_notice = "unset"
    ctx.update(
        primary=primary,
        # "Checking…" (no probe result yet) also polls, so the panel resolves without a reload.
        polling=view.signin_pending or view.busy or view.available is None,
        # Spec 6.9: Disconnect works even when the vault cannot be unsealed, so "Not available"
        # still offers it while a session is stored or the state says it was ever connected.
        can_disconnect=primary in ("connected", "vault_error")
        or (primary == "unavailable" and (view.connection != "disconnected" or view.has_stored_session)),
        state_unsaved=view.state_unsaved,
        form=form
        or {
            "enabled": cfg.enabled,
            "folder": cfg.folder,
            "labels": set(cfg.labels),
            "retention_days": cfg.retention_days,
        },
        all_labels=cloud_cfg.ALL_LABELS,
        last_error=last_error,
        stalled=connected and view.stalled,
        conflicts=[item["name"] for item in issues if item["kind"] == "conflict"],
        failures=[
            {"name": item["name"], "detail": _cloud_text(item["detail"])}
            for item in issues
            if item["kind"] == "failed"
        ],
        folder_notice=folder_notice,
        can_upload=connected and bool(cfg.folder) and folder_notice is None,
        unavailable_reason=_cloud_text(view.unavailable_reason),
        runs=_cloud_runs(view.log),
    )
    return ctx


async def _cloud_panel_response(
    request: Request,
    *,
    error: str | None = None,
    notice: str | None = None,
    form: dict | None = None,
    preserve_form: bool = False,
) -> HTMLResponse:
    view = await _cloud_status_or_none()
    return templates.TemplateResponse(
        request,
        "partials/cloud_panel.html",
        _cloud_context(view, error=error, notice=notice, form=form, preserve_form=preserve_form),
    )


async def _cloud_call(method: str, *args, validation: bool = False, on_oserror: str | None = None) -> str | None:
    """Call a service method off the event loop. Returns the text to show, or ``None`` on success.

    The service's own exceptions carry user-facing text. A ``ValueError`` is shown only for a form
    the owner typed (``validation``); anything else is logged and replaced by a generic line.
    Every caller changes state, so the worker thread is shielded and always awaited.
    """
    try:
        await _await_thread_completion(getattr(cloud_backup.get_service(), method), *args)
    except (cloud_backup.CloudBusy, cloud_backup.CloudUnavailable, cloud_backup.CloudActionError) as exc:
        return _cloud_text(str(exc)) or CLOUD_GENERIC_ERROR
    except ValueError as exc:
        if validation:
            return _cloud_text(str(exc)) or CLOUD_GENERIC_ERROR
        log.exception("cloud backup %s failed", method)
        return CLOUD_GENERIC_ERROR
    except OSError:
        log.exception("cloud backup %s failed", method)
        return on_oserror or CLOUD_GENERIC_ERROR
    except Exception:  # noqa: BLE001 - never echo a raw exception to the page
        log.exception("cloud backup %s failed unexpectedly", method)
        return CLOUD_GENERIC_ERROR
    return None


@app.get("/api/cloud/panel", response_class=HTMLResponse)
async def api_cloud_panel(request: Request) -> HTMLResponse:
    # Also the panel's own poll: keep the settings form the owner may be typing in.
    return await _cloud_panel_response(request, preserve_form=True)


@app.post("/api/cloud/settings", response_class=HTMLResponse)
async def api_cloud_settings(
    request: Request,
    enabled: str | None = Form(None),
    folder: str = Form(""),
    labels: list[str] = Form([]),
    retention_days: str = Form(""),
) -> HTMLResponse:
    form = {
        "enabled": enabled is not None,
        "folder": folder.strip(),
        "labels": set(labels),
        "retention_days": retention_days.strip(),
    }
    if not re.fullmatch(r"[0-9]{1,6}", form["retention_days"]):
        error = CLOUD_BAD_RETENTION
    else:
        cfg = cloud_cfg.CloudConfig(
            enabled=form["enabled"],
            folder=form["folder"],
            labels=tuple(labels),
            retention_days=int(form["retention_days"]),
        )
        error = await _cloud_call("save_settings", cfg, validation=True, on_oserror=CLOUD_SAVE_FAILED)
    if error is not None:
        return await _cloud_panel_response(request, error=error, form=form)
    # Show right away whether the folder exists; busy or not connected just means "not now".
    await _cloud_call("check_folder")
    return await _cloud_panel_response(request, notice="Settings saved.")


@app.post("/api/cloud/connect", response_class=HTMLResponse)
async def api_cloud_connect(request: Request) -> HTMLResponse:
    return await _cloud_panel_response(request, error=await _cloud_call("start_sign_in"))


@app.post("/api/cloud/connect/cancel", response_class=HTMLResponse)
async def api_cloud_connect_cancel(request: Request) -> HTMLResponse:
    return await _cloud_panel_response(request, error=await _cloud_call("cancel_sign_in"))


@app.post("/api/cloud/disconnect", response_class=HTMLResponse)
async def api_cloud_disconnect(request: Request) -> HTMLResponse:
    error = await _cloud_call("disconnect")
    return await _cloud_panel_response(request, error=error, notice=None if error else CLOUD_DISCONNECTED)


@app.post("/api/cloud/upload-now", response_class=HTMLResponse)
async def api_cloud_upload_now(request: Request) -> HTMLResponse:
    error = await _cloud_call("start_run", "manual")
    return await _cloud_panel_response(request, error=error, notice=None if error else "Upload started.")


@app.post("/api/cloud/create-folder", response_class=HTMLResponse)
async def api_cloud_create_folder(request: Request) -> HTMLResponse:
    error = await _cloud_call("create_folder")
    return await _cloud_panel_response(request, error=error, notice=None if error else "Folder created.")


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
    backups_dir = Path(os.environ.get("AC_STACK_DIR", "/ac")) / "backups"
    if backups_svc.resolve_backup_archive(backups_dir=backups_dir, archive_name=name) is None:
        raise HTTPException(status_code=404, detail="archive not found")
    # run_restore re-checks with resolve_backup_archive itself (it can be
    # reached independently of this route) — this is a fast-fail HTTP-level
    # rejection, not the authoritative check.
    record = _kick("restore", lambda cb: run_restore(name, on_progress=cb))
    return {"id": record.id, "status": "running"}


async def _await_thread_completion(func, /, *args):
    """Run `func(*args)` on a worker thread, shielding it from caller cancellation.

    Awaiting via `asyncio.shield` means cancelling the *caller* (e.g. a client
    disconnect mid-request) does not cancel the underlying thread task. On
    `CancelledError` we still await the underlying task so the thread is
    guaranteed to finish (and any cleanup it depends on, e.g. a `finally`
    that removes staged files, is safe to run) before we re-raise — the
    caller cannot regain control while the thread is still touching the
    filesystem.
    """
    task = asyncio.create_task(asyncio.to_thread(func, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        finally:
            raise


def _copy_upload_to_staging(source, staged: Path, max_bytes: int) -> int:
    """Stream `source` (a sync file-like object) into `staged`, fsync'd.

    Runs on a worker thread via `_await_thread_completion` — must be
    thread-safe and must not touch the event loop.
    """
    total = 0
    source.seek(0)
    with staged.open("xb") as output:
        while chunk := source.read(1024 * 1024):
            total += len(chunk)
            if total > max_bytes:
                raise OverflowError("uploaded archive exceeds the configured size limit")
            output.write(chunk)
        output.flush()
        os.fsync(output.fileno())
    return total


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

    backups_dir.mkdir(parents=True, exist_ok=True)
    try:
        try:
            await _await_thread_completion(_copy_upload_to_staging, file.file, staged, _MAX_IMPORT_BYTES)
        except OverflowError:
            raise HTTPException(413, "uploaded archive exceeds the configured size limit")
        except OSError as e:
            raise HTTPException(500, f"could not save upload: {e}")

        validation_error = await _await_thread_completion(validate_canonical_backup, staged)
        if validation_error is not None:
            raise HTTPException(400, f"invalid restore archive — {validation_error}")

        try:
            os.replace(staged, dest)
        except OSError as e:
            raise HTTPException(500, f"could not publish upload: {e}")

        try:
            record = _kick("restore", lambda cb: run_restore(archive_name, on_progress=cb))
        except HTTPException as e:
            if e.status_code == 409:
                dest.unlink(missing_ok=True)
            raise
        return {"id": record.id, "status": "running"}
    finally:
        staged.unlink(missing_ok=True)


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
        request, "players.html", {"title": "Players"},
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
        snap = await asyncio.to_thread(
            players_svc.collect_players, **db_credentials(), presence=_presence_snapshot(),
        )
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
        request, "stats.html", {"title": "Stats"},
    )


# --- Armory (spec 5, 7, 8): character search, the character page, the 3D model API, the asset
# proxy endpoint and the footer strip. The page is rendered from DB + DBC data only and never
# waits on Wowhead; the model panel asks /api/armory/<guid>/model afterwards. Blocking work that
# can wait on Wowhead (asset serving, the model API) runs on a dedicated, bounded thread limiter
# (R21), so a black-holed upstream (serve can block 25-70 s) never starves the other routes.

ARMORY_THREAD_TOKENS = 8
ARMORY_RESOLVE_WORKERS = 4  # concurrent display-ID resolutions per model call; the proxy caps upstream at 4
ARMORY_GENERIC_ERROR = "Something went wrong — see the admin log."
ARMORY_NOT_WRITABLE = "Could not save the 3D viewer setting (the admin data folder is not writable)."
ARMORY_STATS_KEY = "PlayerSave.Stats.MinLevel"
ARMORY_MAX_QUERY = 64  # names are at most 12 characters; nothing longer can match
_ARMORY_MAX_LOGGED_OPTIONS = 256
_ArmoryGuid = Annotated[int, PathParam(ge=1, le=4294967295)]

_armory_limiter_obj: anyio.CapacityLimiter | None = None
_armory_unknown_options_logged: set[str] = set()
_armory_unknown_options_mu = threading.Lock()


def _armory_limiter() -> anyio.CapacityLimiter:
    """The armory's own thread limiter, made on first use (on the event loop, so no race)."""
    global _armory_limiter_obj
    if _armory_limiter_obj is None:
        _armory_limiter_obj = anyio.CapacityLimiter(ARMORY_THREAD_TOKENS)
    return _armory_limiter_obj


async def _armory_run(func, /, *args):
    """``func(*args)`` on a worker thread under the armory limiter; a cancelled caller still waits for it."""
    return await anyio.to_thread.run_sync(func, *args, limiter=_armory_limiter())


def _armory_stats_min_level() -> str:
    """The effective ``PlayerSave.Stats.MinLevel`` (Settings' resolved keys), or ``unset``."""
    try:
        for entry in list_keys_resolved():
            if entry.get("key") == ARMORY_STATS_KEY:
                value = str(entry.get("effective_value") or "").strip()
                return value or "unset"
    except Exception:  # noqa: BLE001 - the note is a hint; it must not break the page
        log.warning("armory: could not read %s from the resolved keys", ARMORY_STATS_KEY, exc_info=True)
    return "unset"


def _armory_blob(view, tooltips: dict) -> dict:
    """The page's embedded JSON (``#armory-data``): tooltips and paper-doll items by DB slot."""
    return {
        "guid": view.header.guid,
        "tooltips": {str(slot): asdict(tip) for slot, tip in tooltips.items()},
        "itemsBySlot": {
            str(item.slot): {
                "slot": item.slot,
                "entry": item.entry,
                "name": item.name,
                "quality": item.quality,
                "itemLevel": item.item_level,
                "inventoryType": item.inventory_type,
                "displayId": item.display_id,
                "icon": tooltips[item.slot].icon,
            }
            for item in view.items
        },
    }


def _armory_character_context(guid: int) -> dict | None:
    """Everything the character page shows, or ``None`` for an unknown guid. Blocking (DB + DBC).

    ``trees``/``glyphs`` hold one entry per talent group (spec 2 only when the character has
    two); ``stats_note`` is set only when no ``character_stats`` row exists. A DB error raises.
    """
    store = dbc_svc.store_from_env()
    view = armory_svc.collect_character(guid, **db_credentials(), store=store)
    if view is None:
        return None
    header = view.header
    tooltips = {item.slot: armory_svc.tooltip(item, store, view.items) for item in view.items}
    specs = range(2 if view.spec_count == 2 else 1)
    stats = armory_svc.stats_rows(view.stats_row, view.stats_columns, class_id=header.class_id)
    return {
        "view": view,
        "tooltips": tooltips,
        "trees": [armory_svc.talent_trees(header.class_id, view.specs[i], store) for i in specs],
        "glyphs": [armory_svc.glyph_rows(view.glyphs[i], store) for i in specs],
        "stats": stats,
        "stats_note": {"min_level": _armory_stats_min_level()} if stats is None else None,
        "dbc_missing": store.missing(),
        "armory_data": _armory_blob(view, tooltips),
    }


def _armory_page(request: Request, context: dict, *, status_code: int = 200) -> HTMLResponse:
    base = {"title": "Armory", "q": "", "view": None, "error": None, "not_found": False}
    return templates.TemplateResponse(request, "armory.html", {**base, **context}, status_code=status_code)


@app.get("/armory", response_class=HTMLResponse)
async def armory_page(request: Request, q: str = "") -> HTMLResponse:
    return _armory_page(request, {"q": q[:ARMORY_MAX_QUERY]})


# Declared before /armory/{guid}: the proxy's paths never reach the character route.
@app.get("/armory/assets/{local_path:path}")
async def armory_asset(request: Request, local_path: str) -> Response:
    """One allow-listed asset through the proxy (spec 8); anything else is a 404 without a fetch."""
    served, st = await _armory_run(_armory_serve, local_path)
    if served.status == 200:
        headers = {"Cache-Control": served.cache_control, "X-Content-Type-Options": "nosniff"}
        if served.etag:
            headers["ETag"] = served.etag
            if _etag_matches(request.headers.get("if-none-match"), served.etag):
                return Response(status_code=304, headers=headers)
        if served.path is not None:
            # Our ETag and type, never Starlette's mtime ETag (every cache hit touches the mtime).
            return FileResponse(served.path, media_type=served.content_type, headers=headers, stat_result=st)
        return Response(content=served.body or b"", media_type=served.content_type, headers=headers)
    status = served.status if served.status in (404, 503) else 503
    return Response(
        content=served.reason or ("not found" if status == 404 else "unavailable"),
        status_code=status,
        media_type="text/plain; charset=utf-8",
        headers={"Cache-Control": served.cache_control or "no-store", "X-Content-Type-Options": "nosniff"},
    )


def _armory_serve(local_path: str):
    """``proxy.serve`` plus the cached file's stat (taken here, off the event loop)."""
    served = armory_assets.get_proxy().serve(local_path)
    st = None
    if served.status == 200 and served.path is not None:
        try:
            st = os.stat(served.path)
        except OSError:  # evicted between the hit and now
            served = armory_assets.Served(503, reason="cached file went away; try again")
    return served, st


def _etag_matches(header: str | None, etag: str) -> bool:
    """``If-None-Match`` (a list, ``*`` or weak tags) names ``etag``."""
    if not header:
        return False

    def opaque(tag: str) -> str:
        tag = tag.strip()
        return tag[2:] if tag.startswith("W/") else tag

    want = opaque(etag)
    return any(part.strip() == "*" or opaque(part) == want for part in header.split(","))


@app.get("/armory/{guid}", response_class=HTMLResponse)
async def armory_character_page(request: Request, guid: _ArmoryGuid) -> HTMLResponse:
    try:
        context = await asyncio.to_thread(_armory_character_context, guid)
    except Exception as e:  # noqa: BLE001 — DB down must not 500 the page (Players convention)
        log.warning("armory: character %d could not be loaded: %s", guid, e)
        return _armory_page(request, {"error": str(e), "guid": guid})
    if context is None:
        return _armory_page(request, {"not_found": True, "guid": guid}, status_code=404)
    return _armory_page(request, {**context, "guid": guid})


@app.get("/api/armory/search", response_class=HTMLResponse)
async def api_armory_search(request: Request, q: str = "") -> HTMLResponse:
    q = q[:ARMORY_MAX_QUERY]
    hits: list = []
    error = None
    short = armory_svc.normalize_search(q) is None
    if not short:
        try:
            hits = await asyncio.to_thread(armory_svc.search_characters, q, **db_credentials())
        except Exception as e:  # noqa: BLE001 — one line in the results, never a 500
            error = str(e)
    return templates.TemplateResponse(
        request,
        "partials/armory_search.html",
        {"hits": hits, "q": q.strip(), "short": short, "error": error},
    )


def _log_unknown_options(names: list[str], model: int) -> None:
    for name in names:
        with _armory_unknown_options_mu:
            new = (
                name not in _armory_unknown_options_logged
                and len(_armory_unknown_options_logged) < _ARMORY_MAX_LOGGED_OPTIONS
            )
            if new:
                _armory_unknown_options_logged.add(name)
        if new:
            log.warning(
                "armory: customisation option %r (model %d) is not in the option-name table; "
                "it gets its first choice", name[:64], model,
            )


def _armory_customization(proxy, model: int, race: int, appearance) -> tuple[list | None, list[str], str | None]:
    """(viewer options, unknown option names, reason when there are no options)."""
    path = f"wrath/meta/charactercustomization/{model}.json"
    served = proxy.serve(path)
    if served.status != 200:
        return None, [], f"{path}: {served.reason or f'HTTP {served.status}'}"
    try:
        if served.body is not None:
            raw = served.body
        elif served.path is not None:
            raw = served.path.read_bytes()
        else:
            raise ValueError("empty answer")
        doc = json.loads(raw)
        data = doc.get("data", doc) if isinstance(doc, dict) else None  # the spike: cust.data || cust
        options_json = data.get("Options") if isinstance(data, dict) else None
        if not isinstance(options_json, list):
            raise ValueError("no Options list")
    except (OSError, ValueError, RecursionError) as e:  # ValueError: bad JSON or bad UTF-8
        log.warning("armory: %s unreadable: %s", path, e)
        return None, [], f"{path}: customisation data unreadable"
    options, unknown = armory_svc.map_customization(options_json, race, appearance)
    _log_unknown_options(unknown, model)
    return options, unknown, None


def _armory_pending_reason(count: int) -> str:
    return (
        f"Couldn't reach Wowhead's model data — {count} item{'' if count == 1 else 's'} "
        "not shown yet; try again shortly"
    )


def _armory_model(guid: int) -> dict | None:
    """The model API's answer (spec 7, R8, R12, R23), or ``None`` for an unknown guid. Blocking.

    Two SELECTs (``collect_model_inputs``): with the page's six a character view stays
    within spec 11's eight (R22). ``unresolved`` = slots Wowhead has no model for
    (definitive); ``pending`` = slots whose lookup failed for now (nothing recorded), which
    ``reason`` explains, as it does null ``options``.
    """
    inputs = armory_svc.collect_model_inputs(guid, **db_credentials())
    if inputs is None:
        return None
    header = inputs.header
    model = armory_svc.model_id(header.race, header.gender)
    proxy = armory_assets.get_proxy()
    enabled = proxy.settings.enabled()
    answer = {
        "modelId": model,
        "options": None,
        "unresolvedOptions": [],
        "items": [],
        "unresolved": [],
        "pending": [],
        "viewer": {"enabled": enabled, "scriptReady": proxy.viewer_status().size > 0},
        "sheath": False,
    }
    if not enabled:
        # Nothing is asked of Wowhead or the resolver; the panel says "switched off".
        answer["reason"] = "3D viewer is switched off"
        return answer
    options, unknown, reason = _armory_customization(proxy, model, header.race, header.appearance)
    answer["options"] = options
    answer["unresolvedOptions"] = unknown
    sent, _omitted = armory_svc.viewer_items(inputs.items)
    resolved, unresolved, pending = armory_resolver.get_resolver().resolve_items(
        sent, {item.slot: item.entry for item in inputs.items}, workers=ARMORY_RESOLVE_WORKERS
    )
    answer["items"] = [[item.inventory_type, item.display_id] for item in resolved]
    answer["unresolved"] = unresolved
    answer["pending"] = pending
    reasons = [reason] if reason is not None else []
    if pending:
        reasons.append(_armory_pending_reason(len(pending)))
    if reasons:
        answer["reason"] = "; ".join(reasons)
    return answer


@app.get("/api/armory/{guid}/model")
async def api_armory_model(guid: _ArmoryGuid) -> JSONResponse:
    try:
        answer = await _armory_run(_armory_model, guid)
    except Exception as e:  # noqa: BLE001 — the panel shows "unavailable" with the reason
        log.warning("armory: model data for character %d failed: %s", guid, e)
        return JSONResponse(
            {"detail": f"Could not load the character: {e}"},
            status_code=503,
            headers={"Cache-Control": "no-store"},
        )
    if answer is None:
        return JSONResponse({"detail": "character not found"}, status_code=404, headers={"Cache-Control": "no-store"})
    return JSONResponse(answer, headers={"Cache-Control": "no-store"})


# The footer strip: every route answers 200 with the re-rendered partial (the cloud-panel
# convention); a problem is a line inside it. The state-changing ones run through
# _await_thread_completion; status() takes the cache lock, so it runs off the event loop too.


def _armory_date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        moment = dt.datetime.fromisoformat(value)
    except ValueError:
        return value
    if moment.tzinfo is not None:
        moment = moment.astimezone(dt.timezone.utc)
    return moment.strftime("%Y-%m-%d")


async def _armory_footer_response(request: Request, *, error: str | None = None, notice: str | None = None) -> HTMLResponse:
    status = None
    try:
        status = await asyncio.to_thread(armory_assets.get_proxy().status)
    except Exception:  # noqa: BLE001
        log.exception("armory: could not read the asset proxy status")
        error = error or "Could not read the 3D viewer status — see the admin log."
    try:
        dbc_missing = await asyncio.to_thread(lambda: dbc_svc.store_from_env().missing())
    except Exception:  # noqa: BLE001
        log.exception("armory: could not read the DBC store state")
        dbc_missing = []
    return templates.TemplateResponse(
        request,
        "partials/armory_footer.html",
        {
            "status": status,
            "cache_mb": _humanize_bytes(status.cache_bytes) if status is not None else 0,
            "viewer_fetched": _armory_date(status.viewer.fetched_at) if status is not None else None,
            "dbc_missing": dbc_missing,
            "error": error,
            "notice": notice,
        },
    )


async def _armory_action(func, *args, on_oserror: str = ARMORY_GENERIC_ERROR):
    """(result, error text). ``ViewerError`` carries its own text; anything else is logged."""
    try:
        return await _await_thread_completion(func, *args), None
    except armory_assets.ViewerError as e:
        return None, e.message
    except OSError:
        log.exception("armory: footer action failed")
        return None, on_oserror
    except Exception:  # noqa: BLE001 - never echo a raw exception to the page
        log.exception("armory: footer action failed unexpectedly")
        return None, ARMORY_GENERIC_ERROR


@app.get("/api/armory/footer", response_class=HTMLResponse)
async def api_armory_footer(request: Request) -> HTMLResponse:
    return await _armory_footer_response(request)


@app.post("/api/armory/settings", response_class=HTMLResponse)
async def api_armory_settings(request: Request, enabled: str = Form("")) -> HTMLResponse:
    value = enabled.strip().lower()
    if value not in ("on", "off"):
        return await _armory_footer_response(request, error="Choose on or off for the 3D viewer.")
    _result, error = await _armory_action(
        armory_assets.get_proxy().settings.set_enabled, value == "on", on_oserror=ARMORY_NOT_WRITABLE
    )
    notice = None if error else f"3D viewer switched {value}."
    return await _armory_footer_response(request, error=error, notice=notice)


@app.post("/api/armory/cache/clear", response_class=HTMLResponse)
async def api_armory_cache_clear(request: Request) -> HTMLResponse:
    removed, error = await _armory_action(armory_assets.get_proxy().clear_cache)
    notice = None if error else f"Cleared {removed} cached file{'' if removed == 1 else 's'}; the viewer script is kept."
    return await _armory_footer_response(request, error=error, notice=notice)


def _armory_update_viewer(proxy) -> bool:
    """Update the pinned viewer script; True when anything about the pinned copy changed."""
    before = proxy.viewer_status()
    return proxy.update_viewer() != before


@app.post("/api/armory/viewer/update", response_class=HTMLResponse)
async def api_armory_viewer_update(request: Request) -> HTMLResponse:
    changed, error = await _armory_action(_armory_update_viewer, armory_assets.get_proxy())
    notice = None
    if error is None:
        notice = "Viewer script updated." if changed else "The pinned viewer script is current; nothing changed."
    return await _armory_footer_response(request, error=error, notice=notice)


@app.post("/api/armory/viewer/revert", response_class=HTMLResponse)
async def api_armory_viewer_revert(request: Request) -> HTMLResponse:
    _result, error = await _armory_action(armory_assets.get_proxy().revert_viewer)
    notice = None if error else "The previous viewer script is pinned again."
    return await _armory_footer_response(request, error=error, notice=notice)


class ProgressionApplyPayload(BaseModel):
    guid: int
    target_expansion: str


class RaidUnlockPayload(BaseModel):
    guid: int
    raid_key: str


@app.get("/progression", response_class=HTMLResponse)
async def progression_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "progression.html",
        {"title": "Progression"},
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
        result = await _await_thread_completion(lambda: progression_svc.apply_progression(
            guid=payload.guid,
            target_expansion=payload.target_expansion,
            config=cfg,
            snapshots_dir=_SNAPSHOTS,
            **db_credentials(),
        ))
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


@app.get("/api/raid-unlock/characters", response_class=HTMLResponse)
async def api_raid_unlock_characters(request: Request) -> HTMLResponse:
    rows = ()
    err = None
    try:
        rows = await asyncio.to_thread(raid_unlock_svc.collect_characters, **db_credentials())
    except Exception as e:  # noqa: BLE001
        err = str(e)
    rows_json = json.dumps([
        {"guid": r.guid, "account": r.account, "name": r.name,
         "level": r.level, "online": r.online}
        for r in rows
    ])
    return templates.TemplateResponse(
        request,
        "partials/raid_unlock_page.html",
        {
            "rows": rows,
            "rows_json": rows_json,
            "raids": raid_unlock_svc.raid_choices(),
            "error": err,
        },
    )


@app.post("/api/raid-unlock/apply")
async def api_raid_unlock_apply(payload: RaidUnlockPayload) -> dict:
    if not runner.try_acquire_mutation():
        raise HTTPException(status_code=409, detail="another destructive action is already running")
    try:
        result = await _await_thread_completion(lambda: raid_unlock_svc.send_raid_unlock(
            guid=payload.guid,
            raid_key=payload.raid_key,
            **db_credentials(),
        ))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        runner.release_mutation()
    return {
        "status": result.status,
        "message": result.message,
        "item_name": result.item_name,
        "eta": result.eta_epoch,
    }


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "title": "Settings",
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
