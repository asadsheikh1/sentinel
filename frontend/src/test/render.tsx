import { QueryClientProvider } from '@tanstack/react-query'
import { render } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router'

import { AuthProvider } from '@/auth/auth'
import { makeQueryClient } from '@/queryClient'
import { routes } from '@/routes'

/** The whole app (real routes, providers and client) at ``path``, over the fake API. */
export function renderApp(path: string) {
  const router = createMemoryRouter(routes, { initialEntries: [path] })
  const client = makeQueryClient()
  client.setDefaultOptions({ queries: { retry: false, refetchInterval: false } })
  const view = render(
    <QueryClientProvider client={client}>
      <AuthProvider>
        <RouterProvider router={router} />
      </AuthProvider>
    </QueryClientProvider>,
  )
  return { ...view, router }
}
