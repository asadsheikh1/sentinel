import { Check, Copy } from 'lucide-react'
import { useState } from 'react'

import type { CheckItem, IntegrationStatusValue } from '@/api/client'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { ago, cn, when } from '@/lib/utils'

export function CopyBlock({ text, label }: { text: string; label: string }) {
  const [copied, setCopied] = useState(false)
  return (
    <div className="overflow-hidden rounded-md border border-line bg-subtle">
      <div className="flex items-center justify-between border-b border-line px-3 py-1">
        <span className="text-xs text-muted">{label}</span>
        <Button
          type="button"
          variant="ghost"
          size="sm"
          onClick={() => {
            void navigator.clipboard?.writeText(text).then(() => setCopied(true))
          }}
        >
          {copied ? <Check className="size-3.5" /> : <Copy className="size-3.5" />}
          {copied ? 'Copied' : 'Copy'}
        </Button>
      </div>
      <pre
        aria-label={label}
        className="overflow-x-auto p-3 font-mono text-xs break-all whitespace-pre-wrap"
      >
        {text}
      </pre>
    </div>
  )
}

/** A secret shown exactly once: the server keeps only a hash of it. */
export function SecretOnce({ secret, what }: { secret: string; what: string }) {
  return (
    <div className="space-y-2 rounded-md border border-sev3/40 bg-sev3/10 p-3">
      <p className="text-sm font-medium">Copy the {what} now. It won’t be shown again.</p>
      <CopyBlock text={secret} label={what} />
      <p className="text-xs text-muted">
        SENTINEL keeps only a hash of it. If it’s lost, rotate it and update the machine.
      </p>
    </div>
  )
}

const STATUS_CLASS: Record<IntegrationStatusValue, string> = {
  healthy: 'border-good/40 text-good',
  degraded: 'border-sev3/40 text-sev3',
  stale: 'border-sev1/40 text-sev1',
  pending: 'border-line text-muted',
  disabled: 'border-line text-muted',
  revoked: 'border-line text-muted',
}

export function IntegrationStatusBadge({ status }: { status: IntegrationStatusValue }) {
  return <Badge className={cn('capitalize', STATUS_CLASS[status])}>{status}</Badge>
}

export function Checklist({ items }: { items: CheckItem[] }) {
  const required = items.filter((i) => i.required)
  const done = required.filter((i) => i.done).length
  return (
    <div className="space-y-3">
      <p className="text-sm text-muted" aria-live="polite">
        {done === required.length
          ? 'Everything required has arrived.'
          : `${done} of ${required.length} done. This updates by itself.`}
      </p>
      <ul className="space-y-2" aria-label="Setup checklist">
        {items.map((item) => (
          <li key={item.key} className="flex gap-3" data-done={item.done}>
            <span
              aria-hidden
              className={cn(
                'mt-0.5 flex size-5 shrink-0 items-center justify-center rounded-full border text-xs',
                item.done ? 'border-good bg-good text-surface' : 'border-line',
              )}
            >
              {item.done ? '✓' : ''}
            </span>
            <div className="min-w-0">
              <p className="text-sm">
                <span className="sr-only">{item.done ? 'Done: ' : 'Waiting: '}</span>
                {item.label}
                {!item.required && <span className="text-muted"> (optional)</span>}
              </p>
              {item.detail && <p className="text-xs break-words text-muted">{item.detail}</p>}
              {item.at && (
                <p className="text-xs text-muted">
                  last{' '}
                  <time dateTime={item.at} title={when(item.at)}>
                    {ago(item.at)}
                  </time>
                </p>
              )}
            </div>
          </li>
        ))}
      </ul>
    </div>
  )
}
