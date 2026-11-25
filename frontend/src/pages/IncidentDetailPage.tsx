import { useQuery } from '@tanstack/react-query'
import { Link, useParams } from 'react-router'

import { api, unwrap } from '@/api/client'
import { useAuth } from '@/auth/context'
import { SeverityBadge, StatusBadge } from '@/components/incidents/badges'
import { CommentForm, IncidentControls } from '@/components/incidents/Controls'
import { Deployment, Entities } from '@/components/incidents/Entities'
import { SignalsTable } from '@/components/incidents/SignalsTable'
import { Timeline } from '@/components/incidents/Timeline'
import { Card, CardBody, CardHeader, CardTitle } from '@/components/ui/card'
import { ago, when } from '@/lib/utils'

import { REFRESH_MS } from './IncidentsPage'

export function IncidentDetailPage() {
  const { id = '' } = useParams()
  const { me, can } = useAuth()
  const canWrite = can('incidents:write')

  const incident = useQuery({
    queryKey: ['incident', id],
    queryFn: async () =>
      unwrap(
        await api.GET('/api/v1/incidents/{incident_id}', {
          params: { path: { incident_id: id } },
        }),
      ),
    refetchInterval: REFRESH_MS,
  })
  const timeline = useQuery({
    queryKey: ['incident', id, 'timeline'],
    queryFn: async () =>
      unwrap(
        await api.GET('/api/v1/incidents/{incident_id}/timeline', {
          params: { path: { incident_id: id } },
        }),
      ),
    refetchInterval: REFRESH_MS,
  })

  if (incident.isPending) {
    return (
      <p className="text-sm text-muted" role="status">
        Loading incident…
      </p>
    )
  }
  if (incident.isError) {
    return (
      <div className="space-y-2">
        <p role="alert" className="text-sm text-sev1">
          {incident.error.message}
        </p>
        <Link to="/incidents" className="text-sm text-accent">
          Back to incidents
        </Link>
      </div>
    )
  }
  const i = incident.data

  return (
    <div className="space-y-4">
      <Link to="/incidents" className="text-sm text-accent">
        ← Incidents
      </Link>
      <header className="space-y-2">
        <div className="flex flex-wrap items-center gap-2">
          <SeverityBadge severity={i.severity} />
          <StatusBadge status={i.status} />
          <span className="font-mono text-xs text-muted">{i.ref}</span>
        </div>
        <h1 className="text-xl font-semibold break-words">{i.title}</h1>
        <p className="text-sm text-muted">
          Started{' '}
          <time dateTime={i.started_at} title={when(i.started_at)}>
            {ago(i.started_at)}
          </time>
          {i.service_name && <> · {i.service_name}</>} · last signal{' '}
          <time dateTime={i.last_signal_at} title={when(i.last_signal_at)}>
            {ago(i.last_signal_at)}
          </time>
        </p>
      </header>

      <div className="grid gap-4 lg:grid-cols-3">
        <div className="min-w-0 space-y-4 lg:col-span-2">
          <Card>
            <CardHeader>
              <CardTitle>Signals ({i.signals.length})</CardTitle>
            </CardHeader>
            <CardBody>
              <SignalsTable signals={i.signals} />
            </CardBody>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle>Timeline</CardTitle>
            </CardHeader>
            <CardBody className="space-y-4">
              {timeline.isError ? (
                <p role="alert" className="text-sm text-sev1">
                  {timeline.error.message}
                </p>
              ) : (
                <Timeline events={timeline.data ?? []} meId={me?.user.id} />
              )}
              {canWrite && <CommentForm incidentId={i.id} />}
            </CardBody>
          </Card>
        </div>

        <div className="min-w-0 space-y-4">
          <Card>
            <CardHeader>
              <CardTitle>Respond</CardTitle>
            </CardHeader>
            <CardBody>
              {canWrite ? (
                <IncidentControls incident={i} />
              ) : (
                <p className="text-sm text-muted">
                  Your role can view incidents but not change them.
                </p>
              )}
            </CardBody>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle>Entities</CardTitle>
            </CardHeader>
            <CardBody>
              <Entities entities={i.entities} />
            </CardBody>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle>Suspected deployment</CardTitle>
            </CardHeader>
            <CardBody>
              <Deployment deployment={i.suspected_deployment} />
            </CardBody>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle>Investigation</CardTitle>
            </CardHeader>
            <CardBody>
              <p className="text-sm text-muted">
                Queued. The AI investigator arrives in a later step.
              </p>
            </CardBody>
          </Card>
        </div>
      </div>
    </div>
  )
}
