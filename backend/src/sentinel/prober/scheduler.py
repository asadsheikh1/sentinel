"""Jittered scheduling: each check runs every ``interval_s``, spread out in time.

* A new (or changed) check first runs within a few seconds, at a random offset,
  so many checks added at once don't fire together.
* After that, each run is planned one interval after the previous *planned* time
  (no drift from run duration), plus or minus 5% jitter.
* If the prober falls behind (e.g. it was paused), a late check runs once and is
  re-planned from now: no burst of catch-up runs.

Pure logic with an injected clock and random source, so it's tested without
waiting.
"""

import random
from collections.abc import Iterable

from sentinel.prober.checks import CheckSpec

FIRST_RUN_SPREAD_S = 5.0
JITTER = 0.05


class Schedule:
    def __init__(self, rng: random.Random | None = None) -> None:
        self._rng = rng or random.Random()  # noqa: S311 - jitter, not security
        self.specs: dict[str, CheckSpec] = {}
        self.next_at: dict[str, float] = {}

    def update(self, specs: Iterable[CheckSpec], now: float) -> None:
        """Replace the set of checks. Unchanged checks keep their timing."""
        incoming = {s.id: s for s in specs}
        for check_id in list(self.specs):
            if check_id not in incoming:
                del self.specs[check_id]
                self.next_at.pop(check_id, None)
        for check_id, spec in incoming.items():
            if self.specs.get(check_id) != spec:
                self.specs[check_id] = spec
                spread = min(float(spec.interval_s), FIRST_RUN_SPREAD_S)
                self.next_at[check_id] = now + self._rng.uniform(0, spread)

    def _jittered(self, interval_s: int) -> float:
        return interval_s * (1 + self._rng.uniform(-JITTER, JITTER))

    def due(self, now: float) -> list[CheckSpec]:
        """Checks to run now; their next run is planned as they are returned."""
        ready: list[CheckSpec] = []
        for check_id, at in sorted(self.next_at.items(), key=lambda kv: kv[1]):
            if at > now:
                break
            spec = self.specs[check_id]
            ready.append(spec)
            planned = at + self._jittered(spec.interval_s)
            if planned <= now:  # we fell behind: don't catch up in a burst
                planned = now + self._jittered(spec.interval_s)
            self.next_at[check_id] = planned
        return ready

    def sleep_for(self, now: float, maximum: float = 1.0) -> float:
        if not self.next_at:
            return maximum
        return max(0.0, min(maximum, min(self.next_at.values()) - now))
