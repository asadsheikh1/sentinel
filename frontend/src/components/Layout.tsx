import { NavLink, Outlet } from 'react-router'

import { useAuth } from '@/auth/context'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'

export function Layout() {
  const { me, logout } = useAuth()
  return (
    <div className="min-h-screen">
      <header className="border-b border-line bg-surface">
        <div className="mx-auto flex h-14 max-w-6xl items-center gap-6 px-4">
          <span className="font-semibold tracking-tight">SENTINEL</span>
          <nav aria-label="Main" className="flex gap-1">
            <NavLink
              to="/incidents"
              className={({ isActive }) =>
                cn(
                  'rounded-md px-3 py-1.5 text-sm',
                  isActive ? 'bg-subtle font-medium' : 'text-muted',
                )
              }
            >
              Incidents
            </NavLink>
          </nav>
          {me && (
            <div className="ml-auto flex items-center gap-3 text-sm">
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
