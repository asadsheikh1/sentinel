"""Investigation jobs: queued here, run by the AI investigator (Step 17).

``enqueue`` puts one ``investigate`` job per incident and trigger on the
``investigations`` queue (de-duplicated). Nothing runs these jobs until the
worker and the investigator arrive in Step 17; ``handle`` is the stub they
will replace.
"""

import uuid

import structlog
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from sentinel.db.models import Job

log = structlog.get_logger("sentinel.incidents")

QUEUE = "investigations"
KIND = "investigate"


async def enqueue(db: AsyncSession, incident_id: uuid.UUID, *, trigger: str = "auto") -> bool:
    """True if a new job was queued (False: one for this incident and trigger exists)."""
    stmt = (
        insert(Job)
        .values(
            queue=QUEUE,
            kind=KIND,
            payload={"incident_id": str(incident_id), "trigger": trigger},
            dedupe_key=f"{KIND}:{incident_id}:{trigger}",
        )
        .on_conflict_do_nothing(index_elements=["dedupe_key"])
        .returning(Job.id)
    )
    return (await db.execute(stmt)).scalar_one_or_none() is not None


async def handle(payload: dict[str, str]) -> None:
    """Stub until Step 17 (the LangGraph investigator)."""
    log.info("investigation_not_available_yet", incident_id=payload.get("incident_id"))
