import { QueryCache, QueryClient } from '@tanstack/react-query'

import { isUnauthorised } from '@/auth/context'

export function makeQueryClient(): QueryClient {
  const client: QueryClient = new QueryClient({
    // A session that expired mid-use: forget who we were; RequireAuth sends us to /login.
    queryCache: new QueryCache({
      onError: (error) => {
        if (isUnauthorised(error)) client.setQueryData(['me'], null)
      },
    }),
    defaultOptions: {
      queries: { retry: (count, error) => !isUnauthorised(error) && count < 2 },
    },
  })
  return client
}
