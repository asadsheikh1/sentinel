"""Entity discovery: turn telemetry resource attributes into an inventory and topology.

The gateway's count connector emits small summary metrics carrying each source's
resource attributes, so logs-only and traces-only sources are discovered too.
"""

import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from sentinel.db.models import Entity, EntityRelation, EntityType

LABELS = (
    "service_name",
    "service_namespace",
    "service_version",
    "host_name",
    "container_name",
    "deployment_environment_name",
    "sentinel_integration_id",
)
SUMMARY_QUERY = (
    f"group by ({', '.join(LABELS)}) "
    '({__name__=~"sentinel_telemetry_(logs|spans|datapoints)(_total)?",'
    'sentinel_integration_id!=""})'
)
SERIES_QUERY = 'count by (sentinel_integration_id) ({sentinel_integration_id!=""})'

Query = Callable[[str], Awaitable[list[dict[str, Any]]]]


@dataclass
class DiscoveredEntity:
    type: EntityType
    key: str
    display_name: str
    attributes: dict[str, str] = field(default_factory=dict)
    integration_ids: set[uuid.UUID] = field(default_factory=set)


@dataclass(frozen=True)
class DiscoveredRelation:
    source: tuple[EntityType, str]
    target: tuple[EntityType, str]
    relation: str = "runs_on"


def parse_resources(
    resources: Iterable[Mapping[str, str]],
) -> tuple[list[DiscoveredEntity], set[DiscoveredRelation]]:
    """Pure mapping from resource label sets to entities and relations."""
    entities: dict[tuple[EntityType, str], DiscoveredEntity] = {}
    relations: set[DiscoveredRelation] = set()

    def add(
        kind: EntityType, key: str, name: str, attrs: dict[str, str], owner: uuid.UUID | None
    ) -> tuple[EntityType, str]:
        entity = entities.setdefault((kind, key), DiscoveredEntity(kind, key, name))
        entity.attributes.update({k: v for k, v in attrs.items() if v})
        if owner:
            entity.integration_ids.add(owner)
        return (kind, key)

    for labels in resources:
        try:
            owner = uuid.UUID(labels.get("sentinel_integration_id", ""))
        except ValueError:
            owner = None
        env = labels.get("deployment_environment_name") or "default"
        service_ref = container_ref = host_ref = None

        if host := labels.get("host_name"):
            host_ref = add(EntityType.HOST, host, host, {"host.name": host}, owner)
        if container := labels.get("container_name"):
            container_ref = add(
                EntityType.CONTAINER,
                f"{host or 'unknown'}/{container}",
                container,
                {"container.name": container},
                owner,
            )
        service = labels.get("service_name")
        if service and not service.startswith("unknown_service"):
            namespace = labels.get("service_namespace", "")
            qualified = f"{namespace}/{service}" if namespace else service
            service_ref = add(
                EntityType.SERVICE,
                f"{qualified}@{env}",
                service,
                {
                    "service.name": service,
                    "service.namespace": namespace,
                    "service.version": labels.get("service_version", ""),
                    "deployment.environment.name": env,
                },
                owner,
            )

        if service_ref and container_ref:
            relations.add(DiscoveredRelation(service_ref, container_ref))
        elif service_ref and host_ref:
            relations.add(DiscoveredRelation(service_ref, host_ref))
        if container_ref and host_ref:
            relations.add(DiscoveredRelation(container_ref, host_ref))
    return list(entities.values()), relations


async def store(
    db: AsyncSession,
    entities: list[DiscoveredEntity],
    relations: set[DiscoveredRelation],
    *,
    now: datetime | None = None,
) -> None:
    """Upsert the discovered inventory. Attributes merge; first_seen is preserved."""
    now = now or datetime.now(UTC)
    ids: dict[tuple[EntityType, str], uuid.UUID] = {}
    for e in sorted(entities, key=lambda x: (x.type.value, x.key)):
        existing = await db.scalar(
            select(Entity).where(Entity.type == e.type, Entity.key == e.key).with_for_update()
        )
        if existing is None:
            existing = Entity(
                type=e.type,
                key=e.key,
                display_name=e.display_name,
                attributes=e.attributes,
                integration_ids=sorted(e.integration_ids),
                first_seen=now,
                last_seen=now,
            )
            db.add(existing)
            await db.flush()
        else:
            existing.attributes = {**existing.attributes, **e.attributes}
            existing.integration_ids = sorted(set(existing.integration_ids) | e.integration_ids)
            existing.last_seen = now
        ids[(e.type, e.key)] = existing.id

    for r in sorted(relations, key=lambda x: (x.source[1], x.target[1], x.relation)):
        stmt = insert(EntityRelation).values(
            from_entity_id=ids[r.source],
            to_entity_id=ids[r.target],
            relation=r.relation,
            source="inferred",
            confidence=1.0,
            last_seen=now,
        )
        await db.execute(
            stmt.on_conflict_do_update(
                index_elements=["from_entity_id", "to_entity_id", "relation"],
                set_={"last_seen": now},
            )
        )


DependencyFetch = Callable[[], Awaitable[list[tuple[str, str, str | None]]]]

# Edges the gateway's servicegraph connector counted in the last hour.
ENV_LABEL = "client_deployment_environment_name"
SERVICE_GRAPH_QUERY = (
    f"group by (client, server, {ENV_LABEL}) "
    "(max_over_time(traces_service_graph_request_total[1h]))"
)


def service_graph(query: Query) -> DependencyFetch:
    """Service call edges (caller, callee, environment) from the gateway's service graph."""

    async def fetch() -> list[tuple[str, str, str | None]]:
        edges = []
        for row in await query(SERVICE_GRAPH_QUERY):
            labels = row.get("metric", {})
            client, server = labels.get("client"), labels.get("server")
            if client and server and client != server:
                edges.append((str(client), str(server), labels.get(ENV_LABEL) or None))
        return edges

    return fetch


async def store_dependencies(
    db: AsyncSession,
    edges: Iterable[tuple[str, str, str | None]],
    *,
    now: datetime | None = None,
) -> int:
    """``caller uses callee`` between service entities of the same environment (I12).

    Traces name services by ``service.name``. An edge links the caller and callee
    entities of its environment (or, without one, every pair that shares an
    environment). Returns the relations written.
    """
    now = now or datetime.now(UTC)
    rows = await db.execute(
        select(Entity.id, Entity.attributes).where(Entity.type == EntityType.SERVICE)
    )
    by_name: dict[str, list[tuple[uuid.UUID, str]]] = {}
    for eid, attrs in rows:
        name = attrs.get("service.name")
        if name:
            by_name.setdefault(name, []).append((eid, attrs.get("deployment.environment.name", "")))
    written = 0
    for caller, callee, edge_env in sorted(set(edges), key=lambda e: (e[0], e[1], e[2] or "")):
        for src, env in by_name.get(caller, []):
            for dst, dst_env in by_name.get(callee, []):
                if src == dst or env != dst_env or (edge_env and env != edge_env):
                    continue
                stmt = insert(EntityRelation).values(
                    from_entity_id=src,
                    to_entity_id=dst,
                    relation="uses",
                    source="traces",
                    confidence=1.0,
                    last_seen=now,
                )
                await db.execute(
                    stmt.on_conflict_do_update(
                        index_elements=["from_entity_id", "to_entity_id", "relation"],
                        set_={"last_seen": now},
                    )
                )
                written += 1
    return written


def prometheus_query(base_url: str, timeout_s: float = 10.0) -> Query:
    async def query(promql: str) -> list[dict[str, Any]]:
        async with httpx.AsyncClient(base_url=base_url, timeout=timeout_s) as client:
            response = await client.get("/api/v1/query", params={"query": promql})
            response.raise_for_status()
            result: list[dict[str, Any]] = response.json()["data"]["result"]
            return result

    return query


async def discover(db: AsyncSession, query: Query) -> tuple[int, dict[str, int]]:
    """Run one discovery pass. Returns (entities seen, series count per integration)."""
    rows = await query(SUMMARY_QUERY)
    entities, relations = parse_resources(row["metric"] for row in rows)
    await store(db, entities, relations)
    series = {
        row["metric"]["sentinel_integration_id"]: int(float(row["value"][1]))
        for row in await query(SERIES_QUERY)
    }
    return len(entities), series


async def resolve_entity(db: AsyncSession, labels: Mapping[str, str]) -> uuid.UUID | None:
    """The inventory entity an alert or event is about, from its labels.

    Most specific first: a health check's endpoint, a container, a service, a host.
    Services are matched by name (and namespace) across environments, newest first,
    because alert labels don't always carry the environment.
    """
    from sentinel.db.models import HealthCheck  # avoid an import cycle at module load

    if check_id := labels.get("sentinel_check_id"):
        try:
            check = await db.get(HealthCheck, uuid.UUID(check_id))
        except ValueError:
            check = None
        if check is not None and check.entity_id is not None:
            return check.entity_id
    host = labels.get("host_name")
    kind = labels.get("entity_type")  # every rule-pack alert says what it's about
    if kind == "host" and host:
        # Not the agent's own service: host metrics carry the agent's `job` label,
        # which the service fallback below would otherwise take (found in Step 12).
        found_host: uuid.UUID | None = await db.scalar(
            select(Entity.id).where(Entity.type == EntityType.HOST, Entity.key == host)
        )
        if found_host:
            return found_host
    if container := labels.get("container_name"):
        found = await db.scalar(
            select(Entity.id).where(
                Entity.type == EntityType.CONTAINER,
                Entity.key == f"{host or 'unknown'}/{container}",
            )
        )
        if found:
            return found
    service = labels.get("service_name")
    if not service and kind in (None, "service") and (job := labels.get("job")):
        service = job.rsplit("/", 1)[-1]
    if service:
        namespace = labels.get("service_namespace") or (
            labels["job"].rsplit("/", 1)[0] if "/" in labels.get("job", "") else ""
        )
        # By attribute, not by key: a deployment says "orders", the inventory knows
        # "aviro/orders@dev". The namespace and environment narrow it when given.
        stmt = select(Entity.id).where(
            Entity.type == EntityType.SERVICE, Entity.attributes["service.name"].astext == service
        )
        if namespace:
            stmt = stmt.where(Entity.attributes["service.namespace"].astext == namespace)
        if env := labels.get("deployment_environment_name"):
            stmt = stmt.where(Entity.attributes["deployment.environment.name"].astext == env)
        found = await db.scalar(stmt.order_by(Entity.last_seen.desc()).limit(1))
        if found:
            return found
    if host:
        return await db.scalar(
            select(Entity.id).where(Entity.type == EntityType.HOST, Entity.key == host)
        )
    return None
