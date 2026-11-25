import { useMutation, useQuery } from '@tanstack/react-query'
import { useState, type FormEvent, type ReactNode } from 'react'
import { Link, useParams } from 'react-router'

import {
  api,
  ApiError,
  unwrap,
  type ApiKey,
  type Integration,
  type IntegrationKind,
} from '@/api/client'
import { useAuth } from '@/auth/context'
import { tile, type Tile } from '@/components/integrations/catalogue'
import {
  AgentInstructions,
  AlertmanagerInstructions,
  AppInstructions,
  DeploymentInstructions,
} from '@/components/integrations/Instructions'
import { Checklist, SecretOnce } from '@/components/integrations/parts'
import { useIntegrationStatus, useMeta } from '@/components/integrations/queries'
import { Button } from '@/components/ui/button'
import { Card, CardBody, CardHeader, CardTitle } from '@/components/ui/card'
import { Input, Label, Select } from '@/components/ui/field'

type Created =
  | { type: 'integration'; integration: Integration; token: string; service?: string }
  | { type: 'apiKey'; key: ApiKey; secret: string }

const list = (text: string) =>
  text
    .split(/[\n,]/)
    .map((s) => s.trim())
    .filter(Boolean)

function errorText(error: unknown): string {
  return error instanceof ApiError ? error.message : 'Something went wrong. Try again.'
}

export function WizardPage() {
  const { slug = '' } = useParams()
  const { can } = useAuth()
  const t = tile(slug)
  const [created, setCreated] = useState<Created | null>(null)

  if (!t || !t.available || !t.creates) {
    return (
      <p className="text-sm">
        That integration isn’t available.{' '}
        <Link to="/integrations/new" className="text-accent">
          Back to the catalogue
        </Link>
      </p>
    )
  }
  const needs = 'apiKey' in t.creates ? 'apikeys:manage' : 'integrations:manage'
  if (!can(needs)) {
    return <p className="text-sm text-muted">Only an admin can add integrations.</p>
  }

  return (
    <div className="mx-auto max-w-3xl space-y-4">
      <Link to="/integrations/new" className="text-sm text-accent">
        ← Catalogue
      </Link>
      <h1 className="text-xl font-semibold">Add: {t.title}</h1>
      <Steps current={created ? 2 : 1} />
      {created ? (
        <Connect tile={t} created={created} />
      ) : (
        <Configure tile={t} onCreated={setCreated} />
      )}
    </div>
  )
}

function Steps({ current }: { current: 1 | 2 }) {
  const steps = ['Configure', 'Connect and verify']
  return (
    <ol className="flex gap-4 text-sm" aria-label="Steps">
      {steps.map((s, i) => (
        <li
          key={s}
          aria-current={i + 1 === current ? 'step' : undefined}
          className={i + 1 === current ? 'font-medium' : 'text-muted'}
        >
          {i + 1}. {s}
        </li>
      ))}
    </ol>
  )
}

function Field({
  id,
  label,
  hint,
  children,
}: {
  id: string
  label: string
  hint?: string
  children: ReactNode
}) {
  return (
    <div className="space-y-1.5">
      <Label htmlFor={id}>{label}</Label>
      {children}
      {hint && <p className="text-xs text-muted">{hint}</p>}
    </div>
  )
}

// ------------------------------------------------------------------ step 1

function Configure({ tile: t, onCreated }: { tile: Tile; onCreated: (c: Created) => void }) {
  const creates = t.creates!
  const kind: IntegrationKind | null = 'integration' in creates ? creates.integration : null
  const [name, setName] = useState('')
  const [environment, setEnvironment] = useState('prod')
  const [processes, setProcesses] = useState('')
  const [logFiles, setLogFiles] = useState('')
  const [journald, setJournald] = useState(true)
  const [service, setService] = useState('')
  const [metrics, setMetrics] = useState(false)
  const [url, setUrl] = useState('https://')
  const [intervalS, setIntervalS] = useState('60')
  const [expected, setExpected] = useState('200')

  const create = useMutation({
    mutationFn: async (): Promise<Created> => {
      if ('apiKey' in creates) {
        const made = unwrap(
          await api.POST('/api/v1/api-keys', { body: { name, scopes: [creates.apiKey] } }),
        )
        return { type: 'apiKey', key: made.api_key, secret: made.secret }
      }
      const agent = kind === 'linux_host' || kind === 'container_host'
      const config: Record<string, unknown> = agent
        ? {
            watched_processes: list(processes),
            log_files: list(logFiles).map((path) => ({ path })),
            journald,
          }
        : {}
      const expected_signals: ('metrics' | 'logs' | 'traces')[] =
        kind === 'otel_app'
          ? ['traces', 'logs', ...(metrics ? (['metrics'] as const) : [])]
          : kind === 'http_check'
            ? ['metrics']
            : ['metrics', 'logs']
      const made = unwrap(
        await api.POST('/api/v1/integrations', {
          body: { name, kind: kind!, environment, config, expected_signals },
        }),
      )
      if (kind === 'http_check') {
        const statuses = list(expected).map(Number)
        unwrap(
          await api.POST('/api/v1/checks', {
            body: {
              integration_id: made.integration.id,
              name,
              kind: 'http',
              target: url.trim(),
              interval_s: Number(intervalS),
              timeout_s: 10,
              assertions: { expected_status: statuses },
            },
          }),
        )
      }
      return {
        type: 'integration',
        integration: made.integration,
        token: made.ingest_token,
        service,
      }
    },
    onSuccess: onCreated,
  })

  function submit(event: FormEvent) {
    event.preventDefault()
    create.mutate()
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Configure</CardTitle>
      </CardHeader>
      <CardBody>
        <form onSubmit={submit} className="space-y-4" aria-label="Configure the integration">
          <Field id="name" label="Name" hint="A name you’ll recognise, e.g. the host or app name.">
            <Input
              id="name"
              required
              maxLength={100}
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
          </Field>

          {kind && kind !== 'http_check' && (
            <Field id="environment" label="Environment">
              <Select
                id="environment"
                value={environment}
                onChange={(e) => setEnvironment(e.target.value)}
              >
                {['prod', 'staging', 'dev', 'test'].map((env) => (
                  <option key={env}>{env}</option>
                ))}
              </Select>
            </Field>
          )}

          {(kind === 'linux_host' || kind === 'container_host') && (
            <>
              <Field
                id="processes"
                label="Processes to watch (optional)"
                hint="Comma-separated, e.g. nginx, postgres. An alert fires if one disappears."
              >
                <Input
                  id="processes"
                  value={processes}
                  onChange={(e) => setProcesses(e.target.value)}
                />
              </Field>
              <Field
                id="logfiles"
                label="Log files to collect (optional)"
                hint="Absolute paths, one per line. The system journal is collected separately."
              >
                <textarea
                  id="logfiles"
                  value={logFiles}
                  onChange={(e) => setLogFiles(e.target.value)}
                  className="min-h-16 w-full rounded-md border border-line bg-surface px-3 py-2 font-mono text-xs"
                />
              </Field>
              <label className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={journald}
                  onChange={(e) => setJournald(e.target.checked)}
                />
                Collect the system journal (needed for the setup check)
              </label>
            </>
          )}

          {kind === 'otel_app' && (
            <>
              <Field
                id="service"
                label="Service name"
                hint="What the app will be called in SENTINEL (OTEL_SERVICE_NAME)."
              >
                <Input
                  id="service"
                  required
                  pattern="[A-Za-z0-9_.\-]{1,100}"
                  value={service}
                  onChange={(e) => setService(e.target.value)}
                />
              </Field>
              <label className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={metrics}
                  onChange={(e) => setMetrics(e.target.checked)}
                />
                The app also sends metrics (expect them, and warn if they stop)
              </label>
            </>
          )}

          {kind === 'http_check' && (
            <>
              <Field
                id="url"
                label="URL"
                hint="Public addresses only: private and internal ones are refused."
              >
                <Input
                  id="url"
                  type="url"
                  required
                  value={url}
                  onChange={(e) => setUrl(e.target.value)}
                />
              </Field>
              <div className="grid grid-cols-2 gap-4">
                <Field id="interval" label="Every (seconds)">
                  <Input
                    id="interval"
                    type="number"
                    min={30}
                    max={86400}
                    required
                    value={intervalS}
                    onChange={(e) => setIntervalS(e.target.value)}
                  />
                </Field>
                <Field id="expected" label="Expected status codes">
                  <Input
                    id="expected"
                    required
                    value={expected}
                    onChange={(e) => setExpected(e.target.value)}
                  />
                </Field>
              </div>
            </>
          )}

          {create.error && (
            <p role="alert" className="text-sm text-sev1">
              {errorText(create.error)}
            </p>
          )}
          <Button type="submit" disabled={create.isPending}>
            {create.isPending ? 'Creating…' : 'Create and show the setup'}
          </Button>
        </form>
      </CardBody>
    </Card>
  )
}

// ------------------------------------------------------------------ step 2

function Connect({ tile: t, created }: { tile: Tile; created: Created }) {
  const origin = window.location.origin
  const meta = useMeta()
  if (created.type === 'apiKey') {
    return (
      <div className="space-y-4">
        <Card>
          <CardHeader>
            <CardTitle>Connect</CardTitle>
          </CardHeader>
          <CardBody className="space-y-4">
            <SecretOnce secret={created.secret} what="API key" />
            {t.slug === 'deployments' ? (
              <DeploymentInstructions origin={origin} />
            ) : (
              <AlertmanagerInstructions origin={origin} secret={created.secret} />
            )}
          </CardBody>
        </Card>
        <KeyChecklist keyId={created.key.id} />
      </div>
    )
  }
  const { integration, token } = created
  const agent = integration.kind === 'linux_host' || integration.kind === 'container_host'
  return (
    <div className="space-y-4">
      <Card>
        <CardHeader>
          <CardTitle>Connect</CardTitle>
        </CardHeader>
        <CardBody className="space-y-4">
          {integration.kind !== 'http_check' && <SecretOnce secret={token} what="ingest token" />}
          {agent && <AgentSetup id={integration.id} />}
          {integration.kind === 'otel_app' && meta.data && (
            <AppInstructions
              endpoint={meta.data.ingest_endpoint}
              service={created.service ?? integration.name}
            />
          )}
          {integration.kind === 'http_check' && (
            <p className="text-sm">
              The check is saved. SENTINEL’s prober runs it from outside your network now and then
              on schedule.
            </p>
          )}
        </CardBody>
      </Card>
      <IntegrationChecklist id={integration.id} />
      <p className="text-sm">
        <Link to={`/integrations/${integration.id}`} className="text-accent">
          Open the integration
        </Link>
        {' · '}
        <Link to="/inventory" className="text-accent">
          Inventory
        </Link>
      </p>
    </div>
  )
}

function AgentSetup({ id }: { id: string }) {
  const setup = useQuery({
    queryKey: ['integration', id, 'setup'],
    queryFn: async () =>
      unwrap(
        await api.GET('/api/v1/integrations/{integration_id}/setup', {
          params: { path: { integration_id: id } },
        }),
      ),
  })
  if (setup.isPending) return <p className="text-sm text-muted">Loading the install steps…</p>
  if (setup.isError)
    return (
      <p role="alert" className="text-sm text-sev1">
        {setup.error.message}
      </p>
    )
  return <AgentInstructions setup={setup.data} />
}

export function IntegrationChecklist({ id }: { id: string }) {
  const status = useIntegrationStatus(id, 3_000)
  return (
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
  )
}

function KeyChecklist({ keyId }: { keyId: string }) {
  const keys = useQuery({
    queryKey: ['api-keys'],
    queryFn: async () => unwrap(await api.GET('/api/v1/api-keys')),
    refetchInterval: 3_000,
  })
  const key = keys.data?.find((k) => k.id === keyId)
  return (
    <Card>
      <CardHeader>
        <CardTitle>Verification</CardTitle>
      </CardHeader>
      <CardBody>
        <Checklist
          items={[
            {
              key: 'key_used',
              label: 'SENTINEL received a request with this key',
              done: Boolean(key?.last_used_at),
              required: true,
              detail: null,
              at: key?.last_used_at ?? null,
            },
          ]}
        />
      </CardBody>
    </Card>
  )
}
