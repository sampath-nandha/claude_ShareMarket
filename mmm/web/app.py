"""FastAPI application: REST for page data, one WebSocket for live ticks, static single-page UI.

Run: `mmm dashboard` (or `uvicorn mmm.web.app:app --port 8501`). Binds to 127.0.0.1 by default.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import structlog
from fastapi import FastAPI, Request, WebSocket
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from mmm import __version__
from mmm.web.live import TickHub
from mmm.web.routes import market, options, research, signals, system

log = structlog.get_logger(__name__)
STATIC = Path(__file__).parent / "static"


def create_app(live: bool = True) -> FastAPI:
    hub = TickHub()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if live:
            hub.start()
        yield
        await hub.stop()

    app = FastAPI(title="MoneyMakingMachine", version=__version__, lifespan=lifespan, docs_url="/api/docs", redoc_url=None)
    app.state.hub = hub
    app.add_middleware(GZipMiddleware, minimum_size=2048)
    for r in (market.router, options.router, signals.router, research.router, system.router):
        app.include_router(r)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/api/version")
    def version():
        return {"version": __version__, "read_only": True}

    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        await hub.handle(websocket)

    @app.exception_handler(Exception)
    async def errors(_req: Request, exc: Exception):
        log.error("web_error", error=str(exc), kind=type(exc).__name__)
        return JSONResponse({"error": f"{type(exc).__name__}: {exc}"}, status_code=500)

    return app


app = create_app()
