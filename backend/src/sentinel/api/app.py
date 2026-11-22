"""FastAPI application factory."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal

import structlog
from fastapi import Depends, FastAPI, Response, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncEngine

from sentinel import __version__
from sentinel.api import errors
from sentinel.api.routers import (
    agent,
    api_keys,
    audit,
    auth,
    checks,
    ingest_auth,
    integrations,
    users,
)
from sentinel.api.schemas import MetaOut
from sentinel.api.security import authenticated, public
from sentinel.auth.ratelimit import RateLimiter
from sentinel.checks.prober_identity import ensure_prober
from sentinel.checks.state import probe_state_pass
from sentinel.config import Settings, get_settings
from sentinel.db.engine import create_engine, create_session_factory, ping
from sentinel.integrations import jobs
from sentinel.integrations.entities import prometheus_query
from sentinel.integrations.gate import IngestGate
from sentinel.log import configure_logging
from sentinel.prober.ssrf_guard import Guard


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
        try:
            app.state.prober_integration_id = await ensure_prober(
                app.state.session_factory, settings
            )
        except Exception as exc:  # the API must start (and answer /healthz) without the DB
            app.state.prober_integration_id = None
            log.warning("prober_provisioning_failed", error=type(exc).__name__)
        tasks = jobs.start(gate) if settings.background_jobs_enabled else []
        if settings.background_jobs_enabled:
            query = prometheus_query(settings.prometheus_url)
            tasks.append(
                asyncio.create_task(
                    jobs.forever(
                        "probe_state",
                        settings.probe_state_interval_s,
                        lambda: probe_state_pass(app.state.session_factory, query, gate),
                    )
                )
            )
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
    # SSRF guard for check targets at creation (tests swap in a fake resolver).
    app.state.check_guard = Guard(allow_hosts=frozenset(settings.prober_allow_hosts))
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

    for module in (auth, users, api_keys, audit, integrations, ingest_auth, agent):
        app.include_router(module.router)
    app.include_router(integrations.entities_router)
    app.include_router(checks.router)
    app.include_router(checks.prober_router)
    app.include_router(checks.events_router)

    return app
