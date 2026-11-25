import type { TimelineEvent } from '@/api/client'
import { ago, cn, when } from '@/lib/utils'

import { actorLabel, describe } from './labels'

export function Timeline({ events, meId }: { events: TimelineEvent[]; meId?: string }) {
  if (events.length === 0) return <p className="text-sm text-muted">Nothing yet.</p>
  return (
    <ol className="space-y-3" aria-label="Timeline">
      {events.map((event) => {
        const actor = meId && event.actor === `user:${meId}` ? 'You' : actorLabel(event.actor)
        return (
          <li key={event.id} className="flex gap-3">
            <span
              aria-hidden
              className={cn(
                'mt-1.5 size-2 shrink-0 rounded-full',
                event.type === 'comment' ? 'bg-accent' : 'bg-line',
              )}
            />
            <div className="min-w-0 flex-1">
              <p
                className={cn(
                  'text-sm break-words whitespace-pre-wrap',
                  event.type === 'comment' && 'rounded-md bg-subtle px-3 py-2',
                )}
              >
                {describe(event)}
              </p>
              <p className="mt-0.5 text-xs text-muted">
                {actor} ·{' '}
                <time dateTime={event.ts} title={when(event.ts)}>
                  {ago(event.ts)}
                </time>
              </p>
            </div>
          </li>
        )
      })}
    </ol>
  )
}
