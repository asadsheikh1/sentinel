"""FastAPI application factory."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal

import structlog
from fastapi import Depends, FastAPI, Response, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncEngine

from sentinel import __version__
from sentinel.api import errors
from sentinel.api.routers import api_keys, audit, auth, ingest_auth, integrations, users
from sentinel.api.schemas import MetaOut
from sentinel.api.security import authenticated, public
from sentinel.auth.ratelimit import RateLimiter
from sentinel.config import Settings, get_settings
from sentinel.db.engine import create_engine, create_session_factory, ping
from sentinel.integrations import jobs
from sentinel.integrations.gate import IngestGate
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
        gate = IngestGate(settings, app.state.session_factory)
        app.state.ingest_gate = gate
        tasks = jobs.start(gate) if settings.background_jobs_enabled else []
        log.info("app_started", env=settings.env.value, version=__version__)
        try:
            yield
        finally:
            if tasks:
                await jobs.stop(tasks, gate)
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
    app.state.login_limiter = RateLimiter(settings.login_rate_limit_per_minute, 60)
    errors.install(app)

    @app.get(
        "/healthz", response_model=HealthResponse, tags=["health"], dependencies=[Depends(public())]
    )
    async def healthz() -> HealthResponse:
        """Liveness: the process is up. Does not touch the database."""
        return HealthResponse(status="ok", version=__version__, env=settings.env.value)

    @app.get(
        "/readyz",
        response_model=ReadyResponse,
        tags=["health"],
        responses={503: {"model": ReadyResponse}},
        dependencies=[Depends(public())],
    )
    async def readyz(response: Response) -> ReadyResponse:
        """Readiness: the database answers. Returns 503 when it doesn't."""
        if await ping(app.state.engine):
            return ReadyResponse(status="ready", database="ok")
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return ReadyResponse(status="not_ready", database="unavailable")

    @app.get(
        "/api/v1/meta",
        response_model=MetaOut,
        tags=["meta"],
        dependencies=[Depends(authenticated())],
    )
    async def meta() -> MetaOut:
        return MetaOut(version=__version__, env=settings.env.value)

    for module in (auth, users, api_keys, audit, integrations, ingest_auth):
        app.include_router(module.router)
    app.include_router(integrations.entities_router)

    return app
