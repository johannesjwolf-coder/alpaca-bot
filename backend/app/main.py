"""FastAPI-Anwendung: API unter /api, gebautes Frontend unter / (gleiche Origin, kein CORS)."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import __version__
from .api import router
from .config import Settings, load_settings
from .core import AppContext
from .db import Database
from .security import OriginCsrfMiddleware

log = logging.getLogger("alpaca_bot")


def create_app(
    settings: Settings | None = None,
    *,
    context_factory: Callable[[Settings, Database], AppContext] | None = None,
    background: bool = True,
) -> FastAPI:
    settings = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        db = Database(settings.db_path)
        ctx = context_factory(settings, db) if context_factory else AppContext(settings, db)
        app.state.ctx = ctx
        await ctx.startup(background=background)
        try:
            yield
        finally:
            await ctx.shutdown()
            db.close()

    app = FastAPI(
        title="Alpaca Paper Bot",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.include_router(router)
    # Reihenfolge: zuletzt hinzugefügt = zuerst ausgeführt.
    app.add_middleware(OriginCsrfMiddleware, allowed_origins=settings.allowed_origins)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)

    dist: Path = settings.frontend_dist
    index = dist / "index.html"
    if (dist / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str):  # noqa: ANN202
        if path.startswith("api/"):
            return JSONResponse({"detail": "Nicht gefunden"}, status_code=404)
        candidate = (dist / path).resolve()
        if path and candidate.is_file() and dist.resolve() in candidate.parents:
            return FileResponse(candidate)
        if index.is_file():
            return FileResponse(index)
        return JSONResponse(
            {"detail": "Frontend nicht gebaut. Im Ordner frontend 'npm run build' ausführen oder den Vite-Dev-Server nutzen."},
            status_code=404,
        )

    return app
