import time
import uuid

import pytest

from sentinel.db.ids import uuid7, uuid7_timestamp_ms


def test_version_and_variant() -> None:
    value = uuid7()
    assert value.version == 7
    assert value.variant == uuid.RFC_4122


def test_timestamp_is_now() -> None:
    before = time.time_ns() // 1_000_000
    value = uuid7()
    after = time.time_ns() // 1_000_000
    assert before <= uuid7_timestamp_ms(value) <= after + 1


def test_strictly_increasing_within_a_burst() -> None:
    values = [uuid7() for _ in range(10_000)]
    assert values == sorted(values)
    assert len(set(values)) == len(values)


def test_explicit_timestamp_is_encoded() -> None:
    far_future = 4_102_444_800_000  # 2100-01-01
    assert uuid7_timestamp_ms(uuid7(far_future)) == far_future


def test_counter_overflow_borrows_next_millisecond() -> None:
    ts = 4_102_444_900_000
    values = [uuid7(ts) for _ in range(0x1000 + 5)]
    assert values == sorted(values)
    assert uuid7_timestamp_ms(values[-1]) == ts + 1


def test_rejects_non_v7() -> None:
    with pytest.raises(ValueError, match="not a UUIDv7"):
        uuid7_timestamp_ms(uuid.uuid4())
