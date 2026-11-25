import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { useSearchParams } from 'react-router'

import { api, unwrap, type Entity, type EntityType } from '@/api/client'
import { useIntegrations } from '@/components/integrations/queries'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { Input, Select } from '@/components/ui/field'
import { ago, cn, when } from '@/lib/utils'

const TYPES: EntityType[] = ['host', 'container', 'service', 'endpoint', 'database', 'job']
const STALE_MS = 10 * 60_000

const RELATION: Record<string, string> = {
  runs_on: 'runs on',
  uses: 'calls',
  fronts: 'fronts',
  built_from: 'built from',
  member_of: 'member of',
}

export function InventoryPage() {
  const [params, setParams] = useSearchParams()
  const type = (params.get('type') ?? '') as EntityType | ''
  const q = params.get('q') ?? ''
  const integrations = useIntegrations(30_000)
  const names = new Map((integrations.data ?? []).map((i) => [i.id, i.name]))

  const entities = useQuery({
    queryKey: ['entities', type, q],
    queryFn: async () =>
      unwrap(
        await api.GET('/api/v1/entities', {
          params: { query: { type: type || undefined, q: q || undefined, limit: 500 } },
        }),
      ),
    refetchInterval: 15_000,
  })
  const byId = new Map((entities.data ?? []).map((e) => [e.id, e]))

  function set(key: string, value: string) {
    const next = new URLSearchParams(params)
    if (value) next.set(key, value)
    else next.delete(key)
    setParams(next)
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <h1 className="text-xl font-semibold">Inventory</h1>
        <div className="flex flex-wrap gap-2">
          <Select
            aria-label="Type"
            className="w-36"
            value={type}
            onChange={(e) => set('type', e.target.value)}
          >
            <option value="">All types</option>
            {TYPES.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </Select>
          <Input
            aria-label="Search"
            className="w-48"
            placeholder="Search names"
            defaultValue={q}
            onKeyDown={(e) => {
              if (e.key === 'Enter') set('q', e.currentTarget.value.trim())
            }}
          />
        </div>
      </div>
      <p className="text-sm text-muted">
        Everything SENTINEL has discovered from the data it receives, and how it’s connected.
      </p>
      <Card>
        {entities.isPending ? (
          <p className="p-4 text-sm text-muted" role="status">
            Loading…
          </p>
        ) : entities.isError ? (
          <p className="p-4 text-sm text-sev1" role="alert">
            {entities.error.message}
          </p>
        ) : entities.data.length === 0 ? (
          <p className="p-4 text-sm text-muted">
            Nothing discovered yet. Add an integration, and what it reports appears here.
          </p>
        ) : (
          <ul className="divide-y divide-line" aria-label="Entities">
            {entities.data.map((e) => (
              <EntityRow
                key={e.id}
                entity={e}
                names={names}
                byId={byId}
                fetchedAt={entities.dataUpdatedAt}
              />
            ))}
          </ul>
        )}
      </Card>
    </div>
  )
}

function EntityRow({
  entity: e,
  names,
  byId,
  fetchedAt,
}: {
  entity: Entity
  names: Map<string, string>
  byId: Map<string, Entity>
  fetchedAt: number
}) {
  const [open, setOpen] = useState(false)
  // Relative to when the list was fetched (stable across re-renders).
  const stale = fetchedAt - new Date(e.last_seen).getTime() > STALE_MS
  const relations = useQuery({
    queryKey: ['entity', e.id, 'relations'],
    enabled: open,
    queryFn: async () =>
      unwrap(
        await api.GET('/api/v1/entities/{entity_id}/relations', {
          params: { path: { entity_id: e.id } },
        }),
      ),
  })
  const label = (id: string) => byId.get(id)?.display_name ?? 'another entity'

  return (
    <li className="px-4 py-3">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <Badge className="border-line text-muted">{e.type}</Badge>
        <span className="min-w-0 flex-1 text-sm font-medium break-all">{e.display_name}</span>
        <span className="text-xs text-muted">
          {e.integration_ids.map((i) => names.get(i) ?? 'removed integration').join(', ') || '—'}
        </span>
        <time
          className={cn('w-32 text-right text-xs', stale ? 'text-sev3' : 'text-muted')}
          dateTime={e.last_seen}
          title={when(e.last_seen)}
        >
          {stale ? `not seen since ${ago(e.last_seen)}` : `seen ${ago(e.last_seen)}`}
        </time>
        <Button variant="ghost" size="sm" aria-expanded={open} onClick={() => setOpen(!open)}>
          {open ? 'Hide' : 'Details'}
        </Button>
      </div>
      {open && (
        <div className="mt-2 space-y-1 pl-2 text-xs text-muted">
          <p className="font-mono break-all">{e.key}</p>
          {relations.data?.length ? (
            <ul aria-label={`Relations of ${e.display_name}`}>
              {relations.data.map((r) => (
                <li key={`${r.from_entity_id}-${r.to_entity_id}-${r.relation}`}>
                  {r.from_entity_id === e.id
                    ? `${RELATION[r.relation] ?? r.relation} ${label(r.to_entity_id)}`
                    : `${label(r.from_entity_id)} ${RELATION[r.relation] ?? r.relation} this`}
                  {r.source === 'traces' && ' (from traces)'}
                </li>
              ))}
            </ul>
          ) : (
            <p>{relations.isPending ? 'Loading relations…' : 'No relations.'}</p>
          )}
        </div>
      )}
    </li>
  )
}
