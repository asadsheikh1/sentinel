"""/internal/ingest-auth: Caddy's forward_auth target for every OTLP request."""

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request, Response, status

from sentinel.api.security import internal_only
from sentinel.integrations.gate import SIGNAL_BY_PATH, IngestGate
from sentinel.integrations.tokens import bearer_token

router = APIRouter(tags=["internal"], include_in_schema=False)


@router.get("/internal/ingest-auth", dependencies=[Depends(internal_only())])
async def ingest_auth(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    forwarded_uri: Annotated[str | None, Header(alias="X-Forwarded-Uri")] = None,
) -> Response:
    """200 + identity headers, 401 (bad token) or 429 (over the token's rate limit)."""
    gate: IngestGate = request.app.state.ingest_gate
    path = (forwarded_uri or "").split("?", 1)[0]
    result = await gate.check(bearer_token(authorization), SIGNAL_BY_PATH.get(path))
    if result.status == status.HTTP_429_TOO_MANY_REQUESTS:
        return Response(
            status_code=result.status, headers={"Retry-After": str(result.retry_after_s or 60)}
        )
    if result.status != status.HTTP_200_OK or result.info is None:
        return Response(
            status_code=status.HTTP_401_UNAUTHORIZED, headers={"WWW-Authenticate": "Bearer"}
        )
    return Response(
        status_code=status.HTTP_200_OK,
        headers={
            "X-Sentinel-Integration-Id": str(result.info.integration_id),
            "X-Sentinel-Integration-Kind": result.info.kind,
        },
    )
