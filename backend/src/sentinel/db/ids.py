"""UUIDv7 generation (RFC 9562).

Python 3.12's ``uuid`` module has no ``uuid7`` (it arrives in 3.14), so we
generate it here. UUIDv7 puts a millisecond Unix timestamp in the top 48 bits,
which keeps primary keys roughly time-ordered: good for B-tree locality and for
"newest first" listings without an extra index.

Layout (128 bits)::

    unix_ts_ms (48) | ver=7 (4) | rand_a (12) | var=0b10 (2) | rand_b (62)

``rand_a`` holds a per-millisecond counter so IDs generated in the same process
within one millisecond are still strictly increasing (RFC 9562 §6.2, method 1).
"""

import os
import threading
import time
import uuid

_lock = threading.Lock()
_last_ms = -1
_counter = 0
_COUNTER_MAX = 0xFFF  # 12 bits


def uuid7(timestamp_ms: int | None = None) -> uuid.UUID:
    """Return a new, monotonically increasing (per process) UUIDv7."""
    global _last_ms, _counter
    with _lock:
        ms = time.time_ns() // 1_000_000 if timestamp_ms is None else timestamp_ms
        if ms <= _last_ms:
            # Same millisecond (or the clock went backwards): keep _last_ms and bump the counter.
            ms = _last_ms
            _counter += 1
            if _counter > _COUNTER_MAX:
                # Counter exhausted: borrow the next millisecond.
                ms += 1
                _counter = 0
        else:
            _counter = 0
        _last_ms = ms

        rand_b = int.from_bytes(os.urandom(8), "big") & ((1 << 62) - 1)
        value = (
            ((ms & ((1 << 48) - 1)) << 80) | (0x7 << 76) | (_counter << 64) | (0b10 << 62) | rand_b
        )
    return uuid.UUID(int=value)


def uuid7_timestamp_ms(value: uuid.UUID) -> int:
    """Extract the millisecond Unix timestamp from a UUIDv7."""
    if value.version != 7:
        raise ValueError(f"not a UUIDv7: {value}")
    return value.int >> 80
