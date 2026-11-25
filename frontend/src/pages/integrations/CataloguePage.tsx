import { Link } from 'react-router'

import { CATALOGUE } from '@/components/integrations/catalogue'
import { Card } from '@/components/ui/card'
import { cn } from '@/lib/utils'

export function CataloguePage() {
  return (
    <div className="space-y-4">
      <Link to="/integrations" className="text-sm text-accent">
        ← Integrations
      </Link>
      <h1 className="text-xl font-semibold">Add an integration</h1>
      <p className="text-sm text-muted">
        Pick what to connect. Each one walks you through setup and shows a live checklist as data
        arrives.
      </p>
      <ul className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3" aria-label="Catalogue">
        {CATALOGUE.map((t) => {
          const body = (
            <Card className={cn('h-full p-4', t.available ? 'hover:border-accent' : 'opacity-60')}>
              <p className="font-medium">{t.title}</p>
              <p className="mt-1 text-sm text-muted">{t.summary}</p>
              {t.note && <p className="mt-2 text-xs text-muted">{t.note}</p>}
            </Card>
          )
          return (
            <li key={t.slug}>
              {t.available ? (
                <Link
                  to={`/integrations/new/${t.slug}`}
                  aria-label={t.title}
                  className="block h-full"
                >
                  {body}
                </Link>
              ) : (
                <div aria-disabled="true" aria-label={`${t.title} (not available yet)`}>
                  {body}
                </div>
              )}
            </li>
          )
        })}
      </ul>
    </div>
  )
}
