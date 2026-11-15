"""FastAPI application factory."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal

import structlog
from fastapi import FastAPI, Response, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncEngine

from sentinel import __version__
from sentinel.config import Settings, get_settings
from sentinel.db.engine import create_engine, create_session_factory, ping
from sentinel.log import configure_logging


class HealthResponse(BaseModel):
    status: Literal["ok"]
    version: str
    env: str


class ReadyResponse(BaseModel):
    status: Literal["ready", "not_ready"]
    database: Literal["ok", "unavailable"]


def create_app(settings: Settings | None = None, *, engine: AsyncEngine | None = None) -> FastAPI:
    """Build the app.

    ``engine`` can be injected (tests). Otherwise one is created from settings
    at startup and disposed at shutdown. Creating an engine does not connect,
    so the API still starts (and ``/healthz`` still answers) if Postgres is down.
    """
    settings = settings or get_settings()
    configure_logging(settings)
    log = structlog.get_logger("sentinel.api")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owned = engine is None
        db_engine = engine or create_engine(settings)
        app.state.engine = db_engine
        app.state.session_factory = create_session_factory(db_engine)
        log.info("app_started", env=settings.env.value, version=__version__)
        try:
            yield
        finally:
            if owned:
                await db_engine.dispose()
            log.info("app_stopped")

    app = FastAPI(
        title="SENTINEL API",
        version=__version__,
        lifespan=lifespan,
        # Interactive docs only outside production.
        docs_url=None if settings.is_prod else "/api/v1/docs",
        redoc_url=None,
        openapi_url=None if settings.is_prod else "/api/v1/openapi.json",
    )
    app.state.settings = settings

    @app.get("/healthz", response_model=HealthResponse, tags=["health"])
    async def healthz() -> HealthResponse:
        """Liveness: the process is up. Does not touch the database."""
        return HealthResponse(status="ok", version=__version__, env=settings.env.value)

    @app.get(
        "/readyz",
        response_model=ReadyResponse,
        tags=["health"],
        responses={503: {"model": ReadyResponse}},
    )
    async def readyz(response: Response) -> ReadyResponse:
        """Readiness: the database answers. Returns 503 when it doesn't."""
        if await ping(app.state.engine):
            return ReadyResponse(status="ready", database="ok")
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return ReadyResponse(status="not_ready", database="unavailable")

    return app
