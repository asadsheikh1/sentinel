import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router'

import { api, ApiError, unwrap, type Integration } from '@/api/client'
import { useAuth } from '@/auth/context'
import { KIND_LABEL } from '@/components/integrations/catalogue'
import { AgentInstructions, AppInstructions } from '@/components/integrations/Instructions'
import { Checklist, IntegrationStatusBadge, SecretOnce } from '@/components/integrations/parts'
import { useIntegrations, useIntegrationStatus, useMeta } from '@/components/integrations/queries'
import { Button } from '@/components/ui/button'
import { Card, CardBody, CardHeader, CardTitle } from '@/components/ui/card'
import { ago, when } from '@/lib/utils'

const AGENT = new Set(['linux_host', 'container_host'])

export function IntegrationDetailPage() {
  const { id = '' } = useParams()
  const { can } = useAuth()
  const integrations = useIntegrations()
  const status = useIntegrationStatus(id)
  const integration = integrations.data?.find((i) => i.id === id)

  if (integrations.isPending)
    return (
      <p className="text-sm text-muted" role="status">
        Loading…
      </p>
    )
  if (!integration) {
    return (
      <p className="text-sm">
        Integration not found.{' '}
        <Link to="/integrations" className="text-accent">
          Back
        </Link>
      </p>
    )
  }

  return (
    <div className="space-y-4">
      <Link to="/integrations" className="text-sm text-accent">
        ← Integrations
      </Link>
      <header className="space-y-1">
        <div className="flex flex-wrap items-center gap-2">
          <IntegrationStatusBadge status={integration.status} />
          <span className="text-xs text-muted">
            {KIND_LABEL[integration.kind]} · {integration.environment}
          </span>
        </div>
        <h1 className="text-xl font-semibold break-words">{integration.name}</h1>
        {integration.status_reason && (
          <p className="text-sm text-muted">{integration.status_reason}</p>
        )}
      </header>

      <div className="grid gap-4 lg:grid-cols-3">
        <div className="min-w-0 space-y-4 lg:col-span-2">
          <Card>
            <CardHeader>
              <CardTitle>Verification</CardTitle>
            </CardHeader>
            <CardBody>
              {status.data ? (
                <Checklist items={status.data.checklist ?? []} />
              ) : (
                <p className="text-sm text-muted">Checking…</p>
              )}
            </CardBody>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle>Signals</CardTitle>
            </CardHeader>
            <CardBody>
              {status.data && status.data.signals.length > 0 ? (
                <table className="w-full text-left text-sm">
                  <thead className="text-xs text-muted">
                    <tr>
                      <th className="py-1 font-medium">Signal</th>
                      <th className="py-1 font-medium">Last seen</th>
                      <th className="py-1 font-medium">Last error</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-line">
                    {status.data.signals.map((s) => (
                      <tr key={s.signal}>
                        <td className="py-1.5 capitalize">{s.signal}</td>
                        <td className="py-1.5">
                          <time dateTime={s.last_seen_at} title={when(s.last_seen_at)}>
                            {ago(s.last_seen_at)}
                          </time>
                        </td>
                        <td className="py-1.5 break-words text-muted">{s.last_error ?? '—'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              ) : (
                <p className="text-sm text-muted">Nothing received yet.</p>
              )}
            </CardBody>
          </Card>
          <Discovered id={integration.id} />
          {can('integrations:manage') && <Setup integration={integration} />}
        </div>
        <div className="min-w-0 space-y-4">
          {can('integrations:manage') ? (
            <Manage integration={integration} />
          ) : (
            <Card>
              <CardBody>
                <p className="text-sm text-muted">Only an admin can change integrations.</p>
              </CardBody>
            </Card>
          )}
        </div>
      </div>
    </div>
  )
}

function Discovered({ id }: { id: string }) {
  const entities = useQuery({
    queryKey: ['entities', { integration: id }],
    queryFn: async () =>
      unwrap(await api.GET('/api/v1/entities', { params: { query: { integration: id } } })),
    refetchInterval: 10_000,
  })
  return (
    <Card>
      <CardHeader>
        <CardTitle>Discovered</CardTitle>
      </CardHeader>
      <CardBody>
        {entities.data && entities.data.length > 0 ? (
          <ul className="space-y-1 text-sm">
            {entities.data.map((e) => (
              <li key={e.id} className="flex justify-between gap-3">
                <span className="min-w-0 break-all">
                  <span className="font-medium">{e.display_name}</span>{' '}
                  <span className="text-xs text-muted">{e.type}</span>
                </span>
                <span className="text-xs whitespace-nowrap text-muted">
                  seen {ago(e.last_seen)}
                </span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-sm text-muted">Nothing discovered yet.</p>
        )}
        <Link to="/inventory" className="mt-2 inline-block text-sm text-accent">
          Inventory
        </Link>
      </CardBody>
    </Card>
  )
}

function Setup({ integration }: { integration: Integration }) {
  const meta = useMeta()
  const agent = AGENT.has(integration.kind)
  const setup = useQuery({
    queryKey: ['integration', integration.id, 'setup'],
    enabled: agent,
    queryFn: async () =>
      unwrap(
        await api.GET('/api/v1/integrations/{integration_id}/setup', {
          params: { path: { integration_id: integration.id } },
        }),
      ),
  })
  if (!agent && integration.kind !== 'otel_app') return null
  return (
    <Card>
      <CardHeader>
        <CardTitle>Setup instructions</CardTitle>
      </CardHeader>
      <CardBody>
        {agent && setup.data && <AgentInstructions setup={setup.data} />}
        {integration.kind === 'otel_app' && meta.data && (
          <AppInstructions endpoint={meta.data.ingest_endpoint} service={integration.name} />
        )}
        <p className="mt-3 text-xs text-muted">
          The token isn’t shown again. Rotate it to get a new one.
        </p>
      </CardBody>
    </Card>
  )
}

function Manage({ integration }: { integration: Integration }) {
  const client = useQueryClient()
  const navigate = useNavigate()
  const [token, setToken] = useState<string | null>(null)
  const path = { params: { path: { integration_id: integration.id } } }
  const refresh = () => client.invalidateQueries({ queryKey: ['integrations'] })

  const rotate = useMutation({
    mutationFn: async () =>
      unwrap(await api.POST('/api/v1/integrations/{integration_id}/tokens/rotate', path)),
    onSuccess: (data) => {
      setToken(data.ingest_token)
      void refresh()
    },
  })
  const revoke = useMutation({
    mutationFn: async () => {
      const r = await api.POST('/api/v1/integrations/{integration_id}/tokens/revoke', path)
      if (!r.response.ok) throw new ApiError(r.response.status, 'Revoking failed.')
    },
    onSuccess: refresh,
  })
  const disable = useMutation({
    mutationFn: async () => {
      const r = await api.DELETE('/api/v1/integrations/{integration_id}', path)
      if (!r.response.ok) throw new ApiError(r.response.status, 'Disabling failed.')
    },
    onSuccess: async () => {
      await refresh()
      navigate('/integrations')
    },
  })
  const error = rotate.error ?? revoke.error ?? disable.error
  const telemetry = integration.kind !== 'http_check'

  return (
    <Card>
      <CardHeader>
        <CardTitle>Manage</CardTitle>
      </CardHeader>
      <CardBody className="space-y-4">
        {telemetry && (
          <div className="space-y-2">
            <p className="text-sm">
              Rotate the token if it may have leaked. The old one stops working at once.
            </p>
            <Button
              variant="outline"
              size="sm"
              disabled={rotate.isPending}
              onClick={() => rotate.mutate()}
            >
              Rotate token
            </Button>
            {token && <SecretOnce secret={token} what="new ingest token" />}
          </div>
        )}
        {telemetry && (
          <div className="space-y-2">
            <p className="text-sm">Revoke stops all data from this integration until you rotate.</p>
            <Button
              variant="outline"
              size="sm"
              disabled={revoke.isPending}
              onClick={() => {
                if (window.confirm('Revoke the token? Data stops at once.')) revoke.mutate()
              }}
            >
              Revoke token
            </Button>
          </div>
        )}
        <div className="space-y-2">
          <p className="text-sm">
            Disable removes it from the list and stops its data. Its history stays.
          </p>
          <Button
            variant="outline"
            size="sm"
            className="text-sev1"
            disabled={disable.isPending}
            onClick={() => {
              if (window.confirm(`Disable “${integration.name}”?`)) disable.mutate()
            }}
          >
            Disable integration
          </Button>
        </div>
        {error && (
          <p role="alert" className="text-sm text-sev1">
            {error.message}
          </p>
        )}
      </CardBody>
    </Card>
  )
}
