import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { readCookie } from '@/api/client'
import { describe as describeEvent } from '@/components/incidents/labels'
import { detail, event, fakeApi, incident, me, RESPONDER_PERMS, VIEWER_PERMS } from '@/test/fakeApi'
import { renderApp } from '@/test/render'

const ID = '11111111-1111-1111-1111-111111111111'

beforeEach(() => {
  document.cookie = 'sentinel_csrf=csrf-cookie-value; path=/'
})

afterEach(() => {
  vi.unstubAllGlobals()
  document.cookie = 'sentinel_csrf=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/'
})

function signedIn(role: 'viewer' | 'responder') {
  return fakeApi()
    .on(
      'GET',
      /^\/api\/v1\/auth\/me$/,
      me(role, role === 'viewer' ? VIEWER_PERMS : RESPONDER_PERMS),
    )
    .on('GET', /^\/api\/v1\/incidents$/, [incident()])
    .on('GET', /^\/api\/v1\/incidents\/[^/]+$/, detail())
    .on('GET', /^\/api\/v1\/incidents\/[^/]+\/timeline$/, [
      event(1, 'opened', { severity: 'SEV3', title: 'orders: 100% of requests fail' }),
      event(2, 'severity_changed', { from: 'SEV3', to: 'SEV1' }),
    ])
}

describe('signing in', () => {
  it('sends a signed-out visitor to the login page, then back where they were going', async () => {
    let signedInNow = false
    const api = fakeApi()
      .on('GET', /^\/api\/v1\/auth\/me$/, () =>
        signedInNow
          ? Response.json(me('viewer', VIEWER_PERMS))
          : Response.json({ detail: 'Sign in' }, { status: 401 }),
      )
      .on('POST', /^\/api\/v1\/auth\/login$/, () => {
        signedInNow = true
        return Response.json(me('viewer', VIEWER_PERMS))
      })
      .on('GET', /^\/api\/v1\/incidents\/[^/]+$/, detail())
      .on('GET', /^\/api\/v1\/incidents\/[^/]+\/timeline$/, [])
    const { router } = renderApp(`/incidents/${ID}`)

    expect(await screen.findByRole('heading', { name: 'Sign in to SENTINEL' })).toBeVisible()
    await userEvent.type(screen.getByLabelText('Email'), 'viewer@example.com')
    await userEvent.type(screen.getByLabelText('Password'), 'correct horse')
    await userEvent.click(screen.getByRole('button', { name: 'Sign in' }))

    expect(await screen.findByRole('heading', { name: /100% of requests fail/ })).toBeVisible()
    expect(router.state.location.pathname).toBe(`/incidents/${ID}`)
    const login = api.requests.find((r) => r.url.endsWith('/api/v1/auth/login'))!
    expect(await login.json()).toEqual({ email: 'viewer@example.com', password: 'correct horse' })
  })

  it('says only that the combination is wrong (no user enumeration)', async () => {
    fakeApi()
      .on('GET', /^\/api\/v1\/auth\/me$/, () => Response.json({}, { status: 401 }))
      .on('POST', /^\/api\/v1\/auth\/login$/, () =>
        Response.json({ detail: 'Invalid email or password.' }, { status: 401 }),
      )
    renderApp('/login')
    await userEvent.type(await screen.findByLabelText('Email'), 'nobody@example.com')
    await userEvent.type(screen.getByLabelText('Password'), 'wrong')
    await userEvent.click(screen.getByRole('button', { name: 'Sign in' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'That email and password combination is not valid.',
    )
  })

  it('signs out with the CSRF token, and forgets the user', async () => {
    const api = signedIn('viewer').on(
      'POST',
      /^\/api\/v1\/auth\/logout$/,
      () => new Response(null, { status: 204 }),
    )
    renderApp('/incidents')
    await userEvent.click(await screen.findByRole('button', { name: 'Sign out' }))
    expect(await screen.findByRole('heading', { name: 'Sign in to SENTINEL' })).toBeVisible()
    const logout = api.requests.find((r) => r.url.endsWith('/logout'))!
    expect(logout.headers.get('X-CSRF-Token')).toBe('csrf-cookie-value')
  })
})

describe('incidents list', () => {
  it('shows active incidents by default, newest first', async () => {
    const api = signedIn('viewer')
    renderApp('/incidents')
    const list = await screen.findByRole('list', { name: 'Incidents' })
    expect(within(list).getByText('INC-7')).toBeVisible()
    expect(within(list).getByText('SEV1')).toBeVisible()
    const url = new URL(api.requests.find((r) => r.url.includes('/api/v1/incidents?'))!.url)
    expect(url.searchParams.getAll('status')).toEqual(['open', 'investigating', 'mitigated'])
    expect(api.requests.every((r) => r.method === 'GET' || r.url.endsWith('/logout'))).toBe(true)
  })

  it('filters by severity through the URL', async () => {
    const api = signedIn('viewer')
    const { router } = renderApp('/incidents')
    await screen.findByRole('list', { name: 'Incidents' })
    await userEvent.selectOptions(screen.getByLabelText('Severity'), 'SEV2')
    await waitFor(() => expect(router.state.location.search).toBe('?severity=SEV2'))
    await waitFor(() =>
      expect(api.requests.some((r) => new URL(r.url).searchParams.get('severity') === 'SEV2')).toBe(
        true,
      ),
    )
  })
})

describe('incident detail', () => {
  it('shows signals, entities, the deployment and the timeline', async () => {
    signedIn('viewer')
    renderApp(`/incidents/${ID}`)
    expect(await screen.findByRole('heading', { name: /100% of requests fail/ })).toBeVisible()
    expect(screen.getByText('Opened the incident')).toBeVisible()
    expect(screen.getByText('aviro/orders@dev', { exact: false })).toBeVisible()
    expect(screen.getByText('scenario/S01-bad')).toBeVisible()
    const timeline = await screen.findByRole('list', { name: 'Timeline' })
    expect(within(timeline).getByText('Severity SEV3 → SEV1')).toBeVisible()
  })

  it('a viewer gets no way to change the incident', async () => {
    signedIn('viewer')
    renderApp(`/incidents/${ID}`)
    expect(
      await screen.findByText('Your role can view incidents but not change them.'),
    ).toBeVisible()
    expect(screen.queryByLabelText('Status')).toBeNull()
    expect(screen.queryByLabelText('Severity')).toBeNull()
    expect(screen.queryByRole('form', { name: 'Add a comment' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Update status' })).toBeNull()
  })

  it('a responder changes the status with the CSRF token, offered only allowed moves', async () => {
    const api = signedIn('responder').on('PATCH', /^\/api\/v1\/incidents\/[^/]+$/, () =>
      Response.json(incident({ status: 'investigating' })),
    )
    renderApp(`/incidents/${ID}`)
    const status = await screen.findByLabelText('Status')
    const options = within(status)
      .getAllByRole('option')
      .map((o) => o.textContent)
    expect(options).toEqual([
      'Move to…',
      'Investigating',
      'Mitigated',
      'Resolved',
      'False positive',
    ])

    await userEvent.selectOptions(status, 'investigating')
    await userEvent.type(screen.getByLabelText('Note (optional)'), 'on it')
    await userEvent.click(screen.getByRole('button', { name: 'Update status' }))

    await waitFor(() => expect(api.requests.some((r) => r.method === 'PATCH')).toBe(true))
    const patch = api.requests.find((r) => r.method === 'PATCH')!
    expect(patch.headers.get('X-CSRF-Token')).toBe('csrf-cookie-value')
    expect(await patch.json()).toEqual({ status: 'investigating', note: 'on it' })
  })

  it("shows the server's refusal", async () => {
    signedIn('responder').on('PATCH', /^\/api\/v1\/incidents\/[^/]+$/, () =>
      Response.json({ detail: "An incident can't go from open to closed." }, { status: 409 }),
    )
    renderApp(`/incidents/${ID}`)
    await userEvent.selectOptions(await screen.findByLabelText('Status'), 'resolved')
    await userEvent.click(screen.getByRole('button', { name: 'Update status' }))
    expect(await screen.findByRole('alert')).toHaveTextContent("can't go from open to closed")
  })

  it('a responder can comment', async () => {
    const api = signedIn('responder').on('POST', /^\/api\/v1\/incidents\/[^/]+\/comments$/, () =>
      Response.json(event(9, 'comment', { text: 'rolled back' }), { status: 201 }),
    )
    renderApp(`/incidents/${ID}`)
    await userEvent.type(await screen.findByLabelText('Comment'), 'rolled back')
    await userEvent.click(screen.getByRole('button', { name: 'Comment' }))
    await waitFor(() => expect(api.requests.some((r) => r.url.endsWith('/comments'))).toBe(true))
    const post = api.requests.find((r) => r.url.endsWith('/comments'))!
    expect(await post.json()).toEqual({ text: 'rolled back' })
  })
})

describe('untrusted content renders as text (stored XSS)', () => {
  const PAYLOADS = [
    '<img src=x onerror="window.__xss=1">',
    '<script>window.__xss=1</script>',
    '<a href="javascript:window.__xss=1">click</a>',
    '"><svg onload="window.__xss=1">',
  ]

  it('in titles, signals, entities, deployments and comments', async () => {
    const [img, script, link, svg] = PAYLOADS
    fakeApi()
      .on('GET', /^\/api\/v1\/auth\/me$/, me('responder', RESPONDER_PERMS))
      .on('GET', /^\/api\/v1\/incidents\/[^/]+$/, () => {
        const d = detail({ title: img, service_name: svg })
        d.signals[0]!.title = script!
        d.entities[0]!.display_name = link!
        d.suspected_deployment!.version = img!
        return Response.json(d)
      })
      .on('GET', /^\/api\/v1\/incidents\/[^/]+\/timeline$/, [
        event(1, 'opened', { severity: 'SEV1', title: script }),
        event(2, 'comment', { text: img }, 'user:someone'),
        event(3, 'status_changed', { from: 'open', to: 'mitigated', note: svg }),
      ])
    const { container } = renderApp(`/incidents/${ID}`)

    expect(await screen.findByRole('heading', { name: img })).toBeVisible()
    expect(screen.getAllByText(script!).length).toBeGreaterThan(0)
    expect(screen.getAllByText(img!).length).toBeGreaterThanOrEqual(2)
    expect(screen.getByText(link!)).toBeVisible()
    expect(container.querySelector('img, script, svg[onload], a[href^="javascript"]')).toBeNull()
    expect((window as { __xss?: number }).__xss).toBeUndefined()
  })

  it('in the list', async () => {
    fakeApi()
      .on('GET', /^\/api\/v1\/auth\/me$/, me('viewer', VIEWER_PERMS))
      .on('GET', /^\/api\/v1\/incidents$/, [
        incident({ title: PAYLOADS[0], service_name: PAYLOADS[3] }),
      ])
    const { container } = renderApp('/incidents')
    expect(await screen.findByText(PAYLOADS[0]!)).toBeVisible()
    expect(container.querySelector('img, script, svg')).toBeNull()
    expect((window as { __xss?: number }).__xss).toBeUndefined()
  })
})

describe('helpers', () => {
  it('reads the CSRF cookie', () => {
    expect(readCookie('sentinel_csrf', 'a=1; sentinel_csrf=abc%3D; b=2')).toBe('abc=')
    expect(readCookie('sentinel_csrf', 'a=1')).toBeNull()
  })

  it('describes every timeline event as plain text', () => {
    const e = (type: string, data: Record<string, unknown>) => describeEvent(event(1, type, data))
    expect(e('signal_attached', { reason: 'related_entity', title: 'gateway 5xx' })).toBe(
      'Signal attached (Related in the topology): gateway 5xx',
    )
    expect(e('status_changed', { from: 'open', to: 'mitigated', note: 'quiet' })).toBe(
      'Status Open → Mitigated: quiet',
    )
    expect(e('severity_changed', { from: 'SEV1', to: 'SEV4', manual: true })).toBe(
      'Severity SEV1 → SEV4 (set by hand)',
    )
    expect(e('deployment', { service: 'orders', version: 'v2', commit_sha: 'abcdef0123456' })).toBe(
      'Suspected deployment: orders v2 (abcdef01)',
    )
    expect(e('investigation_queued', {})).toBe('Investigation queued')
    expect(e('signal_attached', { reason: 'first_signal', title: 'orders 5xx' })).toBe(
      'First signal: orders 5xx',
    )
    expect(e('hypothesis_added', {})).toBe('hypothesis added')
  })
})
