"""Severity rules (pure): SEV1 (worst) to SEV4.

- **R1:** the worst signal sets the base. `critical` → SEV2, `warning` → SEV3,
  `info` → SEV4.
- **R2:** a `critical` signal **and** one of the following → SEV1:
  - at least 50% of a service's requests fail;
  - 3 or more services are involved;
  - an involved entity has criticality `critical`.
- **R3:** 2 or more services involved, or an entity with criticality `high` →
  one level more severe (R3 alone never reaches SEV1).
- **R4:** every involved entity has criticality `low`, and R2 and R3 don't
  apply → one level less severe.

The incident service applies this when an incident opens and each time a
signal joins it, and only ever *raises* the severity automatically. A person
can set any severity.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from sentinel.db.models import Severity

ORDER = (Severity.SEV1, Severity.SEV2, Severity.SEV3, Severity.SEV4)
_BASE = {"critical": Severity.SEV2, "warning": Severity.SEV3, "info": Severity.SEV4}
HALF_FAILING = 0.5


@dataclass(frozen=True)
class SignalFacts:
    severity: str  # critical | warning | info
    alertname: str | None = None
    value: float | None = None  # the alert's value, e.g. the error ratio


@dataclass(frozen=True)
class Assessment:
    severity: Severity
    reasons: tuple[str, ...]


def rank(severity: Severity) -> int:
    """0 for SEV1 (worst) … 3 for SEV4."""
    return ORDER.index(severity)


def worse(a: Severity, b: Severity) -> Severity:
    return a if rank(a) <= rank(b) else b


def _shift(severity: Severity, levels: int) -> Severity:
    return ORDER[max(0, min(len(ORDER) - 1, rank(severity) + levels))]


def assess(
    signals: Sequence[SignalFacts], *, services: int, criticalities: Iterable[str | None]
) -> Assessment:
    if not signals:
        return Assessment(Severity.SEV4, ("no signals",))
    crit = list(criticalities)
    worst = min((s.severity for s in signals), key=lambda s: rank(_BASE.get(s, Severity.SEV4)))
    severity = _BASE.get(worst, Severity.SEV4)
    reasons = [f"R1: worst signal is {worst}"]

    if worst == "critical":
        why = []
        if any(
            s.alertname == "ServiceHighErrorRatio" and (s.value or 0) >= HALF_FAILING
            for s in signals
        ):
            why.append("at least half of a service's requests fail")
        if services >= 3:
            why.append(f"{services} services involved")
        if "critical" in crit:
            why.append("a critical entity is involved")
        if why:
            return Assessment(Severity.SEV1, (*reasons, "R2: " + "; ".join(why)))

    raise_by = []
    if services >= 2:
        raise_by.append(f"{services} services involved")
    if "high" in crit:
        raise_by.append("a high-criticality entity is involved")
    if raise_by:
        if rank(severity) > rank(Severity.SEV2):  # one level, but R3 alone never makes SEV1
            severity = _shift(severity, -1)
        reasons.append("R3: " + "; ".join(raise_by))
    elif crit and all(c == "low" for c in crit):
        severity = _shift(severity, +1)
        reasons.append("R4: every involved entity is low-criticality")
    return Assessment(severity, tuple(reasons))
