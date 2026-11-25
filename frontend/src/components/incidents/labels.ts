import type { IncidentStatus, Severity, TimelineEvent } from '@/api/client'

export const STATUS_LABEL: Record<IncidentStatus, string> = {
  open: 'Open',
  investigating: 'Investigating',
  mitigated: 'Mitigated',
  resolved: 'Resolved',
  closed: 'Closed',
  false_positive: 'False positive',
}

/** The moves the server's state machine allows (it checks again; a refusal is shown). */
export const TRANSITIONS: Record<IncidentStatus, IncidentStatus[]> = {
  open: ['investigating', 'mitigated', 'resolved', 'false_positive'],
  investigating: ['mitigated', 'resolved', 'false_positive'],
  mitigated: ['open', 'investigating', 'resolved', 'false_positive'],
  resolved: ['closed', 'open', 'false_positive'],
  closed: ['false_positive'],
  false_positive: ['open'],
}

export const SEVERITIES: Severity[] = ['SEV1', 'SEV2', 'SEV3', 'SEV4']

export const SEVERITY_CLASS: Record<Severity, string> = {
  SEV1: 'border-sev1/40 bg-sev1/10 text-sev1',
  SEV2: 'border-sev2/40 bg-sev2/10 text-sev2',
  SEV3: 'border-sev3/40 bg-sev3/10 text-sev3',
  SEV4: 'border-sev4/40 bg-sev4/10 text-sev4',
}

export const STATUS_CLASS: Record<IncidentStatus, string> = {
  open: 'border-sev1/40 text-sev1',
  investigating: 'border-sev2/40 text-sev2',
  mitigated: 'border-sev3/40 text-sev3',
  resolved: 'border-good/40 text-good',
  closed: 'border-line text-muted',
  false_positive: 'border-line text-muted',
}

export const REASON_LABEL: Record<string, string> = {
  first_signal: 'Opened the incident',
  same_entity: 'Same entity',
  same_service: 'Same service',
  related_entity: 'Related in the topology',
  shared_trace: 'Shares a trace',
  deploy_window: 'Same release',
}

function str(value: unknown): string {
  return typeof value === 'string' ? value : value == null ? '' : JSON.stringify(value)
}

/** One line per timeline event. Plain text: values from signals, logs and people
 *  are untrusted and are only ever rendered as text, never as HTML. */
export function describe(event: TimelineEvent): string {
  const d = event.data as Record<string, unknown>
  switch (event.type) {
    case 'opened':
      return `Incident opened at ${str(d.severity)}: ${str(d.title)}`
    case 'signal_attached':
      if (d.reason === 'first_signal') return `First signal: ${str(d.title)}`
      return `Signal attached (${REASON_LABEL[str(d.reason)] ?? str(d.reason)}): ${str(d.title)}`
    case 'severity_changed':
      return `Severity ${str(d.from)} → ${str(d.to)}${d.manual ? ' (set by hand)' : ''}`
    case 'status_changed':
      return `Status ${STATUS_LABEL[d.from as IncidentStatus] ?? str(d.from)} → ${
        STATUS_LABEL[d.to as IncidentStatus] ?? str(d.to)
      }${d.note ? `: ${str(d.note)}` : ''}`
    case 'deployment':
      return `Suspected deployment: ${str(d.service)} ${str(d.version)}${
        d.commit_sha ? ` (${str(d.commit_sha).slice(0, 8)})` : ''
      }`
    case 'investigation_queued':
      return 'Investigation queued'
    case 'comment':
      return str(d.text)
    default:
      return event.type.replaceAll('_', ' ')
  }
}

export function actorLabel(actor: string): string {
  return actor === 'system' ? 'SENTINEL' : actor.startsWith('user:') ? 'A person' : actor
}
