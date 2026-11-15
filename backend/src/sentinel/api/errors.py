"""RFC 9457 problem details (``application/problem+json``) for every error."""

from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

PROBLEM_JSON = "application/problem+json"


def problem(
    status: int,
    detail: str | None = None,
    *,
    instance: str | None = None,
    errors: list[dict[str, Any]] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {
        "type": "about:blank",
        "title": HTTPStatus(status).phrase,
        "status": status,
    }
    if detail:
        body["detail"] = detail
    if instance:
        body["instance"] = instance
    if errors:
        body["errors"] = errors
    return JSONResponse(body, status_code=status, media_type=PROBLEM_JSON, headers=headers)


def install(app: FastAPI) -> None:
    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        detail = exc.detail if isinstance(exc.detail, str) else None
        return problem(
            exc.status_code,
            detail,
            instance=request.url.path,
            headers=dict(exc.headers) if exc.headers else None,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            {
                "loc": list(e.get("loc", ())),
                "msg": e.get("msg"),
                "type": e.get("type"),
            }
            for e in exc.errors()
        ]  # input values are omitted on purpose: they may contain passwords
        return problem(422, "Request validation failed.", instance=request.url.path, errors=errors)
