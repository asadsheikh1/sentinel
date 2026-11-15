"""FastAPI application factory."""

from typing import Literal

import structlog
from fastapi import FastAPI
from pydantic import BaseModel

from sentinel import __version__
from sentinel.config import Settings, get_settings
from sentinel.log import configure_logging


class HealthResponse(BaseModel):
    status: Literal["ok"]
    version: str
    env: str


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)

    app = FastAPI(
        title="SENTINEL API",
        version=__version__,
        # Interactive docs only outside production.
        docs_url=None if settings.is_prod else "/api/v1/docs",
        redoc_url=None,
        openapi_url=None if settings.is_prod else "/api/v1/openapi.json",
    )
    app.state.settings = settings

    @app.get("/healthz", response_model=HealthResponse, tags=["health"])
    async def healthz() -> HealthResponse:
        """Liveness check. Does not touch the database (that is ``/readyz``, Step 2)."""
        return HealthResponse(status="ok", version=__version__, env=settings.env.value)

    structlog.get_logger("sentinel.api").info(
        "app_created", env=settings.env.value, version=__version__
    )
    return app
