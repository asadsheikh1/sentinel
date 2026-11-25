import { defineConfig, devices } from '@playwright/test'

// Needs the running stack (`make stack-up-fast`) and the web UI (`make ui`).
export default defineConfig({
  testDir: './e2e',
  timeout: 150_000,
  expect: { timeout: 45_000 },
  workers: 1, // one stack, shared users and alerts
  reporter: [['list']],
  use: {
    baseURL: process.env.SENTINEL_WEB_URL ?? 'http://127.0.0.1:3000',
    trace: 'retain-on-failure',
  },
  projects: [
    {
      name: 'chromium',
      // Locally, the installed Chrome (no browser download); CI installs Chromium.
      use: { ...devices['Desktop Chrome'], ...(process.env.CI ? {} : { channel: 'chrome' }) },
    },
  ],
})
