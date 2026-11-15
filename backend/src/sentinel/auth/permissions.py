"""Permissions, roles and API-key scopes (single source of truth).

The table in docs/rbac.md is generated from this module (``make rbac-docs``),
and a test fails if the committed doc drifts from the code.
"""

from enum import StrEnum

from sentinel.db.models import UserRole


class Permission(StrEnum):
    """What a *user* may do. Checked by ``require(...)`` on every route."""

    INCIDENTS_READ = "incidents:read"
    INCIDENTS_WRITE = "incidents:write"
    PATCHES_CREATE = "patches:create"
    ACTIONS_REQUEST = "actions:request"
    ACTIONS_APPROVE = "actions:approve"
    CONFIG_MANAGE = "config:manage"
    INTEGRATIONS_MANAGE = "integrations:manage"
    USERS_MANAGE = "users:manage"
    APIKEYS_MANAGE = "apikeys:manage"
    AUDIT_READ = "audit:read"


class KeyScope(StrEnum):
    """What a *machine key* may do. Keys never get user permissions."""

    ALERTS_WRITE = "alerts:write"
    DEPLOYMENTS_WRITE = "deployments:write"
    GITHUB_WEBHOOK = "github:webhook"


_VIEWER = frozenset({Permission.INCIDENTS_READ})
_RESPONDER = _VIEWER | {
    Permission.INCIDENTS_WRITE,
    Permission.PATCHES_CREATE,
    Permission.ACTIONS_REQUEST,
}
_APPROVER = _RESPONDER | {Permission.ACTIONS_APPROVE}
_ADMIN = frozenset(Permission)

ROLE_PERMISSIONS: dict[UserRole, frozenset[Permission]] = {
    UserRole.VIEWER: _VIEWER,
    UserRole.RESPONDER: _RESPONDER,
    UserRole.APPROVER: _APPROVER,
    UserRole.ADMIN: _ADMIN,
}


def permissions_for(role: UserRole) -> frozenset[Permission]:
    return ROLE_PERMISSIONS[role]
