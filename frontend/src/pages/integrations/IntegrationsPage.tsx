import { Link } from 'react-router'

import type { Integration, IntegrationStatusValue } from '@/api/client'
import { useAuth } from '@/auth/context'
import { KIND_LABEL } from '@/components/integrations/catalogue'
import { IntegrationStatusBadge } from '@/components/integrations/parts'
import { useIntegrations } from '@/components/integrations/queries'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { cn, when } from '@/lib/utils'

const TILES: { status: IntegrationStatusValue; label: string; tone: string }[] = [
  { status: 'healthy', label: 'Healthy', tone: 'text-good' },
  { status: 'degraded', label: 'Degraded', tone: 'text-sev3' },
  { status: 'stale', label: 'Stale', tone: 'text-sev1' },
  { status: 'pending', label: 'Waiting for data', tone: 'text-muted' },
]

const SHOWN = 5

/** SENTINEL's own internal pieces are not something a user set up. */
const visible = (i: Integration) => i.kind !== 'prober' && i.status !== 'disabled'

export function IntegrationsPage() {
  const { can } = useAuth()
  const integrations = useIntegrations()
  const rows = (integrations.data ?? []).filter(visible)
  const count = (s: IntegrationStatusValue) => rows.filter((i) => i.status === s).length
  const attention = rows.filter((i) => i.status === 'stale' || i.status === 'degraded')

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between gap-3">
        <h1 className="text-xl font-semibold">Integrations</h1>
        {can('integrations:manage') && (
          <Button asChild>
            <Link to="/integrations/new">Add integration</Link>
          </Button>
        )}
      </div>

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4" aria-label="Health">
        {TILES.map((t) => (
          <Card key={t.status} className="px-4 py-3">
            <p className="text-xs text-muted">{t.label}</p>
            <p className={cn('text-2xl font-semibold', t.tone)} data-testid={`tile-${t.status}`}>
              {integrations.isPending ? '–' : count(t.status)}
            </p>
          </Card>
        ))}
      </div>

      {attention.length > 0 && (
        <Card className="border-sev1/40" role="region" aria-label="Needs attention">
          <ul className="divide-y divide-line">
            {attention.slice(0, SHOWN).map((i) => (
              <li key={i.id} className="px-4 py-3 text-sm">
                <Link to={`/integrations/${i.id}`} className="font-medium text-accent">
                  {i.name}
                </Link>{' '}
                is <IntegrationStatusBadge status={i.status} />{' '}
                <span className="text-muted">{i.status_reason}</span>
              </li>
            ))}
          </ul>
          {attention.length > SHOWN && (
            <p className="border-t border-line px-4 py-2 text-sm text-muted">
              and {attention.length - SHOWN} more need attention (below).
            </p>
          )}
        </Card>
      )}

      <Card>
        {integrations.isPending ? (
          <p className="p-4 text-sm text-muted" role="status">
            Loading…
          </p>
        ) : integrations.isError ? (
          <p className="p-4 text-sm text-sev1" role="alert">
            {integrations.error.message}
          </p>
        ) : rows.length === 0 ? (
          <p className="p-4 text-sm text-muted">
            Nothing is connected yet.{' '}
            {can('integrations:manage') && (
              <Link to="/integrations/new" className="text-accent">
                Add the first integration
              </Link>
            )}
          </p>
        ) : (
          <ul className="divide-y divide-line" aria-label="Integrations">
            {rows.map((i) => (
              <li key={i.id}>
                <Link
                  to={`/integrations/${i.id}`}
                  className="flex flex-wrap items-center gap-x-3 gap-y-1 px-4 py-3 hover:bg-subtle"
                >
                  <span className="min-w-0 flex-1 truncate text-sm font-medium">{i.name}</span>
                  <span className="text-xs text-muted">{KIND_LABEL[i.kind]}</span>
                  <span className="text-xs text-muted">{i.environment}</span>
                  <IntegrationStatusBadge status={i.status} />
                  <time className="w-36 text-right text-xs text-muted" dateTime={i.created_at}>
                    added {when(i.created_at)}
                  </time>
                </Link>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  )
}
