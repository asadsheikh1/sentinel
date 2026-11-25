import type { AttachedSignal } from '@/api/client'
import { Badge } from '@/components/ui/badge'
import { ago, when } from '@/lib/utils'

import { REASON_LABEL } from './labels'

const KIND: Record<string, string> = {
  prom_alert: 'Alert',
  error_group_new: 'New error',
  error_group_spike: 'Error spike',
  telemetry_gap: 'Telemetry gap',
  health: 'Health',
}

export function SignalsTable({ signals }: { signals: AttachedSignal[] }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-sm">
        <thead className="text-xs text-muted">
          <tr>
            <th className="py-2 pr-3 font-medium">Signal</th>
            <th className="py-2 pr-3 font-medium">Kind</th>
            <th className="py-2 pr-3 font-medium">Why it's here</th>
            <th className="py-2 pr-3 font-medium">Started</th>
            <th className="py-2 font-medium">Status</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-line">
          {signals.map((s) => (
            <tr key={s.id} className="align-top">
              <td className="max-w-md py-2 pr-3 break-words">{s.title}</td>
              <td className="py-2 pr-3 whitespace-nowrap">
                {KIND[s.kind] ?? s.kind} <span className="text-xs text-muted">({s.severity})</span>
              </td>
              <td className="py-2 pr-3 whitespace-nowrap">{REASON_LABEL[s.reason] ?? s.reason}</td>
              <td className="py-2 pr-3 whitespace-nowrap">
                <time dateTime={s.starts_at} title={when(s.starts_at)}>
                  {ago(s.starts_at)}
                </time>
              </td>
              <td className="py-2">
                <Badge className={s.status === 'firing' ? 'text-sev1' : 'text-good'}>
                  {s.status}
                </Badge>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
