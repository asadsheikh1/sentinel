"""Step 11, pure parts: traceback parsing, normalising, fingerprints, classifying, paging."""

import asyncio
import json
import string
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from sentinel.config import Settings
from sentinel.detection.error_groups import (
    batch,
    classify_line,
    is_spiking,
    scope_of,
    spike_ratio,
)
from sentinel.detection.fingerprint import (
    app_packages,
    fingerprint,
    frame_key,
    normalise_message,
    normalise_path,
    top_in_app_frame,
)
from sentinel.detection.poller import QUERIES, read_window
from sentinel.detection.traceback import Frame, parse
from sentinel.telemetry.loki import LogLine, LokiClient
from tests.fake_loki import FakeLoki

CORPUS = Path(__file__).resolve().parents[1] / "fixtures" / "tracebacks"
T0 = datetime(2026, 11, 23, 10, 0, tzinfo=UTC)


def corpus(name: str) -> str:
    return (CORPUS / f"{name}.txt").read_text()


# ------------------------------------------------------------------ traceback corpus

# name: (type, message starts with, innermost file ends with, function, chained, frames)
EXPECTED = {
    "simple_keyerror": ("KeyError", "'gift_message'", "checkout.py", "simple", 0, 2),
    "chained_during_handling": ("KeyError", "'missing'", "checkout.py", "chained", 1, 2),
    "chained_direct_cause": ("RuntimeError", "payment provider failed", "checkout.py", "caused", 1, 2),  # noqa: E501
    "recursion_repeated": ("RecursionError", "maximum recursion depth", "checkout.py", "f", 0, 5),
    "multiline_message": ("ValueError", "first line\nsecond line\nthird line", "checkout.py", "multiline", 0, 2),  # noqa: E501
    "no_message": ("NotImplementedError", "", "checkout.py", "no_message", 0, 2),
    "qualified_type": ("OrderError", "order 9f1c2e3a", "checkout.py", "qualified", 0, 2),
    "syntax_error": ("SyntaxError", "invalid syntax", "plugins/broken.py", "", 0, 3),
    "exception_group": ("ExceptionGroup", "several things failed (2 sub-exceptions)", "checkout.py", "group", 0, 2),  # noqa: E501
    "with_note": ("KeyError", "'sku'\nwhile pricing basket 1234", "checkout.py", "noted", 0, 2),
    "async_handler": ("ConnectionError", "db 10.0.0.5:5432 refused", "checkout.py", "handler", 0, 6),  # noqa: E501
    "import_error": ("ModuleNotFoundError", "No module named", "checkout.py", "frozen_import", 0, 2),  # noqa: E501
    "aviro_s01_keyerror": ("KeyError", "'gift_message'", "aviro/orders/app.py", "create_order", 0, 20),  # noqa: E501
}  # fmt: skip


def test_the_corpus_is_complete() -> None:
    assert {p.stem for p in CORPUS.glob("*.txt")} == set(EXPECTED)


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_corpus_tracebacks_parse(name: str) -> None:
    etype, message, file_end, func, chained, n_frames = EXPECTED[name]
    parsed = parse(corpus(name))
    assert parsed is not None
    assert parsed.exc_type == etype
    assert parsed.message.startswith(message)
    assert parsed.chained == chained
    assert len(parsed.frames) == n_frames
    assert parsed.frames[-1].file.endswith(file_end)
    assert parsed.frames[-1].function == func


def test_frames_carry_their_source_line() -> None:
    parsed = parse(corpus("aviro_s01_keyerror"))
    assert parsed is not None
    assert parsed.frames[-1].line == 63
    assert parsed.frames[-1].source == 'gift_message = raw["gift_message"].strip()[:200]'


def test_a_traceback_inside_a_log_line_is_found() -> None:
    text = "2026-11-23 10:00:00 ERROR checkout failed\n" + corpus("simple_keyerror")
    parsed = parse(text)
    assert parsed is not None
    assert parsed.exc_type == "KeyError"


def test_the_raised_exception_of_a_group_is_the_group() -> None:
    parsed = parse(corpus("exception_group"))
    assert parsed is not None
    # The sub-exceptions (ValueError, TypeError) are drawn further right: not ours.
    assert parsed.exc_type == "ExceptionGroup"


@pytest.mark.parametrize("text", ["", "no traceback here", "Traceback (most recent call last)"])
def test_text_without_a_full_traceback(text: str) -> None:
    parsed = parse(text)
    assert parsed is None or parsed.exc_type is None


@given(st.text(max_size=3000))
@settings(max_examples=300, deadline=None)
def test_parse_never_raises(text: str) -> None:
    parse(text)
    parse("Traceback (most recent call last):\n" + text)


_ident = st.from_regex(r"[A-Za-z_][A-Za-z0-9_]{0,15}", fullmatch=True)
_path = st.lists(
    st.text(string.ascii_lowercase + "_", min_size=1, max_size=8), min_size=1, max_size=4
).map(lambda parts: "/srv/app/" + "/".join(parts) + ".py")


@given(
    frames=st.lists(st.tuples(_path, st.integers(1, 99999), _ident), min_size=1, max_size=12),
    etype=st.from_regex(r"[A-Z][A-Za-z]{0,15}(Error|Exception)", fullmatch=True),
    message=st.text(string.ascii_letters + string.digits + " .,:'-_", max_size=80),
)
@settings(max_examples=200, deadline=None)
def test_parse_round_trips_a_formatted_traceback(
    frames: list[tuple[str, int, str]], etype: str, message: str
) -> None:
    body = "".join(f'  File "{f}", line {n}, in {fn}\n    do_{fn}()\n' for f, n, fn in frames)
    exc = f"{etype}: {message}" if message.strip() else etype
    parsed = parse(f"Traceback (most recent call last):\n{body}{exc}\n")
    assert parsed is not None
    assert [(f.file, f.line, f.function) for f in parsed.frames] == frames
    assert parsed.exc_type == etype
    assert parsed.message == message.strip()


# ------------------------------------------------------------------ normalising


@pytest.mark.parametrize(
    ("message", "normalised"),
    [
        ("'gift_message'", "'gift_message'"),
        ("order 9f1c2e3a-0000-4000-8000-000000000001 not found for user 42",
         "order <uuid> not found for user <n>"),
        ("db 10.0.0.5:5432 refused", "db <ip> refused"),
        ("timeout after 30.5s at 2026-11-23T10:00:00Z", "timeout after <n>s at <ts>"),
        ("no customer bob@example.com", "no customer <email>"),
        ("object at 0x7f3a2b", "object at <hex>"),
        ("GET https://api.example.com/v1/orders/77 failed", "GET <url> failed"),
        ("KeyError 'item_3'", "KeyError <str>"),
        ('bad value "two words"', "bad value <str>"),
        ("  lots   of\n space ", "lots of space"),
    ],
)  # fmt: skip
def test_normalise_examples(message: str, normalised: str) -> None:
    assert normalise_message(message) == normalised


@given(st.text(max_size=600))
@settings(max_examples=400, deadline=None)
def test_normalise_is_idempotent(text: str) -> None:
    once = normalise_message(text)
    assert normalise_message(once) == once


# One kind of ID per slot, as one line of code produces (a UUID and a number are
# deliberately different placeholders: different code paths, different errors).
_ID_KINDS = [
    st.uuids().map(str),
    st.integers(0, 10**12).map(str),
    # Addresses with a quote in them are left out: in a log message that reads as quoting.
    st.emails().filter(lambda e: "'" not in e and '"' not in e),
    st.tuples(*[st.integers(0, 255)] * 4).map(lambda t: ".".join(map(str, t))),
    st.binary(min_size=6, max_size=16).map(lambda b: "a1" + b.hex()),
    st.datetimes().map(lambda d: d.isoformat(timespec="seconds")),
]


@given(st.data(), st.lists(st.sampled_from(range(len(_ID_KINDS))), min_size=3, max_size=3))
@settings(max_examples=300, deadline=None)
def test_the_same_error_with_different_ids_has_one_fingerprint(data: Any, kinds: list[int]) -> None:
    template = "payment {} failed for order {} (user {})"
    a = [data.draw(_ID_KINDS[k]) for k in kinds]
    b = [data.draw(_ID_KINDS[k]) for k in kinds]
    key_a = fingerprint("PaymentError", template.format(*a), "shop/pay.py:charge")
    key_b = fingerprint("PaymentError", template.format(*b), "shop/pay.py:charge")
    assert key_a.fingerprint == key_b.fingerprint, (key_a.message, key_b.message)


def test_different_errors_have_different_fingerprints() -> None:
    base = fingerprint("KeyError", "'gift_message'", "aviro/orders/app.py:create_order")
    assert base.fingerprint != fingerprint("KeyError", "'sku'", base.top_frame).fingerprint
    assert (
        base.fingerprint != fingerprint("ValueError", "'gift_message'", base.top_frame).fingerprint
    )
    assert (
        base.fingerprint
        != fingerprint("KeyError", "'gift_message'", "aviro/orders/app.py:list").fingerprint
    )
    assert base.fingerprint == fingerprint("KeyError", "'gift_message'", base.top_frame).fingerprint


# ------------------------------------------------------------------ frames


def test_paths_are_the_same_on_every_machine() -> None:
    assert (
        normalise_path("/app/.venv/lib/python3.12/site-packages/aviro/orders/app.py")
        == "aviro/orders/app.py"
    )
    assert (
        normalise_path("/usr/local/lib/python3.12/asyncio/runners.py")
        == "<stdlib>/asyncio/runners.py"
    )
    assert normalise_path("/srv/shop/src/shop/checkout.py") == "src/shop/checkout.py"
    assert normalise_path("/app/main.py") == "main.py"


def test_the_top_frame_is_the_apps_even_inside_site_packages() -> None:
    parsed = parse(corpus("aviro_s01_keyerror"))
    assert parsed is not None
    pkgs = app_packages(
        code_file_path="/app/.venv/lib/python3.12/site-packages/aviro/common/service.py",
        logger="aviro.orders",
    )
    assert pkgs == {"aviro"}
    top = top_in_app_frame(parsed.frames, pkgs)
    assert top is not None
    assert frame_key(top) == "aviro/orders/app.py:create_order"


def test_a_library_raising_from_app_code_blames_the_app_frame() -> None:
    frames = [
        Frame("/app/shop/api.py", 10, "checkout"),
        Frame("/app/shop/repo.py", 44, "save_order"),
        Frame("/usr/lib/python3/dist-packages/sqlalchemy/engine/base.py", 1900, "_execute"),
        Frame("/usr/lib/python3.12/asyncio/tasks.py", 300, "step"),
    ]
    top = top_in_app_frame(frames, app_packages(code_file_path=None, logger="shop.api"))
    assert top is not None
    assert frame_key(top) == "shop/repo.py:save_order"


def test_frameworks_that_log_for_the_app_are_never_the_app() -> None:
    pkgs = app_packages(
        code_file_path="/venv/lib/python3.12/site-packages/uvicorn/protocols/http/h11.py",
        logger="uvicorn.error",
    )
    assert pkgs == frozenset()


def test_only_library_frames_fall_back_to_the_innermost() -> None:
    frames = [Frame("<frozen runpy>", 1, "run"), Frame("/x/site-packages/lib/a.py", 2, "f")]
    top = top_in_app_frame(frames, set())
    assert top is not None
    assert top.function == "f"


def test_line_numbers_do_not_change_the_group() -> None:
    a = frame_key(Frame("/app/shop/repo.py", 44, "save_order"))
    b = frame_key(Frame("/app/shop/repo.py", 51, "save_order"))
    assert a == b


# ------------------------------------------------------------------ classifying lines


def _otlp_s01(order: str = "1") -> LogLine:
    return LogLine(
        ts=T0,
        line="unhandled exception",
        labels={
            "service_name": "orders", "service_namespace": "aviro",
            "deployment_environment_name": "dev",
            "sentinel_integration_id": "01a2051b-5137-7000-9881-139b2877e6c1",
            "severity_number": "17", "severity_text": "ERROR",
            "exception_type": "KeyError", "exception_message": "gift_message",
            "exception_stacktrace": corpus("aviro_s01_keyerror"),
            "code_file_path": "/app/.venv/lib/python3.12/site-packages/aviro/common/service.py",
            "code_function_name": "faults_and_errors", "scope_name": "aviro.orders",
            "trace_id": f"{order:0>32}",
        },
    )  # fmt: skip


def test_an_otlp_exception_record_is_an_occurrence() -> None:
    occ = classify_line(_otlp_s01())
    assert occ is not None
    assert occ.scope.key == "service:aviro/orders@dev"
    assert occ.key.exception_type == "KeyError"
    assert occ.key.top_frame == "aviro/orders/app.py:create_order"
    assert occ.level == "error"
    assert occ.trace_id == "0" * 31 + "1"


def test_many_requests_failing_the_same_way_make_one_group() -> None:
    occs = [classify_line(_otlp_s01(str(i))) for i in range(50)]
    groups = batch(o for o in occs if o is not None)
    assert len(groups) == 1
    (only,) = groups.values()
    assert only.count == 50
    assert len(only.trace_ids) == 50


def _agent(line: str, severity: str, **extra: str) -> LogLine:
    labels = {"host_name": "web-1", "container_name": "shop_api_1", "severity_number": severity}
    return LogLine(ts=T0, line=line, labels={**labels, **extra})


def test_stderr_info_lines_are_not_errors() -> None:
    # journald gives everything on stderr priority 3 (ERROR); uvicorn logs INFO there.
    assert classify_line(_agent("INFO:     Started server process [7]", "17")) is None
    assert classify_line(_agent('{"level": "info", "msg": "ok"}', "17")) is None


def test_a_json_logger_on_stdout_can_still_say_error() -> None:
    line = json.dumps(
        {"level": "error", "msg": "card declined for order 991", "logger": "shop.pay"}
    )
    occ = classify_line(_agent(line, "9"))
    assert occ is not None
    assert occ.key.message == "card declined for order <n>"
    assert occ.key.top_frame == "shop.pay"
    assert occ.scope.key == "container:web-1/shop_api_1"


def test_a_json_line_with_a_stack_uses_its_traceback() -> None:
    body = {"level": "error", "msg": "unhandled exception", "exc_type": "KeyError",
            "stack": corpus("simple_keyerror"), "logger": "shop.checkout"}  # fmt: skip
    occ = classify_line(_agent(json.dumps(body), "9"))
    assert occ is not None
    assert occ.key.exception_type == "KeyError"
    assert occ.key.top_frame == "src/shop/checkout.py:simple"


def test_a_bare_traceback_at_info_level_is_an_error() -> None:
    occ = classify_line(_agent(corpus("chained_direct_cause"), "9", systemd_unit="shop.service"))
    assert occ is not None
    assert occ.key.exception_type == "RuntimeError"


def test_sdk_warnings_and_fatal_levels() -> None:
    warn = LogLine(T0, "slow", {"service_name": "a", "severity_number": "13", "scope_name": "a"})
    fatal = LogLine(
        T0, "out of disk", {"service_name": "a", "severity_number": "21", "scope_name": "a"}
    )
    assert classify_line(warn) is None
    occ = classify_line(fatal)
    assert occ is not None
    assert occ.level == "critical"
    assert occ.key.top_frame == "a"  # no traceback, no call site: the logger


@pytest.mark.parametrize(
    ("labels", "key"),
    [
        ({"service_name": "orders", "service_namespace": "aviro", "deployment_environment_name": "dev"},  # noqa: E501
         "service:aviro/orders@dev"),
        ({"service_name": "unknown_service:python", "host_name": "h", "container_name": "c"}, "container:h/c"),  # noqa: E501
        ({"host_name": "h", "systemd_unit": "nginx.service"}, "unit:h/nginx.service"),
        ({"host_name": "h", "syslog_identifier": "cron"}, "unit:h/cron"),
        ({"host_name": "h"}, "host:h"),
        ({"sentinel_integration_id": "x"}, "integration:x"),
    ],
)  # fmt: skip
def test_scopes(labels: dict[str, str], key: str) -> None:
    assert scope_of(labels).key == key


# ------------------------------------------------------------------ spikes


def _minutes(counts: dict[int, int]) -> dict[str, int]:
    end = int(T0.timestamp() // 60)
    return {str(end - back): n for back, n in counts.items()}


def test_spike_ratio_compares_the_last_5_minutes_with_the_hour_before() -> None:
    counts = _minutes({**dict.fromkeys(range(5, 65), 1), 0: 30, 1: 30})
    current, baseline = spike_ratio(counts, T0)
    assert current == 60
    assert baseline == pytest.approx(5.0)


def _group(counts: dict[str, int], age: timedelta) -> Any:
    return SimpleNamespace(minute_counts=counts, first_seen=T0 - age)


def test_spike_needs_volume_factor_and_age() -> None:
    s = Settings(error_spike_factor=5, error_spike_min_count=20)
    steady = _minutes(dict.fromkeys(range(0, 65), 2))
    burst = _minutes({**dict.fromkeys(range(5, 65), 1), 0: 30})
    small = _minutes({0: 10})
    assert is_spiking(_group(burst, timedelta(hours=2)), T0, s)[0] is True
    assert is_spiking(_group(steady, timedelta(hours=2)), T0, s)[0] is False
    assert is_spiking(_group(small, timedelta(hours=2)), T0, s)[0] is False
    assert is_spiking(_group(burst, timedelta(minutes=5)), T0, s)[0] is False  # that's "new"


# ------------------------------------------------------------------ paging


def _read(
    fake: FakeLoki, start: int, end: int, page: int, max_pages: int = 50
) -> tuple[list[LogLine], int]:
    async def go() -> tuple[list[LogLine], int]:
        async with LokiClient("http://loki", transport=fake.transport) as loki:
            return await read_window(loki, QUERIES[0], start, end, page=page, max_pages=max_pages)

    return asyncio.run(go())


def test_paging_counts_lines_that_share_a_timestamp_exactly_once() -> None:
    fake = FakeLoki()
    # 3 lines at each of 10 nanoseconds, across pages of 4: boundaries split timestamps.
    for ns in range(100, 110):
        for i in range(3):
            fake.add(ns, f"err {ns}-{i}", severity_number="17", stream=str(i))
    lines, reached = _read(fake, 0, 1000, page=4)
    assert reached == 1000
    assert sorted(line.line for line in lines) == sorted(e.line for e in fake.entries)


def test_paging_stops_at_the_budget_and_says_where() -> None:
    fake = FakeLoki()
    for ns in range(100, 200):
        fake.add(ns, f"err {ns}", severity_number="17")
    lines, reached = _read(fake, 0, 1000, page=10, max_pages=3)
    assert reached < 1000
    assert all(line.ns < reached for line in lines)
    rest, end = _read(fake, reached, 1000, page=10)
    assert end == 1000
    assert len(lines) + len(rest) == 100


def test_a_page_full_of_one_timestamp_moves_on() -> None:
    fake = FakeLoki()
    for i in range(12):
        fake.add(500, f"burst {i}", severity_number="17", stream=str(i))
    fake.add(600, "after", severity_number="17")
    lines, reached = _read(fake, 0, 1000, page=5)
    assert reached == 1000
    assert "after" in {line.line for line in lines}
