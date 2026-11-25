import type { IntegrationKind } from '@/api/client'

export type Tile = {
  slug: string
  title: string
  summary: string
  /** What the wizard creates: an integration (with an ingest token) or a scoped API key. */
  creates:
    { integration: IntegrationKind } | { apiKey: 'deployments:write' | 'alerts:write' } | null
  available: boolean
  note?: string
}

/** The MVP catalogue (integrations design §5.2). */
export const CATALOGUE: Tile[] = [
  {
    slug: 'linux-host',
    title: 'Linux host',
    summary: 'A VM or server: CPU, memory, disks, processes and the system journal.',
    creates: { integration: 'linux_host' },
    available: true,
  },
  {
    slug: 'container-host',
    title: 'Container host',
    summary: 'A host running Podman or Docker: the host, plus each container’s stats and logs.',
    creates: { integration: 'container_host' },
    available: true,
  },
  {
    slug: 'python-app',
    title: 'Python app',
    summary: 'Traces, logs and metrics from your app with OpenTelemetry auto-instrumentation.',
    creates: { integration: 'otel_app' },
    available: true,
  },
  {
    slug: 'http-check',
    title: 'HTTP check',
    summary: 'Probe a URL from outside, on a schedule: status, latency and certificate.',
    creates: { integration: 'http_check' },
    available: true,
  },
  {
    slug: 'deployments',
    title: 'Deployment webhook',
    summary: 'Tell SENTINEL about every deploy, so incidents link to what changed.',
    creates: { apiKey: 'deployments:write' },
    available: true,
  },
  {
    slug: 'alertmanager',
    title: 'Alertmanager',
    summary: 'Send your own Prometheus alerts to SENTINEL as signals.',
    creates: { apiKey: 'alerts:write' },
    available: true,
  },
  {
    slug: 'github',
    title: 'GitHub',
    summary: 'Repositories, commits and pull requests for code-aware investigations.',
    creates: null,
    available: false,
    note: 'Arrives with the repository steps.',
  },
]

export const KIND_LABEL: Record<IntegrationKind, string> = {
  linux_host: 'Linux host',
  container_host: 'Container host',
  otel_app: 'App (OpenTelemetry)',
  http_check: 'HTTP check',
  deploy_webhook: 'Deployment webhook',
  github: 'GitHub',
  alertmanager: 'Alertmanager',
  postgresql: 'PostgreSQL',
  checkin: 'Check-in',
  aws_account: 'AWS account',
  upload: 'Upload',
  prober: 'SENTINEL prober',
}

export function tile(slug: string): Tile | undefined {
  return CATALOGUE.find((t) => t.slug === slug)
}
