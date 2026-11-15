# ADR-0016: The agent is data-out only (no remote execution)

- **Status:** Proposed (pending plan approval)
- **Date:** 2026-11-14

## Context
Remote actions on monitored hosts (restart a process, run a command) would be useful, but they turn every agent into a remote-execution backdoor if SENTINEL or a token is compromised.

## Decision
In v1 the agent only sends data. It has no inbound ports, no command channel, and no remote config push (OpAMP is Could, later). Actions in v1 are limited to GitHub and SENTINEL-managed demo containers via the runner. Cloud-API actions with a separate approved write role are a later Could.

## Consequences
A much smaller blast radius and an easy security story for users. SENTINEL recommends actions on external hosts but cannot perform them.

## Alternatives considered
Bidirectional agent with an allowlisted command set (deferred; would need signed commands, local policy, and audit on the host).
