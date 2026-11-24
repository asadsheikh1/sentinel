"""All ORM models. Importing this package registers every table on ``Base.metadata``."""

from sentinel.db.models.checks import CheckKind, Event, EventType, HealthCheck, ProbeState
from sentinel.db.models.detection import (
    Deployment,
    ErrorGroup,
    PollWatermark,
    Signal,
    SignalKind,
    SignalStatus,
)
from sentinel.db.models.identity import ApiKey, Session, User, UserInvite, UserRole
from sentinel.db.models.incidents import (
    Incident,
    IncidentEntity,
    IncidentEvent,
    IncidentSignal,
    IncidentStatus,
    Severity,
)
from sentinel.db.models.integrations import (
    SIGNALS,
    Entity,
    EntityRelation,
    EntityType,
    IngestToken,
    Integration,
    IntegrationKind,
    IntegrationSignalStatus,
    IntegrationStatus,
)
from sentinel.db.models.platform import JOB_STATUSES, ActorType, AuditLog, Job

__all__ = [
    "JOB_STATUSES",
    "SIGNALS",
    "ActorType",
    "ApiKey",
    "AuditLog",
    "CheckKind",
    "Deployment",
    "Entity",
    "EntityRelation",
    "EntityType",
    "ErrorGroup",
    "Event",
    "EventType",
    "HealthCheck",
    "Incident",
    "IncidentEntity",
    "IncidentEvent",
    "IncidentSignal",
    "IncidentStatus",
    "IngestToken",
    "Integration",
    "IntegrationKind",
    "IntegrationSignalStatus",
    "IntegrationStatus",
    "Job",
    "PollWatermark",
    "ProbeState",
    "Session",
    "Severity",
    "Signal",
    "SignalKind",
    "SignalStatus",
    "User",
    "UserInvite",
    "UserRole",
]
