"""/agent/*: the SENTINEL Agent installer files. Public: they contain no secrets.

Agents reach these through the ingest endpoint (Caddy), the only SENTINEL
address a monitored host needs.
"""

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import PlainTextResponse

from sentinel.api.security import public
from sentinel.integrations.agent_config import default_agent_dir

router = APIRouter(prefix="/agent", tags=["agent"])

# Fixed allowlist: the name is never used to build a path on its own.
FILES = {
    "install.sh": ("install.sh", "text/x-shellscript"),
    "uninstall.sh": ("uninstall.sh", "text/x-shellscript"),
    "socket-proxy.cfg": ("socket-proxy.cfg", "text/plain"),
    "Containerfile": ("Containerfile", "text/plain"),
}


@router.get("/{name}", response_class=PlainTextResponse, dependencies=[Depends(public())])
async def agent_file(name: str, request: Request) -> PlainTextResponse:
    entry = FILES.get(name)
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found.")
    configured = request.app.state.settings.agent_dir
    directory = Path(configured) if configured else default_agent_dir()
    try:
        text = (directory / entry[0]).read_text(encoding="utf-8")
    except FileNotFoundError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found.") from e
    return PlainTextResponse(text, media_type=entry[1], headers={"Cache-Control": "no-cache"})
