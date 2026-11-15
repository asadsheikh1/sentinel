import { render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import App from './App'

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('App', () => {
  it('shows the API version when /healthz is healthy', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => Response.json({ status: 'ok', version: '0.1.0', env: 'test' })),
    )
    render(<App />)
    expect(await screen.findByText(/Healthy — version 0\.1\.0 \(test\)/)).toBeInTheDocument()
  })

  it('shows an error when the API is down', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response('', { status: 503 })),
    )
    render(<App />)
    expect(await screen.findByRole('alert')).toHaveTextContent('API returned 503')
  })
})
