"""Step 12, pure parts: the state machine, severity rules, topology and correlation matching."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from sentinel.db.models import IncidentStatus as S
from sentinel.db.models import Severity
from sentinel.detection.correlator import IncidentView, SignalView, match, reachable, related
from sentinel.incidents.lifecycle import (
    ACTIVE,
    TRANSITIONS,
    InvalidTransitionError,
    can_transition,
    check_transition,
)
from sentinel.incidents.severity import SignalFacts, assess, rank, worse

T0 = datetime(2026, 11, 25, 10, 0, tzinfo=UTC)
WINDOW = timedelta(minutes=15)
DEPLOY = timedelta(minutes=60)

# ------------------------------------------------------------------ state machine

ALLOWED = {
    (S.OPEN, S.INVESTIGATING), (S.OPEN, S.MITIGATED), (S.OPEN, S.RESOLVED),
    (S.INVESTIGATING, S.MITIGATED), (S.INVESTIGATING, S.RESOLVED),
    (S.MITIGATED, S.OPEN), (S.MITIGATED, S.INVESTIGATING), (S.MITIGATED, S.RESOLVED),
    (S.RESOLVED, S.CLOSED), (S.RESOLVED, S.OPEN),
    (S.FALSE_POSITIVE, S.OPEN),
} | {(s, S.FALSE_POSITIVE) for s in S if s is not S.FALSE_POSITIVE}  # fmt: skip


@pytest.mark.parametrize("current", list(S))
@pytest.mark.parametrize("target", list(S))
def test_every_transition_is_allowed_or_refused_as_designed(current: S, target: S) -> None:
    if current is target:
        return
    assert can_transition(current, target) is ((current, target) in ALLOWED)
    if (current, target) in ALLOWED:
        check_transition(current, target)
    else:
        with pytest.raises(InvalidTransitionError):
            check_transition(current, target)


def test_the_happy_path_and_false_positive_from_anywhere() -> None:
    path = [S.OPEN, S.INVESTIGATING, S.MITIGATED, S.RESOLVED, S.CLOSED]
    for a, b in zip(path, path[1:], strict=False):  # noqa: RUF007
        assert can_transition(a, b)
    assert all(can_transition(s, S.FALSE_POSITIVE) for s in S if s is not S.FALSE_POSITIVE)


def test_a_closed_incident_cannot_be_reopened() -> None:
    assert TRANSITIONS[S.CLOSED] == {S.FALSE_POSITIVE}
    assert {S.OPEN, S.INVESTIGATING, S.MITIGATED} == ACTIVE


# ------------------------------------------------------------------ severity

CRIT, WARN, INFO = SignalFacts("critical"), SignalFacts("warning"), SignalFacts("info")
ERR_100 = SignalFacts("critical", "ServiceHighErrorRatio", 1.0)
ERR_20 = SignalFacts("critical", "ServiceHighErrorRatio", 0.2)


@pytest.mark.parametrize(
    ("signals", "services", "crit", "expected", "rule"),
    [
        ([CRIT], 1, [], Severity.SEV2, "R1"),
        ([WARN], 1, [], Severity.SEV3, "R1"),
        ([INFO], 0, [], Severity.SEV4, "R1"),
        ([WARN, CRIT, INFO], 1, [], Severity.SEV2, "R1"),
        ([ERR_100], 1, [], Severity.SEV1, "R2"),  # S01: every order fails
        ([ERR_20], 1, [], Severity.SEV2, "R1"),
        ([CRIT], 3, [], Severity.SEV1, "R2"),
        ([CRIT], 1, ["critical"], Severity.SEV1, "R2"),
        ([WARN], 2, [], Severity.SEV2, "R3"),
        ([WARN], 1, ["high"], Severity.SEV2, "R3"),
        ([INFO], 2, [], Severity.SEV3, "R3"),
        ([CRIT], 2, [], Severity.SEV2, "R3"),  # R3 alone never makes SEV1
        ([WARN], 1, ["low"], Severity.SEV4, "R4"),
        ([INFO], 1, ["low", "low"], Severity.SEV4, "R4"),  # already the lowest
        ([WARN], 1, ["low", None], Severity.SEV3, "R1"),  # not *every* entity is low
        ([WARN], 1, ["critical"], Severity.SEV3, "R1"),  # "critical" entity needs a critical signal
    ],
)
def test_severity_rule_table(
    signals: list[SignalFacts], services: int, crit: list[str | None], expected: Severity, rule: str
) -> None:
    result = assess(signals, services=services, criticalities=crit)
    assert result.severity is expected
    assert any(r.startswith(rule) for r in result.reasons), result.reasons


def test_severity_order() -> None:
    assert rank(Severity.SEV1) < rank(Severity.SEV4)
    assert worse(Severity.SEV3, Severity.SEV1) is Severity.SEV1
    assert assess([], services=0, criticalities=[]).severity is Severity.SEV4


# ------------------------------------------------------------------ topology

HOST, CONTAINER, ORDERS, GATEWAY, PAYMENTS, OTHER_SVC, OTHER_HOST = (uuid.uuid4() for _ in range(7))
# orders runs_on its container, which runs_on the host; another service shares the host;
# the gateway uses orders, which uses payments.
EDGES = [
    (ORDERS, CONTAINER), (CONTAINER, HOST), (OTHER_SVC, HOST),
    (GATEWAY, ORDERS), (ORDERS, PAYMENTS),
]  # fmt: skip
FORWARD: dict[uuid.UUID, frozenset[uuid.UUID]] = {}
BACKWARD: dict[uuid.UUID, frozenset[uuid.UUID]] = {}
for a, b in EDGES:
    FORWARD[a] = FORWARD.get(a, frozenset()) | {b}
    BACKWARD[b] = BACKWARD.get(b, frozenset()) | {a}


def test_reachability_follows_direction_and_hop_limit() -> None:
    assert reachable(FORWARD, ORDERS, {HOST})
    assert reachable(FORWARD, GATEWAY, {HOST})  # gateway → orders → container → host
    assert not reachable(FORWARD, HOST, {ORDERS})
    assert not reachable(FORWARD, GATEWAY, {HOST}, hops=2)


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        (HOST, ORDERS, True),  # host disk full ↔ a service on it (S13)
        (ORDERS, HOST, True),
        (GATEWAY, ORDERS, True),  # a caller and the service it calls
        (PAYMENTS, ORDERS, True),  # S05: the dependency and its caller
        (OTHER_SVC, ORDERS, False),  # two services that only share a host
        (OTHER_HOST, ORDERS, False),
    ],
)
def test_related(a: uuid.UUID, b: uuid.UUID, expected: bool) -> None:
    assert related(FORWARD, BACKWARD, a, {b}) is expected


# ------------------------------------------------------------------ matching


def _sig(entity: uuid.UUID | None, service: str | None = None, at: datetime = T0,
         traces: frozenset[str] = frozenset()) -> SignalView:  # fmt: skip
    return SignalView(uuid.uuid4(), entity, service, at, traces)


def _inc(*entities: uuid.UUID, services: set[str] | None = None, last: datetime = T0,
         traces: set[str] | None = None, release: set[str] | None = None,
         deployed_at: datetime | None = None) -> IncidentView:  # fmt: skip
    return IncidentView(
        uuid.uuid4(),
        last,
        set(entities),
        services or set(),
        traces or set(),
        release or set(),
        deployed_at,
    )


def _match(sig: SignalView, *incidents: IncidentView) -> tuple[IncidentView, str] | None:
    return match(sig, incidents, FORWARD, BACKWARD, window=WINDOW, deploy_lookback=DEPLOY)


def test_same_entity_wins_over_weaker_reasons() -> None:
    inc = _inc(ORDERS, services={"orders"})
    found = _match(_sig(ORDERS, "orders"), inc)
    assert found == (inc, "same_entity")


def test_host_disk_and_a_service_on_it_are_one_incident() -> None:
    service_incident = _inc(ORDERS)
    found = _match(_sig(HOST), service_incident)
    assert found == (service_incident, "related_entity")


def test_an_unrelated_host_opens_its_own_incident() -> None:
    assert _match(_sig(OTHER_HOST), _inc(ORDERS)) is None


def test_two_services_on_one_host_are_not_merged() -> None:
    assert _match(_sig(OTHER_SVC, "other"), _inc(ORDERS, services={"orders"})) is None


def test_the_window_closes_after_15_minutes() -> None:
    inc = _inc(ORDERS, last=T0)
    assert _match(_sig(ORDERS, at=T0 + timedelta(minutes=14)), inc) is not None
    assert _match(_sig(ORDERS, at=T0 + timedelta(minutes=16)), inc) is None


def test_shared_trace_and_service_name_without_an_entity() -> None:
    inc = _inc(ORDERS, services={"orders"}, traces={"t1"})
    assert _match(_sig(None, "orders"), inc) == (inc, "same_service")
    assert _match(_sig(OTHER_HOST, traces=frozenset({"t1"})), inc) == (inc, "shared_trace")


def test_deploy_window_needs_the_same_release_and_time() -> None:
    inc = _inc(ORDERS, release={"orders", "worker"}, deployed_at=T0 - timedelta(minutes=5))
    worker = uuid.uuid4()
    assert _match(_sig(worker, "worker"), inc) == (inc, "deploy_window")
    assert _match(_sig(worker, "unrelated"), inc) is None
    before_deploy = _sig(worker, "worker", at=T0 - timedelta(minutes=10))
    assert _match(before_deploy, inc) is None


def test_the_most_recent_matching_incident_wins() -> None:
    old = _inc(ORDERS, last=T0 - timedelta(minutes=10))
    new = _inc(ORDERS, last=T0 - timedelta(minutes=1))
    assert _match(_sig(ORDERS), old, new) == (new, "same_entity")
