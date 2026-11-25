/**
 * A stand-in for the API in component tests: routes by method and path, records
 * every request (with its headers) so tests can check what the UI sent.
 */
import { vi } from 'vitest'

type Handler = (request: Request, match: RegExpMatchArray) => Response | Promise<Response>

export function fakeApi() {
  const routes: { method: string; path: RegExp; handler: Handler }[] = []
  const requests: Request[] = []
  const fetch = vi.fn(async (input: RequestInfo | URL) => {
    const request = input instanceof Request ? input : new Request(String(input))
    requests.push(request.clone())
    const url = new URL(request.url, 'http://localhost')
    for (const r of routes) {
      const match = url.pathname.match(r.path)
      if (r.method === request.method && match) return r.handler(request, match)
    }
    return Response.json(
      { detail: `no fake for ${request.method} ${url.pathname}` },
      { status: 404 },
    )
  })
  vi.stubGlobal('fetch', fetch)
  return {
    requests,
    on(method: string, path: RegExp, handler: Handler | object) {
      routes.unshift({
        method,
        path,
        handler:
          typeof handler === 'function' ? (handler as Handler) : () => Response.json(handler),
      })
      return this
    },
  }
}

export const VIEWER_PERMS = ['incidents:read']
export const RESPONDER_PERMS = [
  'incidents:read',
  'incidents:write',
  'patches:create',
  'actions:request',
]

export function me(role: 'viewer' | 'responder', permissions: string[]) {
  return {
    user: {
      id: '00000000-0000-0000-0000-0000000000aa',
      email: `${role}@example.com`,
      display_name: `A ${role}`,
      role,
      is_active: true,
      has_password: true,
      last_login_at: null,
      created_at: '2026-11-25T10:00:00Z',
    },
    permissions,
    csrf_token: 'csrf-from-me',
  }
}

const T = '2026-11-25T10:00:00Z'

export function incident(overrides: Record<string, unknown> = {}) {
  return {
    id: '11111111-1111-1111-1111-111111111111',
    number: 7,
    ref: 'INC-7',
    title: 'orders: 100% of requests fail',
    status: 'open',
    severity: 'SEV1',
    service_name: 'orders',
    primary_entity_id: null,
    suspected_deployment_id: null,
    started_at: T,
    detected_at: T,
    last_signal_at: T,
    mitigated_at: null,
    resolved_at: null,
    closed_at: null,
    created_by: 'system',
    updated_at: T,
    ...overrides,
  }
}

export function detail(overrides: Record<string, unknown> = {}) {
  return {
    ...incident(),
    summary: null,
    investigation: null,
    entities: [
      {
        id: 'e1',
        type: 'service',
        key: 'aviro/orders@dev',
        display_name: 'orders',
        role: 'origin',
      },
    ],
    signals: [
      {
        id: 's1',
        kind: 'prom_alert',
        status: 'firing',
        severity: 'critical',
        title: 'orders: 100% of requests fail',
        entity_id: 'e1',
        service_name: 'orders',
        source_integration_id: null,
        labels: {},
        payload: {},
        starts_at: T,
        ends_at: null,
        received_at: T,
        reason: 'first_signal',
      },
    ],
    suspected_deployment: {
      id: 'd1',
      service_name: 'orders',
      version: 'scenario/S01-bad',
      commit_sha: 'f927540971ee0000',
      previous_commit_sha: null,
      deployed_at: T,
      source: 'script',
    },
    ...overrides,
  }
}

export function event(id: number, type: string, data: Record<string, unknown>, actor = 'system') {
  return { id, ts: T, type, actor, data }
}
