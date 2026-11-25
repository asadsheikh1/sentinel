import { NavLink, Outlet } from 'react-router'

import { useAuth } from '@/auth/context'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'

const NAV: [string, string][] = [
  ['/incidents', 'Incidents'],
  ['/integrations', 'Integrations'],
  ['/inventory', 'Inventory'],
]

export function Layout() {
  const { me, logout } = useAuth()
  return (
    <div className="min-h-screen">
      <header className="border-b border-line bg-surface">
        <div className="mx-auto flex h-14 max-w-6xl items-center gap-3 px-4 sm:gap-6">
          <span className="font-semibold tracking-tight">SENTINEL</span>
          <nav aria-label="Main" className="flex min-w-0 gap-1 overflow-x-auto">
            {NAV.map(([to, label]) => (
              <NavLink
                key={to}
                to={to}
                className={({ isActive }) =>
                  cn(
                    'rounded-md px-2 py-1.5 text-sm whitespace-nowrap sm:px-3',
                    isActive ? 'bg-subtle font-medium' : 'text-muted',
                  )
                }
              >
                {label}
              </NavLink>
            ))}
          </nav>
          {me && (
            <div className="ml-auto flex shrink-0 items-center gap-3 text-sm">
              <span className="hidden text-muted sm:inline">
                {me.user.display_name} · <span className="capitalize">{me.user.role}</span>
              </span>
              <Button variant="outline" size="sm" onClick={() => void logout()}>
                Sign out
              </Button>
            </div>
          )}
        </div>
      </header>
      <main className="mx-auto max-w-6xl px-4 py-6">
        <Outlet />
      </main>
    </div>
  )
}
