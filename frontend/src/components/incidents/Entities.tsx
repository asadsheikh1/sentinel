import type { EntityRef, IncidentDetail } from '@/api/client'
import { when } from '@/lib/utils'

export function Entities({ entities }: { entities: EntityRef[] }) {
  if (entities.length === 0) {
    return <p className="text-sm text-muted">No inventory entity is linked yet.</p>
  }
  return (
    <ul className="space-y-2 text-sm">
      {entities.map((e) => (
        <li key={e.id} className="flex items-baseline justify-between gap-3">
          <span className="min-w-0 break-all">
            <span className="font-medium">{e.display_name}</span>{' '}
            <span className="text-xs text-muted">
              {e.type} · {e.key}
            </span>
          </span>
          <span className="text-xs whitespace-nowrap text-muted">
            {e.role === 'origin' ? 'Where it started' : 'Also affected'}
          </span>
        </li>
      ))}
    </ul>
  )
}

export function Deployment({ deployment }: { deployment: IncidentDetail['suspected_deployment'] }) {
  if (!deployment) return <p className="text-sm text-muted">No deployment in the hour before.</p>
  return (
    <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
      <dt className="text-muted">Service</dt>
      <dd>{deployment.service_name}</dd>
      <dt className="text-muted">Version</dt>
      <dd className="break-all">{deployment.version ?? '—'}</dd>
      <dt className="text-muted">Commit</dt>
      <dd className="font-mono text-xs">
        {deployment.commit_sha?.slice(0, 12) ?? '—'}
        {deployment.previous_commit_sha && (
          <span className="text-muted">
            {' '}
            (before: {deployment.previous_commit_sha.slice(0, 12)})
          </span>
        )}
      </dd>
      <dt className="text-muted">Deployed</dt>
      <dd>{when(deployment.deployed_at)}</dd>
    </dl>
  )
}
