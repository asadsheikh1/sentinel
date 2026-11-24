"""The incident state machine (pure).

    open → investigating → mitigated → resolved → closed
    false_positive: from any state

Also allowed, for real life:
- mitigated → open: it came back (a new firing signal re-opens it automatically).
- resolved → open: it wasn't fixed after all.
- open → mitigated / resolved, investigating → resolved: skipping a stage.
- false_positive → open: marked by mistake.

Every transition is recorded on the timeline by the incident service.
"""

from sentinel.db.models import IncidentStatus as S

TRANSITIONS: dict[S, frozenset[S]] = {
    S.OPEN: frozenset({S.INVESTIGATING, S.MITIGATED, S.RESOLVED, S.FALSE_POSITIVE}),
    S.INVESTIGATING: frozenset({S.MITIGATED, S.RESOLVED, S.FALSE_POSITIVE}),
    S.MITIGATED: frozenset({S.OPEN, S.INVESTIGATING, S.RESOLVED, S.FALSE_POSITIVE}),
    S.RESOLVED: frozenset({S.CLOSED, S.OPEN, S.FALSE_POSITIVE}),
    S.CLOSED: frozenset({S.FALSE_POSITIVE}),
    S.FALSE_POSITIVE: frozenset({S.OPEN}),
}

# Incidents that new signals can still join.
ACTIVE = frozenset({S.OPEN, S.INVESTIGATING, S.MITIGATED})


class InvalidTransitionError(ValueError):
    def __init__(self, current: S, target: S) -> None:
        super().__init__(f"An incident can't go from {current.value} to {target.value}.")
        self.current = current
        self.target = target


def can_transition(current: S, target: S) -> bool:
    return target in TRANSITIONS[current]


def check_transition(current: S, target: S) -> None:
    if not can_transition(current, target):
        raise InvalidTransitionError(current, target)
