/**
 * The signed-in user, as the UI sees it. The server decides who may do what:
 * this mirrors `/api/v1/auth/me` so the UI can hide controls a role can't use,
 * and the API checks every permission again on every request.
 */
import { createContext, useContext } from 'react'

import { ApiError, type Me, type Permission } from '@/api/client'

export type Auth = {
  me: Me | null
  loading: boolean
  can: (permission: Permission) => boolean
  login: (email: string, password: string) => Promise<void>
  logout: () => Promise<void>
}

export const AuthContext = createContext<Auth | null>(null)

export function useAuth(): Auth {
  const auth = useContext(AuthContext)
  if (!auth) throw new Error('useAuth needs an <AuthProvider>')
  return auth
}

/** A session that expired mid-use: any 401 sends the user back to sign in. */
export function isUnauthorised(error: unknown): boolean {
  return error instanceof ApiError && error.status === 401
}
