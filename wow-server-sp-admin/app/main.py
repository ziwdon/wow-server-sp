import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import datetime as dt

from app.services import backups as backups_svc
from app.services import db_stats
from app.services import docker_client
from app.services import logs as logs_svc
from app.state import db_credentials, init_state, list_keys_resolved

APP_DIR = Path(__file__).resolve().parent
_AC_STACK = Path(os.environ.get("AC_STACK_DIR", "/ac"))
_SNAPSHOTS = Path(os.environ.get("ADMIN_SNAPSHOTS_DIR", "/admin-snapshots"))
log = logging.getLogger(__name__)


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
    # Task 26 appends snapshot GC here.
    yield


app = FastAPI(title="azerothcore-admin", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=1024)
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))


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
            "started_at": info.started_at,
            "exit_code": info.exit_code,
        },
    )


def _humanize_bytes(b: int) -> int:
    return round(b / (1024 * 1024))


def _humanize_uptime(started_at: str | None) -> str:
    if not started_at:
        return "—"
    try:
        started = dt.datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    except ValueError:
        return "—"
    delta = dt.datetime.now(dt.timezone.utc) - started
    hours, rem = divmod(int(delta.total_seconds()), 3600)
    minutes = rem // 60
    return f"{hours}h {minutes}m"


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
            "server_lines": logs_svc.tail_filtered(logs_dir / "Server.log", n=20),
            "pb_lines": logs_svc.tail_filtered(logs_dir / "Playerbots.log", n=20),
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
        human = dt.datetime.fromtimestamp(s.last_backup_unix).strftime("%Y-%m-%d %H:%M")
    return templates.TemplateResponse(
        "partials/backups.html",
        {
            "request": request,
            "last_backup_human": human,
            "last_error": s.last_error,
        },
    )
