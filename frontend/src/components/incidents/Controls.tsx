import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useState, type FormEvent } from 'react'

import {
  api,
  ApiError,
  unwrap,
  type IncidentDetail,
  type IncidentStatus,
  type Severity,
} from '@/api/client'
import { Button } from '@/components/ui/button'
import { Input, Label, Select, Textarea } from '@/components/ui/field'

import { SEVERITIES, STATUS_LABEL, TRANSITIONS } from './labels'

type Patch = { status?: IncidentStatus; severity?: Severity; note?: string }

function usePatch(id: string) {
  const client = useQueryClient()
  return useMutation({
    mutationFn: async (body: Patch) =>
      unwrap(
        await api.PATCH('/api/v1/incidents/{incident_id}', {
          params: { path: { incident_id: id } },
          body,
        }),
      ),
    onSettled: () => client.invalidateQueries({ queryKey: ['incident', id] }),
  })
}

function errorText(error: unknown): string | null {
  if (!error) return null
  return error instanceof ApiError ? error.message : 'Something went wrong. Try again.'
}

/** Status and severity, for people allowed to change them (incidents:write). */
export function IncidentControls({ incident }: { incident: IncidentDetail }) {
  const patch = usePatch(incident.id)
  const targets = TRANSITIONS[incident.status]
  const [status, setStatus] = useState<IncidentStatus | ''>('')
  const [note, setNote] = useState('')

  function changeStatus(event: FormEvent) {
    event.preventDefault()
    if (!status) return
    patch.mutate(
      { status, note: note.trim() || undefined },
      {
        onSuccess: () => {
          setStatus('')
          setNote('')
        },
      },
    )
  }

  return (
    <div className="space-y-4">
      <form onSubmit={changeStatus} className="space-y-2" aria-label="Change status">
        <Label htmlFor="status">Status</Label>
        <Select
          id="status"
          value={status}
          onChange={(e) => setStatus(e.target.value as IncidentStatus | '')}
        >
          <option value="">Move to…</option>
          {targets.map((t) => (
            <option key={t} value={t}>
              {STATUS_LABEL[t]}
            </option>
          ))}
        </Select>
        <Input
          aria-label="Note (optional)"
          placeholder="Note (optional)"
          value={note}
          maxLength={5000}
          onChange={(e) => setNote(e.target.value)}
        />
        <Button type="submit" size="sm" disabled={!status || patch.isPending}>
          Update status
        </Button>
      </form>

      <div className="space-y-2">
        <Label htmlFor="severity">Severity</Label>
        <Select
          id="severity"
          value={incident.severity}
          disabled={patch.isPending}
          onChange={(e) => patch.mutate({ severity: e.target.value as Severity })}
        >
          {SEVERITIES.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </Select>
      </div>

      {patch.error && (
        <p role="alert" className="text-sm text-sev1">
          {errorText(patch.error)}
        </p>
      )}
    </div>
  )
}

export function CommentForm({ incidentId }: { incidentId: string }) {
  const client = useQueryClient()
  const [text, setText] = useState('')
  const comment = useMutation({
    mutationFn: async (body: { text: string }) =>
      unwrap(
        await api.POST('/api/v1/incidents/{incident_id}/comments', {
          params: { path: { incident_id: incidentId } },
          body,
        }),
      ),
    onSuccess: () => {
      setText('')
      return client.invalidateQueries({ queryKey: ['incident', incidentId] })
    },
  })

  return (
    <form
      aria-label="Add a comment"
      className="space-y-2"
      onSubmit={(e) => {
        e.preventDefault()
        if (text.trim()) comment.mutate({ text: text.trim() })
      }}
    >
      <Textarea
        aria-label="Comment"
        placeholder="Add a comment for the timeline"
        value={text}
        maxLength={5000}
        onChange={(e) => setText(e.target.value)}
      />
      <Button type="submit" size="sm" disabled={!text.trim() || comment.isPending}>
        Comment
      </Button>
      {comment.error && (
        <p role="alert" className="text-sm text-sev1">
          {errorText(comment.error)}
        </p>
      )}
    </form>
  )
}
