/**
 * Onboarding through the UI only (Step 14): what a new user does, with nothing
 * but the wizard's own instructions.
 *
 * - An HTTP check: configure it, and watch the first probe tick.
 * - A Linux host (needs SENTINEL_E2E_AGENT=1 and the disposable systemd machine
 *   image from `make test-agent`): create it, copy the token and the install
 *   command off the page, run them on a fresh machine, and watch every required
 *   item tick, then find the machine in the inventory.
 */
import { execFileSync } from 'node:child_process'
import { randomUUID } from 'node:crypto'

import { expect, test, type Page } from '@playwright/test'

import { accounts, type Account } from './setup.ts'

const TARGET = 'sentinel-probe-target' // allowlisted in the dev stack
const MACHINE = 'sentinel-ui-onboarding'
const MACHINE_IMAGE =
  process.env.SENTINEL_AGENT_TEST_IMAGE ?? 'localhost/sentinel-test-ubuntu:24.04'
const PACKAGE_DIR = process.env.SENTINEL_AGENT_PACKAGE_DIR
const ARCH = process.arch === 'arm64' ? 'arm64' : 'amd64'

let admin: Account

test.beforeAll(async () => {
  admin = (await accounts()).admin
})

function podman(args: string[], input?: string): string {
  return execFileSync('podman', args, { input, encoding: 'utf8', timeout: 600_000 })
}

async function signIn(page: Page) {
  await page.goto('/login')
  await page.getByLabel('Email').fill(admin.email)
  await page.getByLabel('Password').fill(admin.password)
  await page.getByRole('button', { name: 'Sign in' }).click()
  await expect(page.getByRole('heading', { name: 'Incidents' })).toBeVisible()
}

async function startWizard(page: Page, tile: string, name: string) {
  await page
    .getByRole('navigation', { name: 'Main' })
    .getByRole('link', { name: 'Integrations' })
    .click()
  await page.getByRole('link', { name: 'Add integration' }).click()
  await page.getByRole('link', { name: tile, exact: true }).click()
  await page.getByLabel('Name').fill(name)
}

function checklistItem(page: Page, label: string) {
  return page
    .getByRole('list', { name: 'Setup checklist' })
    .getByRole('listitem')
    .filter({ hasText: label })
}

test('an HTTP check, set up from the UI, shows its first probe', async ({ page }) => {
  podman(['rm', '-f', TARGET])
  podman(['run', '-d', '--name', TARGET, '--network', 'sentinel-ingest',
          'docker.io/library/caddy:2.11.9-alpine', 'caddy', 'respond', '--listen', ':8000', '--body', 'ok']) // prettier-ignore
  try {
    await signIn(page)
    await startWizard(page, 'HTTP check', `ui-check-${randomUUID().slice(0, 6)}`)
    await page.getByLabel('URL').fill(`http://${TARGET}:8000/`)
    await page.getByRole('button', { name: 'Create and show the setup' }).click()

    await expect(page.getByText('The check is saved.')).toBeVisible()
    await expect(checklistItem(page, 'A check is configured')).toHaveAttribute('data-done', 'true')
    await expect(checklistItem(page, 'The first probe ran')).toHaveAttribute('data-done', 'true', {
      timeout: 120_000,
    })
    await expect(checklistItem(page, 'The first probe ran')).toContainText(': up')
  } finally {
    podman(['rm', '-f', TARGET])
  }
})

test('a Linux host, onboarded with only the wizard’s instructions', async ({ page }) => {
  test.skip(!process.env.SENTINEL_E2E_AGENT, 'needs SENTINEL_E2E_AGENT=1 and the machine image')
  test.setTimeout(600_000)
  await signIn(page)
  await startWizard(page, 'Linux host', `ui-host-${randomUUID().slice(0, 6)}`)
  await page.getByRole('button', { name: 'Create and show the setup' }).click()

  // What the page tells the user: the token (once) and the install command.
  const token = (await page.getByLabel('ingest token').textContent())!.trim()
  const install = (await page.getByLabel('Install command', { exact: true }).textContent())!.trim()
  expect(token).toMatch(/^sit_/)
  expect(install).not.toContain(token)

  // A fresh machine on SENTINEL's ingest network.
  podman(['rm', '-f', MACHINE])
  const mounts = PACKAGE_DIR ? ['-v', `${PACKAGE_DIR}:/pkg:ro`] : []
  podman(['run', '-d', '--name', MACHINE, '--hostname', MACHINE, '--network', 'sentinel-ingest',
          '--privileged', ...mounts, MACHINE_IMAGE]) // prettier-ignore
  try {
    await expect
      .poll(() => podman(['exec', MACHINE, 'systemctl', 'is-system-running']).trim(), {
        timeout: 60_000,
      })
      .toMatch(/running|degraded/)
      .catch(() => undefined) // is-system-running exits non-zero while starting
    podman(['exec', MACHINE, 'bash', '-c',
            'apt-get update -qq && apt-get install -y -qq curl ca-certificates >/dev/null 2>&1']) // prettier-ignore

    // Step 1 of the page: save the token to a file. Step 2: run the install command.
    podman(['exec', '-i', MACHINE, 'bash', '-c', 'umask 077; cat > /root/sentinel-token'], token)
    const endpoint = install.match(/--endpoint (\S+)/)![1]!
    let command = install
      .replaceAll(endpoint, 'http://ingest.sentinel.internal:4319') // this machine's way in
      .replace('sudo ', '') // root already
    if (PACKAGE_DIR) {
      command += ` --package-file /pkg/otelcol-contrib_0.165.0_linux_${ARCH}.tar.gz --cosign-file /pkg/cosign-linux-${ARCH}`
    }
    podman(['exec', MACHINE, 'bash', '-c', `cd /root && ${command}`])

    // The checklist on the same page ticks, item by item.
    for (const label of [
      'SENTINEL received data sent with the token',
      'Metrics are arriving',
      'Logs are arriving',
      "The installer's test marker came through the log pipeline",
      'The machine appears in the inventory',
    ]) {
      await expect(checklistItem(page, label)).toHaveAttribute('data-done', 'true', {
        timeout: 180_000,
      })
    }
    await expect(page.getByText('Everything required has arrived.')).toBeVisible()
    if (process.env.SENTINEL_E2E_SHOTS) {
      // The checklist only: the page above it shows the (disposable) token.
      await page
        .getByRole('list', { name: 'Setup checklist' })
        .locator('..')
        .screenshot({ path: `${process.env.SENTINEL_E2E_SHOTS}/onboarding-linux-host.png` })
    }

    // And the machine is in the inventory.
    await page
      .getByRole('navigation', { name: 'Main' })
      .getByRole('link', { name: 'Inventory' })
      .click()
    await page.getByLabel('Search').fill(MACHINE)
    await page.getByLabel('Search').press('Enter')
    await expect(
      page.getByRole('list', { name: 'Entities' }).getByText(MACHINE, { exact: true }),
    ).toBeVisible()
  } finally {
    podman(['rm', '-f', MACHINE])
  }
})
