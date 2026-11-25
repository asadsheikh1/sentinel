import { useQuery } from '@tanstack/react-query'
import { Link, useSearchParams } from 'react-router'

import { api, unwrap, type IncidentStatus, type Severity } from '@/api/client'
import { SeverityBadge, StatusBadge } from '@/components/incidents/badges'
import { SEVERITIES, STATUS_LABEL } from '@/components/incidents/labels'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { Input, Select } from '@/components/ui/field'
import { ago, when } from '@/lib/utils'

const ACTIVE: IncidentStatus[] = ['open', 'investigating', 'mitigated']
const PAGE = 25
export const REFRESH_MS = 10_000

function statusFilter(value: string | null): IncidentStatus[] | undefined {
  if (value === 'all') return undefined
  if (!value || value === 'active') return ACTIVE
  return [value as IncidentStatus]
}

export function IncidentsPage() {
  const [params, setParams] = useSearchParams()
  const status = params.get('status') ?? 'active'
  const severity = params.get('severity') ?? ''
  const service = params.get('service') ?? ''
  const cursor = params.get('cursor')

  const incidents = useQuery({
    queryKey: ['incidents', status, severity, service, cursor],
    queryFn: async () =>
      unwrap(
        await api.GET('/api/v1/incidents', {
          params: {
            query: {
              status: statusFilter(status),
              severity: severity ? [severity as Severity] : undefined,
              service: service || undefined,
              cursor: cursor ? Number(cursor) : undefined,
              limit: PAGE,
            },
          },
        }),
      ),
    refetchInterval: REFRESH_MS, // new incidents appear without a reload
  })

  function set(key: string, value: string) {
    const next = new URLSearchParams(params)
    if (value) next.set(key, value)
    else next.delete(key)
    next.delete('cursor')
    setParams(next)
  }

  const rows = incidents.data ?? []
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <h1 className="text-xl font-semibold">Incidents</h1>
        <div className="flex flex-wrap gap-2">
          <Select
            aria-label="Status"
            className="w-40"
            value={status}
            onChange={(e) => set('status', e.target.value === 'active' ? '' : e.target.value)}
          >
            <option value="active">Active</option>
            <option value="all">All</option>
            {(Object.keys(STATUS_LABEL) as IncidentStatus[]).map((s) => (
              <option key={s} value={s}>
                {STATUS_LABEL[s]}
              </option>
            ))}
          </Select>
          <Select
            aria-label="Severity"
            className="w-36"
            value={severity}
            onChange={(e) => set('severity', e.target.value)}
          >
            <option value="">Any severity</option>
            {SEVERITIES.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </Select>
          <Input
            aria-label="Service"
            className="w-44"
            placeholder="Service"
            defaultValue={service}
            onKeyDown={(e) => {
              if (e.key === 'Enter') set('service', e.currentTarget.value.trim())
            }}
          />
        </div>
      </div>

      <Card>
        {incidents.isPending ? (
          <p className="p-4 text-sm text-muted" role="status">
            Loading incidents…
          </p>
        ) : incidents.isError ? (
          <p className="p-4 text-sm text-sev1" role="alert">
            Couldn't load incidents: {incidents.error.message}
          </p>
        ) : rows.length === 0 ? (
          <p className="p-4 text-sm text-muted">No incidents match.</p>
        ) : (
          <ul className="divide-y divide-line" aria-label="Incidents">
            {rows.map((i) => (
              <li key={i.id}>
                <Link
                  to={`/incidents/${i.id}`}
                  className="flex flex-wrap items-center gap-x-3 gap-y-1 px-4 py-3 hover:bg-subtle"
                >
                  <SeverityBadge severity={i.severity} />
                  <span className="font-mono text-xs text-muted">{i.ref}</span>
                  <span className="min-w-0 flex-1 truncate text-sm font-medium">{i.title}</span>
                  {i.service_name && <span className="text-xs text-muted">{i.service_name}</span>}
                  <StatusBadge status={i.status} />
                  <time
                    className="w-28 text-right text-xs text-muted"
                    dateTime={i.started_at}
                    title={when(i.started_at)}
                  >
                    {ago(i.started_at)}
                  </time>
                </Link>
              </li>
            ))}
          </ul>
        )}
      </Card>

      <div className="flex justify-end gap-2">
        {cursor && (
          <Button variant="outline" size="sm" onClick={() => set('cursor', '')}>
            Newest
          </Button>
        )}
        {rows.length === PAGE && (
          <Button
            variant="outline"
            size="sm"
            onClick={() => {
              const next = new URLSearchParams(params)
              next.set('cursor', String(rows[rows.length - 1]!.number))
              setParams(next)
            }}
          >
            Older
          </Button>
        )}
      </div>
    </div>
  )
}
