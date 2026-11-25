/**
 * The typed API client. Types come from the backend's OpenAPI schema
 * (`make api-client` regenerates `schema.d.ts`); never edit them by hand.
 *
 * Same origin only: the session is an HttpOnly, SameSite=Strict cookie the
 * browser sends by itself. Every non-GET request echoes the readable
 * `sentinel_csrf` cookie in `X-CSRF-Token` (the double-submit check).
 */
import createClient, { type Middleware } from 'openapi-fetch'

import type { components, paths } from './schema'

export type Schemas = components['schemas']
export type Me = Schemas['MeOut']
export type Incident = Schemas['IncidentOut']
export type IncidentDetail = Schemas['IncidentDetail']
export type IncidentStatus = Schemas['IncidentStatus']
export type Severity = Schemas['Severity']
export type TimelineEvent = Schemas['TimelineEventOut']
export type AttachedSignal = Schemas['AttachedSignal']
export type EntityRef = Schemas['EntityRef']
export type Permission = Schemas['Permission']

export const CSRF_COOKIE = 'sentinel_csrf'
export const CSRF_HEADER = 'X-CSRF-Token'
const SAFE = new Set(['GET', 'HEAD', 'OPTIONS'])

export function readCookie(name: string, cookies: string = document.cookie): string | null {
  for (const part of cookies.split(';')) {
    const [key, ...rest] = part.trim().split('=')
    if (key === name) return decodeURIComponent(rest.join('='))
  }
  return null
}

/** A failed call, with the API's own message when it gave one. */
export class ApiError extends Error {
  readonly status: number

  constructor(status: number, message: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

const csrf: Middleware = {
  onRequest({ request }) {
    if (!SAFE.has(request.method)) {
      const token = readCookie(CSRF_COOKIE)
      if (token) request.headers.set(CSRF_HEADER, token)
    }
    return request
  },
}

export const api = createClient<paths>({
  baseUrl: globalThis.location?.origin ?? '', // same origin; absolute for Request()
  credentials: 'same-origin',
  // Looked up per request (not captured once), so tests can stand in for the network.
  fetch: (request) => globalThis.fetch(request),
})
api.use(csrf)

/** The body of a successful call, or an `ApiError` (401 included: the caller decides). */
export function unwrap<T>(result: { data?: T; error?: unknown; response: Response }): T {
  if (result.response.ok && result.data !== undefined) return result.data
  throw new ApiError(result.response.status, messageOf(result.error, result.response))
}

function messageOf(error: unknown, response: Response): string {
  if (error && typeof error === 'object' && 'detail' in error) {
    const detail = (error as { detail: unknown }).detail
    if (typeof detail === 'string') return detail
  }
  return `The request failed (${response.status}).`
}
