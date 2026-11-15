# Architecture Decision Records

Format: MADR-lite (Context, Decision, Consequences, Alternatives). New ADRs are added whenever an approved design changes.

- [ADR-0001: Modular monolith with a separate privileged runner](0001-modular-monolith.md)
- [ADR-0002: Local inference via Ollama behind an internal LLM gateway](0002-local-inference-ollama.md)
- [ADR-0003: PostgreSQL + pgvector as the single datastore](0003-postgres-pgvector.md)
- [ADR-0004: LangGraph only for investigation and repair workflows](0004-langgraph-scope.md)
- [ADR-0005: No MCP inside SENTINEL for v1](0005-no-mcp-in-v1.md)
- [ADR-0006: Postgres-backed job queue instead of Celery/Redis](0006-postgres-job-queue.md)
- [ADR-0007: Prometheus, Loki, and Jaeger behind an OTel Collector](0007-observability-backends.md)
- [ADR-0008: Privileged runner broker for all container operations](0008-privileged-runner.md)
- [ADR-0009: Single Graviton EC2 with Podman Compose for the MVP](0009-single-ec2-compose.md)
- [ADR-0010: OpenTofu instead of Terraform](0010-opentofu.md)
- [ADR-0011: Vite + React SPA instead of Next.js](0011-vite-react-spa.md)
- [ADR-0012: Server-enforced evidence citations for every hypothesis](0012-evidence-cited-hypotheses.md)
- [ADR-0013: Use the OpenTelemetry Collector as the SENTINEL Agent](0013-otel-collector-as-agent.md)
- [ADR-0014: OTLP-first ingestion through one authenticated gateway](0014-otlp-first-ingestion-gateway.md)
- [ADR-0015: In-house SSRF-guarded prober for external health checks](0015-own-ssrf-safe-prober.md)
- [ADR-0016: The agent is data-out only (no remote execution)](0016-data-only-agent.md)
- [ADR-0017: Single organisation per deployment](0017-single-organisation.md)
- [ADR-0018: Podman as the container runtime (no Docker)](0018-podman-runtime.md)
