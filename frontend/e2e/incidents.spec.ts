import { randomUUID } from 'node:crypto'

import { expect, test, type Page } from '@playwright/test'

import { accounts, raiseIncident, type Account } from './setup.ts'

let users: Awaited<ReturnType<typeof accounts>>

test.beforeAll(async () => {
  users = await accounts()
})

async function signIn(page: Page, account: Account) {
  await page.goto('/')
  await expect(page).toHaveURL(/\/login$/) // signed out: sent to sign in
  await page.getByLabel('Email').fill(account.email)
  await page.getByLabel('Password').fill(account.password)
  await page.getByRole('button', { name: 'Sign in' }).click()
  await expect(page.getByRole('heading', { name: 'Incidents' })).toBeVisible()
}

test('a viewer watches an incident appear, opens it, and cannot change it', async ({ page }) => {
  await signIn(page, users.viewer)

  // The incident is raised while the list is open: it must appear without a reload.
  const service = `checkout-${randomUUID().slice(0, 6)}`
  const title = `${service}: 100% of requests fail`
  const raised = await raiseIncident(users.admin, service, title)
  const row = page.getByRole('link', { name: new RegExp(title) })
  await expect(row).toBeVisible()
  await expect(row).toContainText(raised.ref)

  await row.click()
  await expect(page).toHaveURL(new RegExp(`/incidents/${raised.id}$`))
  await expect(page.getByRole('heading', { name: title })).toBeVisible()
  await expect(page.getByRole('cell', { name: 'Opened the incident' })).toBeVisible()
  await expect(page.getByRole('list', { name: 'Timeline' })).toContainText('Incident opened at SEV')

  // Acceptance: a viewer can't change status. No control in the UI…
  await expect(page.getByText('Your role can view incidents but not change them.')).toBeVisible()
  await expect(page.getByLabel('Status', { exact: true })).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Update status' })).toHaveCount(0)
  await expect(page.getByLabel('Comment', { exact: true })).toHaveCount(0)

  // …and the API refuses it too, with the viewer's own session and CSRF token.
  const csrf = (await page.context().cookies()).find((c) => c.name === 'sentinel_csrf')!.value
  const forced = await page.request.patch(`/api/v1/incidents/${raised.id}`, {
    data: { status: 'resolved' },
    headers: { 'X-CSRF-Token': csrf },
  })
  expect(forced.status()).toBe(403)
  await page.reload()
  await expect(page.getByText('Open', { exact: true }).first()).toBeVisible()
})

test('a responder moves the incident on and comments; the timeline records both', async ({
  page,
}) => {
  const service = `payments-${randomUUID().slice(0, 6)}`
  const raised = await raiseIncident(users.admin, service, `${service}: timeouts`)
  await signIn(page, users.responder)
  await page.goto(`/incidents/${raised.id}`)

  await page.getByLabel('Status', { exact: true }).selectOption('investigating')
  await page.getByLabel('Note (optional)').fill('Looking at the payments dependency')
  await page.getByRole('button', { name: 'Update status' }).click()
  const timeline = page.getByRole('list', { name: 'Timeline' })
  await expect(timeline).toContainText(
    'Status Open → Investigating: Looking at the payments dependency',
  )

  await page.getByLabel('Comment', { exact: true }).fill('Rolled back to the previous release.')
  await page.getByRole('button', { name: 'Comment' }).click()
  await expect(timeline).toContainText('Rolled back to the previous release.')
  await expect(timeline.getByText('You ·').first()).toBeVisible()
})

test('an alert carrying markup renders inert (stored XSS, under the real CSP)', async ({
  page,
}) => {
  const service = `xss-${randomUUID().slice(0, 6)}`
  const payload = `<img src=x onerror="window.__xss=1;alert(1)"><script>window.__xss=2</script>`
  const raised = await raiseIncident(users.admin, service, `${service} ${payload}`)

  const dialogs: string[] = []
  page.on('dialog', (d) => {
    dialogs.push(d.message())
    void d.dismiss()
  })
  await signIn(page, users.viewer)
  const response = await page.goto(`/incidents/${raised.id}`)
  expect(response!.headers()['content-security-policy']).toContain("script-src 'self'")

  await expect(page.getByRole('heading', { name: `${service} ${payload}` })).toBeVisible()
  await expect(page.getByRole('list', { name: 'Timeline' })).toContainText(payload)
  expect(await page.locator('main img, main script').count()).toBe(0)
  expect(await page.evaluate(() => (globalThis as { __xss?: number }).__xss)).toBeUndefined()
  expect(dialogs).toEqual([])
})
