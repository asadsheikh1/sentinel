import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { fakeApi, me, VIEWER_PERMS } from '@/test/fakeApi'
import { renderApp } from '@/test/render'

const ADMIN = [
  'incidents:read', 'incidents:write', 'integrations:manage', 'apikeys:manage', 'users:manage',
] // prettier-ignore
const IID = '22222222-2222-2222-2222-222222222222'
const TOKEN = 'sit_abcdefgh_' + 'x'.repeat(43)
const T = '2026-11-25T10:00:00Z'

function integration(overrides: Record<string, unknown> = {}) {
  return {
    id: IID,
    name: 'web-1',
    kind: 'linux_host',
    environment: 'prod',
    config: {},
    expected_signals: ['logs', 'metrics'],
    status: 'pending',
    status_reason: null,
    created_at: T,
    ...overrides,
  }
}

function status(checklist: { key: string; label: string; done: boolean; required?: boolean }[]) {
  return {
    id: IID,
    status: 'pending',
    status_reason: null,
    expected_signals: ['metrics', 'logs'],
    signals: [],
    checklist: checklist.map((c) => ({ required: true, detail: null, at: null, ...c })),
  }
}

const SETUP = {
  integration_id: IID,
  kind: 'linux_host',
  endpoint: 'https://ingest.example.com',
  agent_version: '0.165.0',
  config_hash: 'abc',
  install_command: `curl -fsSL https://ingest.example.com/agent/install.sh -o sentinel-agent-install.sh && sudo bash sentinel-agent-install.sh --endpoint https://ingest.example.com --integration ${IID}`,
  container_command: 'curl … --mode container',
  uninstall_command: 'curl … uninstall',
  notes: ['The installer asks for the ingest token.'],
}

beforeEach(() => {
  document.cookie = 'sentinel_csrf=csrf-cookie-value; path=/'
  vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText: vi.fn(async () => {}) } })
})
afterEach(() => vi.unstubAllGlobals())

function asAdmin() {
  return fakeApi()
    .on('GET', /^\/api\/v1\/auth\/me$/, me('responder', ADMIN))
    .on('GET', /^\/api\/v1\/meta$/, {
      version: '0.1.0',
      env: 'test',
      ingest_endpoint: 'https://ingest.example.com',
    })
}

describe('catalogue and wizard', () => {
  it('offers the MVP tiles; GitHub is not available yet', async () => {
    asAdmin()
    renderApp('/integrations/new')
    const tiles = await screen.findByRole('list', { name: 'Catalogue' })
    const names = within(tiles)
      .getAllByRole('listitem')
      .map((li) => li.querySelector('p')!.textContent)
    expect(names).toEqual([
      'Linux host', 'Container host', 'Python app', 'HTTP check', 'Deployment webhook',
      'Alertmanager', 'GitHub',
    ]) // prettier-ignore
    expect(within(tiles).queryByRole('link', { name: 'GitHub' })).toBeNull()
    expect(within(tiles).getByLabelText('GitHub (not available yet)')).toBeVisible()
  })

  it('a viewer cannot add integrations', async () => {
    fakeApi()
      .on('GET', /^\/api\/v1\/auth\/me$/, me('viewer', VIEWER_PERMS))
      .on('GET', /^\/api\/v1\/integrations$/, [])
    renderApp('/integrations')
    expect(await screen.findByText(/Nothing is connected yet/)).toBeVisible()
    expect(screen.queryByRole('link', { name: 'Add integration' })).toBeNull()
  })

  it('a Linux host: configure → token shown once → install steps → the checklist ticks', async () => {
    let done = false
    const api = asAdmin()
      .on('POST', /^\/api\/v1\/integrations$/, () =>
        Response.json({ integration: integration(), ingest_token: TOKEN }, { status: 201 }),
      )
      .on('GET', /\/setup$/, SETUP)
      .on('GET', /\/status$/, () =>
        Response.json(
          status([
            { key: 'token_used', label: 'SENTINEL received data sent with the token', done },
            { key: 'host', label: 'The machine appears in the inventory', done },
          ]),
        ),
      )
    renderApp('/integrations/new/linux-host')
    await userEvent.type(await screen.findByLabelText('Name'), 'web-1')
    await userEvent.type(screen.getByLabelText(/Processes to watch/), 'nginx, postgres')
    await userEvent.type(screen.getByLabelText(/Log files to collect/), '/var/log/app.log')
    await userEvent.click(screen.getByRole('button', { name: 'Create and show the setup' }))

    // The token, once, with the warning.
    expect(await screen.findByText(/It won’t be shown again/)).toBeVisible()
    expect(screen.getByLabelText('ingest token')).toHaveTextContent(TOKEN)
    // The install command reads it from a file: never on the command line.
    const install = await screen.findByLabelText('Install command')
    expect(install).toHaveTextContent('--token-file ~/sentinel-token')
    expect(install.textContent).not.toContain(TOKEN)

    const create = api.requests.find((r) => r.method === 'POST' && r.url.endsWith('/integrations'))!
    expect(create.headers.get('X-CSRF-Token')).toBe('csrf-cookie-value')
    expect(await create.json()).toEqual({
      name: 'web-1',
      kind: 'linux_host',
      environment: 'prod',
      config: {
        watched_processes: ['nginx', 'postgres'],
        log_files: [{ path: '/var/log/app.log' }],
        journald: true,
      },
      expected_signals: ['metrics', 'logs'],
    })

    const checklist = await screen.findByRole('list', { name: 'Setup checklist' })
    const ticked = () => checklist.querySelectorAll('li[data-done="true"]').length
    expect(ticked()).toBe(0)
    done = true
    await waitFor(
      () => expect(ticked()).toBe(2),
      { timeout: 6_000 }, // the wizard polls every 3 s
    ) // prettier-ignore
    expect(screen.getByText('Everything required has arrived.')).toBeVisible()
  }, 10_000)

  it('an HTTP check creates the integration and the check; a refused URL is explained', async () => {
    const api = asAdmin()
      .on('POST', /^\/api\/v1\/integrations$/, () =>
        Response.json(
          { integration: integration({ kind: 'http_check' }), ingest_token: TOKEN },
          { status: 201 },
        ),
      )
      .on('POST', /^\/api\/v1\/checks$/, () =>
        Response.json(
          { detail: 'target resolves to a private address (10.0.0.5)' },
          { status: 422 },
        ),
      )
    renderApp('/integrations/new/http-check')
    await userEvent.type(await screen.findByLabelText('Name'), 'shop')
    const url = screen.getByLabelText('URL')
    await userEvent.clear(url)
    await userEvent.type(url, 'https://internal.example.com/health')
    await userEvent.click(screen.getByRole('button', { name: 'Create and show the setup' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('private address')

    const check = api.requests.find((r) => r.url.endsWith('/api/v1/checks'))!
    expect(await check.json()).toEqual({
      integration_id: IID,
      name: 'shop',
      kind: 'http',
      target: 'https://internal.example.com/health',
      interval_s: 60,
      timeout_s: 10,
      assertions: { expected_status: [200] },
    })
  })

  it('a deployment webhook: a scoped key, shown once, and a curl step for CI', async () => {
    const key = {
      id: 'k1',
      name: 'ci',
      prefix: 'sk_live_ab',
      scopes: ['deployments:write'],
      created_at: T,
      last_used_at: null,
      revoked_at: null,
    }
    const api = asAdmin()
      .on('POST', /^\/api\/v1\/api-keys$/, () =>
        Response.json({ api_key: key, secret: 'sk_live_secret' }, { status: 201 }),
      )
      .on('GET', /^\/api\/v1\/api-keys$/, [key])
    renderApp('/integrations/new/deployments')
    await userEvent.type(await screen.findByLabelText('Name'), 'ci')
    await userEvent.click(screen.getByRole('button', { name: 'Create and show the setup' }))
    expect(await screen.findByLabelText('API key')).toHaveTextContent('sk_live_secret')
    expect(screen.getByLabelText('Deployment event')).toHaveTextContent(
      `${window.location.origin}/api/v1/ingest/deployments`,
    )
    const post = api.requests.find((r) => r.url.endsWith('/api-keys') && r.method === 'POST')!
    expect(await post.json()).toEqual({ name: 'ci', scopes: ['deployments:write'] })
    const checklist = await screen.findByRole('list', { name: 'Setup checklist' })
    expect(checklist.querySelectorAll('li[data-done="false"]')).toHaveLength(1)
  })
})

describe('integrations and inventory', () => {
  it('health tiles count by status, stale ones are flagged, SENTINEL’s prober is hidden', async () => {
    asAdmin().on('GET', /^\/api\/v1\/integrations$/, [
      integration({ id: 'a', name: 'web-1', status: 'healthy' }),
      integration({
        id: 'b',
        name: 'web-2',
        status: 'stale',
        status_reason: 'No data for 5 minutes',
      }),
      integration({ id: 'c', name: 'SENTINEL prober', kind: 'prober', status: 'healthy' }),
    ])
    renderApp('/integrations')
    await waitFor(() => expect(screen.getByTestId('tile-healthy')).toHaveTextContent('1'))
    expect(screen.getByTestId('tile-stale')).toHaveTextContent('1')
    const attention = screen.getByRole('region', { name: 'Needs attention' })
    expect(attention).toHaveTextContent('web-2')
    expect(attention).toHaveTextContent('No data for 5 minutes')
    expect(screen.queryByText('SENTINEL prober')).toBeNull()
  })

  it('rotating a token shows the new one once', async () => {
    const api = asAdmin()
      .on('GET', /^\/api\/v1\/integrations$/, [integration({ status: 'healthy' })])
      .on('GET', /\/status$/, status([]))
      .on('GET', /\/setup$/, SETUP)
      .on('GET', /^\/api\/v1\/entities$/, [])
      .on('POST', /\/tokens\/rotate$/, { ingest_token: 'sit_rotated_' + 'y'.repeat(44) })
    renderApp(`/integrations/${IID}`)
    await userEvent.click(await screen.findByRole('button', { name: 'Rotate token' }))
    expect(await screen.findByLabelText('new ingest token')).toHaveTextContent('sit_rotated_')
    expect(api.requests.find((r) => r.url.endsWith('/rotate'))!.headers.get('X-CSRF-Token')).toBe(
      'csrf-cookie-value',
    )
  })

  it('a viewer sees the checklist but no management', async () => {
    fakeApi()
      .on('GET', /^\/api\/v1\/auth\/me$/, me('viewer', VIEWER_PERMS))
      .on('GET', /^\/api\/v1\/integrations$/, [integration({ status: 'healthy' })])
      .on(
        'GET',
        /\/status$/,
        status([{ key: 'host', label: 'The machine appears in the inventory', done: true }]),
      )
      .on('GET', /^\/api\/v1\/entities$/, [])
    renderApp(`/integrations/${IID}`)
    expect(await screen.findByText('Only an admin can change integrations.')).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Rotate token' })).toBeNull()
    expect(await screen.findByText(/The machine appears in the inventory/)).toBeVisible()
  })

  it('the inventory shows entities, who reports them, and their relations', async () => {
    const host = {
      id: 'h',
      type: 'host',
      key: 'web-1',
      display_name: 'web-1',
      attributes: {},
      integration_ids: [IID],
      first_seen: T,
      last_seen: new Date().toISOString(),
    }
    const svc = {
      ...host,
      id: 's',
      type: 'service',
      key: 'shop/orders@prod',
      display_name: 'orders',
    }
    fakeApi()
      .on('GET', /^\/api\/v1\/auth\/me$/, me('viewer', VIEWER_PERMS))
      .on('GET', /^\/api\/v1\/integrations$/, [integration({ name: 'web-1 agent' })])
      .on('GET', /^\/api\/v1\/entities$/, [host, svc])
      .on('GET', /^\/api\/v1\/entities\/s\/relations$/, [
        {
          from_entity_id: 's',
          to_entity_id: 'h',
          relation: 'runs_on',
          source: 'inferred',
          confidence: 1,
          last_seen: T,
        },
      ])
    renderApp('/inventory')
    const list = await screen.findByRole('list', { name: 'Entities' })
    expect(within(list).getAllByText('web-1 agent')).toHaveLength(2)
    const row = within(list).getByText('orders').closest('li')!
    await userEvent.click(within(row).getByRole('button', { name: 'Details' }))
    expect(await within(row).findByText('runs on web-1')).toBeVisible()
  })
})
