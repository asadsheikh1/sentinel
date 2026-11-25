/** Signing in and out, and the guard for signed-in pages (see `context.ts`). */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { Navigate, useLocation } from 'react-router'

import { api, unwrap, type Me } from '@/api/client'

import { AuthContext, useAuth, type Auth } from './context'

const ME = ['me'] as const

async function fetchMe(): Promise<Me | null> {
  const result = await api.GET('/api/v1/auth/me')
  if (result.response.status === 401) return null
  return unwrap(result)
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const client = useQueryClient()
  const me = useQuery({ queryKey: ME, queryFn: fetchMe, staleTime: 60_000 })

  const login = useMutation({
    mutationFn: async ({ email, password }: { email: string; password: string }) =>
      unwrap(await api.POST('/api/v1/auth/login', { body: { email, password } })),
    onSuccess: (data) => client.setQueryData(ME, data),
  })
  const logout = useMutation({
    mutationFn: async () => {
      await api.POST('/api/v1/auth/logout')
    },
    onSettled: () => {
      client.clear()
      client.setQueryData(ME, null)
    },
  })

  const value: Auth = {
    me: me.data ?? null,
    loading: me.isPending,
    can: (permission) => me.data?.permissions.includes(permission) ?? false,
    login: async (email, password) => {
      await login.mutateAsync({ email, password })
    },
    logout: async () => {
      await logout.mutateAsync()
    },
  }
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

/** Signed-in only; otherwise to the login page, coming back here afterwards. */
export function RequireAuth({ children }: { children: ReactNode }) {
  const { me, loading } = useAuth()
  const location = useLocation()
  if (loading) {
    return (
      <p className="p-8 text-sm text-muted" role="status">
        Loading…
      </p>
    )
  }
  if (!me) {
    return <Navigate to="/login" replace state={{ from: location.pathname + location.search }} />
  }
  return <>{children}</>
}
