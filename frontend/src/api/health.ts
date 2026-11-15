// Hand-written until the OpenAPI-generated client arrives (Step 13).
export type Health = { status: 'ok'; version: string; env: string }

export async function fetchHealth(signal?: AbortSignal): Promise<Health> {
  const res = await fetch('/healthz', { signal })
  if (!res.ok) throw new Error(`API returned ${res.status}`)
  return (await res.json()) as Health
}
