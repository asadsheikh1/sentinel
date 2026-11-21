"""Typed, safe, size-capped read access to the telemetry stores."""

from sentinel.telemetry.base import (
    InvalidQueryError,
    Limits,
    Selector,
    TelemetryError,
    TelemetryTimeoutError,
    TimeRange,
)
from sentinel.telemetry.jaeger import JaegerClient, Span, Trace, TraceResult
from sentinel.telemetry.loki import LogLine, LogResult, LokiClient
from sentinel.telemetry.prometheus import MetricResult, MetricSeries, PrometheusClient, Template

__all__ = [
    "InvalidQueryError",
    "JaegerClient",
    "Limits",
    "LogLine",
    "LogResult",
    "LokiClient",
    "MetricResult",
    "MetricSeries",
    "PrometheusClient",
    "Selector",
    "Span",
    "TelemetryError",
    "TelemetryTimeoutError",
    "Template",
    "TimeRange",
    "Trace",
    "TraceResult",
]
