import { useQuery } from '@tanstack/react-query'

import { api, unwrap } from '@/api/client'

export function useMeta() {
  return useQuery({
    queryKey: ['meta'],
    queryFn: async () => unwrap(await api.GET('/api/v1/meta')),
    staleTime: Infinity,
  })
}

export function useIntegrations(refetchMs = 10_000) {
  return useQuery({
    queryKey: ['integrations'],
    queryFn: async () => unwrap(await api.GET('/api/v1/integrations')),
    refetchInterval: refetchMs,
  })
}

/** Status and the setup checklist; polled while someone watches it tick. */
export function useIntegrationStatus(id: string, refetchMs = 10_000) {
  return useQuery({
    queryKey: ['integration', id, 'status'],
    queryFn: async () =>
      unwrap(
        await api.GET('/api/v1/integrations/{integration_id}/status', {
          params: { path: { integration_id: id } },
        }),
      ),
    refetchInterval: refetchMs,
  })
}
