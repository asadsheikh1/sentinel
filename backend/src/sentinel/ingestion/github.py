"""GitHub webhooks: ``deployment_status``, ``push`` and ``workflow_run`` → events.

The signature (``X-Hub-Signature-256``, HMAC-SHA256 of the raw body with the
shared secret) is checked by the API before anything here runs. Deliveries are
de-duplicated by GitHub's delivery ID; a deployment's later statuses update the
same deployment.
"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from sentinel.db.models import EventType
from sentinel.events.store import record_event
from sentinel.ingestion.deployments import DeploymentIn, record_deployment, update_status
from sentinel.security.redaction import redact_text

HANDLED = frozenset({"deployment_status", "push", "workflow_run", "ping"})
MAX_COMMITS = 20
_STATUS = {"success": "succeeded", "failure": "failed", "error": "failed"}


def _when(value: Any) -> datetime:
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                return parsed
        except ValueError:
            pass
    return datetime.now(UTC)


def _first_line(message: Any, limit: int = 200) -> str:
    line = str(message or "").splitlines()[0] if message else ""
    return redact_text(line[:limit]).text


async def _deployment_status(db: AsyncSession, body: dict[str, Any]) -> str:
    deployment = body["deployment"]
    status = body["deployment_status"]
    repo = body["repository"]
    state = str(status.get("state", ""))
    mapped = _STATUS.get(state, "in_progress")
    custom = deployment.get("payload") if isinstance(deployment.get("payload"), dict) else {}
    service = str(custom.get("service") or repo["name"])
    key = f"github:deployment:{repo['id']}:{deployment['id']}"
    data = DeploymentIn(
        service=service,
        version=str(deployment.get("ref") or "")[:200] or None,
        commit_sha=deployment.get("sha"),
        deployed_at=_when(status.get("created_at") or deployment.get("created_at")),
        deployed_by=str((deployment.get("creator") or {}).get("login") or "")[:200] or None,
        environment=str(deployment.get("environment") or "production")[:100],
        source="github",
        status=mapped,
    )
    _, created = await record_deployment(db, data, key=key)
    if not created:
        await update_status(db, key, mapped)  # type: ignore[arg-type]
        return "updated"
    return "created"


async def _push(db: AsyncSession, body: dict[str, Any], delivery: str) -> str:
    repo = body["repository"]
    commits = body.get("commits") or []
    head = body.get("head_commit") or {}
    _, created = await record_event(
        db,
        type=EventType.PUSH,
        occurred_at=_when(head.get("timestamp")),
        dedupe_key=f"github:delivery:{delivery}",
        severity="info",
        payload={
            "repository": repo["full_name"],
            "ref": body.get("ref"),
            "before": body.get("before"),
            "after": body.get("after"),
            "forced": bool(body.get("forced")),
            "pusher": (body.get("pusher") or {}).get("name"),
            "commit_count": len(commits),
            "commits": [
                {
                    "sha": c.get("id"),
                    "message": _first_line(c.get("message")),
                    "author": (c.get("author") or {}).get("name"),
                }
                for c in commits[:MAX_COMMITS]
            ],
        },
    )
    return "created" if created else "duplicate"


async def _workflow_run(db: AsyncSession, body: dict[str, Any], delivery: str) -> str:
    if body.get("action") != "completed":
        return "ignored"
    run = body["workflow_run"]
    conclusion = run.get("conclusion")
    _, created = await record_event(
        db,
        type=EventType.WORKFLOW_RUN,
        occurred_at=_when(run.get("updated_at")),
        dedupe_key=f"github:delivery:{delivery}",
        severity="warning" if conclusion in {"failure", "timed_out", "startup_failure"} else "info",
        payload={
            "repository": body["repository"]["full_name"],
            "workflow": run.get("name"),
            "conclusion": conclusion,
            "head_sha": run.get("head_sha"),
            "head_branch": run.get("head_branch"),
            "event": run.get("event"),
            "run_number": run.get("run_number"),
            "run_attempt": run.get("run_attempt"),
            "url": run.get("html_url"),
        },
    )
    return "created" if created else "duplicate"


async def handle(db: AsyncSession, event: str, delivery: str, body: dict[str, Any]) -> str:
    """Process one delivery. Returns what happened (for the response and the tests)."""
    if event == "ping":
        return "pong"
    if event == "deployment_status":
        return await _deployment_status(db, body)
    if event == "push":
        return await _push(db, body, delivery)
    if event == "workflow_run":
        return await _workflow_run(db, body, delivery)
    return "ignored"
