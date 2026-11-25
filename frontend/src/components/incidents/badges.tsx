import type { IncidentStatus, Severity } from '@/api/client'
import { Badge } from '@/components/ui/badge'
import { cn } from '@/lib/utils'

import { SEVERITY_CLASS, STATUS_CLASS, STATUS_LABEL } from './labels'

export function SeverityBadge({ severity }: { severity: Severity }) {
  return <Badge className={cn('font-semibold', SEVERITY_CLASS[severity])}>{severity}</Badge>
}

export function StatusBadge({ status }: { status: IncidentStatus }) {
  return <Badge className={STATUS_CLASS[status]}>{STATUS_LABEL[status]}</Badge>
}
