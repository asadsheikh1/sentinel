/**
 * Real accounts and real incidents for the browser tests: an admin from the CLI,
 * a viewer and a responder invited through the API, and incidents raised by
 * posting an alert to Alertmanager (the full path: webhook → signal → correlator).
 */
import { spawnSync } from 'node:child_process'
import { randomBytes, randomUUID } from 'node:crypto'

import { request, type APIRequestContext } from '@playwright/test'

const WEB = process.env.SENTINEL_WEB_URL ?? 'http://127.0.0.1:3000'
const ALERTMANAGER = process.env.SENTINEL_ALERTMANAGER_URL ?? 'http://127.0.0.1:9093'

export type Account = { email: string; password: string }

function password(): string {
  return randomBytes(18).toString('base64url')
}

async function signedIn(account: Account): Promise<{ ctx: APIRequestContext; csrf: string }> {
  const ctx = await request.newContext({ baseURL: WEB })
  const res = await ctx.post('/api/v1/auth/login', { data: account })
  if (!res.ok()) throw new Error(`login failed: ${res.status()} ${await res.text()}`)
  return { ctx, csrf: (await res.json()).csrf_token }
}

let adminSession: Promise<{ ctx: APIRequestContext; csrf: string }> | undefined

/** One admin sign-in for the whole run (sign-ins are rate limited per address). */
function admin(account: Account) {
  adminSession ??= signedIn(account)
  return adminSession
}

export async function accounts(): Promise<Record<'admin' | 'viewer' | 'responder', Account>> {
  const tag = randomUUID().slice(0, 8)
  const adminAccount = { email: `e2e-admin-${tag}@example.com`, password: password() }
  const created = spawnSync(
    'uv',
    ['run', '--directory', '../backend', 'sentinel', 'create-admin', '--email', adminAccount.email,
     '--name', 'E2E Admin', '--password-stdin'],
    { input: `${adminAccount.password}\n`, encoding: 'utf8' },
  ) // prettier-ignore
  if (created.status !== 0) throw new Error(`create-admin failed: ${created.stderr}`)

  const { ctx, csrf } = await admin(adminAccount)
  const out: Record<string, Account> = { admin: adminAccount }
  for (const role of ['viewer', 'responder'] as const) {
    const account = { email: `e2e-${role}-${tag}@example.com`, password: password() }
    const res = await ctx.post('/api/v1/users', {
      data: { email: account.email, display_name: `E2E ${role}`, role },
      headers: { 'X-CSRF-Token': csrf },
    })
    if (!res.ok()) throw new Error(`create ${role}: ${res.status()} ${await res.text()}`)
    const { invite_token } = await res.json()
    const anon = await request.newContext({ baseURL: WEB })
    const accepted = await anon.post('/api/v1/auth/accept-invite', {
      data: { token: invite_token, password: account.password },
    })
    if (!accepted.ok()) throw new Error(`accept invite: ${accepted.status()}`)
    await anon.dispose()
    out[role] = account
  }
  return out as Record<'admin' | 'viewer' | 'responder', Account>
}

/** Raise a critical alert for `service` with this title; returns once it's an incident. */
export async function raiseIncident(adminAccount: Account, service: string, title: string) {
  const am = await request.newContext()
  const res = await am.post(`${ALERTMANAGER}/api/v2/alerts`, {
    data: [
      {
        labels: {
          alertname: 'ServiceHighErrorRatio',
          severity: 'critical',
          pack: 'service',
          entity_type: 'service',
          service_name: service,
        },
        annotations: { summary: title, value: '1' },
      },
    ],
  })
  if (!res.ok()) throw new Error(`alertmanager: ${res.status()}`)
  await am.dispose()

  const { ctx } = await admin(adminAccount)
  for (let i = 0; i < 60; i++) {
    const list = await (await ctx.get(`/api/v1/incidents?service=${service}`)).json()
    if (list.length > 0) return list[0] as { id: string; ref: string; title: string }
    await new Promise((r) => setTimeout(r, 2_000))
  }
  throw new Error(`no incident for ${service} after 120 s`)
}
