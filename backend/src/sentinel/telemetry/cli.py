"""``sentinel telemetry …``: query the stores by hand through the same safe adapters."""

import argparse
import asyncio
import json
import uuid
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from sentinel.config import Settings
from sentinel.telemetry.base import InvalidQueryError, Limits, Selector, TelemetryError, TimeRange
from sentinel.telemetry.fixtures import FixtureMissingError, FixtureTelemetryAdapter, Mode
from sentinel.telemetry.jaeger import JaegerClient
from sentinel.telemetry.loki import LokiClient
from sentinel.telemetry.prometheus import PrometheusClient, Template


def add_parser(sub: Any) -> None:
    tel = sub.add_parser("telemetry", help="query the telemetry stores (debugging)")
    tel_sub = tel.add_subparsers(dest="telemetry_command", required=True)

    def selector_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--service")
        p.add_argument("--namespace")
        p.add_argument("--environment")
        p.add_argument("--host")
        p.add_argument("--integration", type=uuid.UUID, help="integration id")
        p.add_argument("--minutes", type=float, default=15)
        p.add_argument("--record", type=Path, help="save responses to this fixture file")
        p.add_argument("--replay", type=Path, help="answer from this fixture file instead")
        p.add_argument("--json", action="store_true", help="print raw JSON")

    m = tel_sub.add_parser("metrics", help="a metric template over a time window")
    m.add_argument("template", choices=[t.value for t in Template])
    m.add_argument("--metric", help="metric name (series template only)")
    selector_args(m)

    lg = tel_sub.add_parser("logs", help="recent log lines (redacted)")
    lg.add_argument("--contains")
    lg.add_argument("--errors", action="store_true", help="ERROR and above only")
    lg.add_argument("--limit", type=int, default=20)
    selector_args(lg)

    tr = tel_sub.add_parser("traces", help="recent traces for a service")
    tr.add_argument("--errors", action="store_true")
    tr.add_argument("--limit", type=int, default=5)
    selector_args(tr)

    one = tel_sub.add_parser("trace", help="one trace by id")
    one.add_argument("trace_id")
    one.add_argument("--record", type=Path)
    one.add_argument("--replay", type=Path)
    one.add_argument("--json", action="store_true")


def _default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    return str(value)


async def _run(args: argparse.Namespace, settings: Settings) -> Any:
    fixtures = None
    if args.record and args.replay:
        raise InvalidQueryError("use --record or --replay, not both")
    if args.record:
        fixtures = FixtureTelemetryAdapter(args.record, Mode.RECORD)
    elif args.replay:
        fixtures = FixtureTelemetryAdapter(args.replay, Mode.REPLAY)
    transport = fixtures.transport() if fixtures else None
    limits = Limits(
        timeout_s=settings.telemetry_timeout_s,
        max_response_bytes=settings.telemetry_max_response_bytes,
    )
    try:
        cmd = args.telemetry_command
        if cmd == "trace":
            async with JaegerClient(settings.jaeger_url, limits, transport=transport) as j:
                return await j.trace(args.trace_id)
        window = TimeRange.last(args.minutes)
        if cmd == "traces":
            if not args.service:
                raise InvalidQueryError("traces needs --service")
            async with JaegerClient(settings.jaeger_url, limits, transport=transport) as j:
                return await j.find(args.service, window, errors_only=args.errors, limit=args.limit)
        selector = Selector(
            service=args.service,
            namespace=args.namespace,
            environment=args.environment,
            host=args.host,
            integration_id=args.integration,
        )
        if cmd == "metrics":
            async with PrometheusClient(settings.prometheus_url, limits, transport=transport) as p:
                return await p.range(Template(args.template), selector, window, metric=args.metric)
        async with LokiClient(settings.loki_url, limits, transport=transport) as lk:
            return await lk.logs(
                selector,
                window,
                contains=args.contains,
                min_severity=17 if args.errors else None,
                limit=args.limit,
            )
    finally:
        if fixtures is not None and fixtures.mode is Mode.RECORD:
            fixtures.save()


def _print(cmd: str, result: Any) -> None:
    if result is None:
        print("not found")
        return
    if cmd == "metrics":
        print(f"query: {result.query}  (step {result.step_s}s)")
        for s in result.series:
            last = f"{s.points[-1][1]:.4g}" if s.points else "-"
            print(
                f"  {s.labels.get('service_name', s.labels)}: {len(s.points)} points, latest {last}"
            )
    elif cmd == "logs":
        print(f"query: {result.query}")
        for line in result.lines:
            svc = line.labels.get("service_name", "?")
            print(f"  {line.ts:%H:%M:%S} {svc:<10} {line.line}")
    elif cmd == "traces":
        for t in result.traces:
            root = t.root
            mark = "ERROR" if t.has_error else "ok"
            name = root.name if root else "?"
            print(
                f"  {t.trace_id} {mark:<5} {len(t.spans)} spans  {name}  [{', '.join(t.services)}]"
            )
    else:
        for s in result.spans:
            mark = "ERROR" if s.error else ""
            print(f"  {s.service:<10} {s.name:<40} {s.duration_ms:8.1f} ms {mark}")
    if getattr(result, "truncated", False):
        print("  (truncated by limits)")
    if getattr(result, "redactions", None):
        print(f"  redacted: {result.redactions}")


def run(args: argparse.Namespace, settings: Settings) -> int:
    try:
        result = asyncio.run(_run(args, settings))
    except (InvalidQueryError, TelemetryError, FixtureMissingError) as e:
        print(f"Error: {e}")
        return 1
    if args.json:
        print(json.dumps(result, default=_default, indent=2))
    else:
        _print(args.telemetry_command, result)
    return 0
