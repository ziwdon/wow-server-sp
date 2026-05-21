import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.state import init_state, list_keys_resolved

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
