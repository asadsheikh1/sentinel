# ADR-0013: Use the OpenTelemetry Collector as the SENTINEL Agent

- **Status:** Proposed (pending plan approval)
- **Date:** 2026-11-14

## Context
SENTINEL must collect host, process, log, container, and database data from machines it does not run. A reliable agent needs log tailing with rotation, multiline handling, buffering, retries, backpressure, and many receivers.

## Decision
Ship the 'SENTINEL Agent' as a pinned OpenTelemetry Collector (contrib) release plus SENTINEL config templates, an installer, and a systemd unit. Later, build a slim distribution with the OpenTelemetry Collector Builder. Do not write a custom agent.

## Consequences
Weeks saved and a mature, signed, widely deployed component on users' machines. Users with existing OTel setups need no SENTINEL software. Component stability levels must be tracked. SENTINEL's own engineering goes into normalisation, topology, health, and RCA.

## Alternatives considered
Custom Go or Python agent (months of work, unaudited); Grafana Alloy (Apache-2.0, viable, but less vendor-neutral); Fluent Bit + node_exporter (two agents, two config languages).
