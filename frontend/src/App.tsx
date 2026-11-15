import { useEffect, useState } from 'react'

import { fetchHealth, type Health } from './api/health'

type State =
  { kind: 'loading' } | { kind: 'ok'; health: Health } | { kind: 'error'; message: string }

export default function App() {
  const [state, setState] = useState<State>({ kind: 'loading' })

  useEffect(() => {
    const ctrl = new AbortController()
    fetchHealth(ctrl.signal)
      .then((health) => setState({ kind: 'ok', health }))
      .catch((err: unknown) => {
        if (ctrl.signal.aborted) return
        setState({ kind: 'error', message: err instanceof Error ? err.message : String(err) })
      })
    return () => ctrl.abort()
  }, [])

  return (
    <main className="shell">
      <h1>SENTINEL</h1>
      <p className="tagline">Autonomous AI Site Reliability Engineer</p>
      <section className="card" aria-live="polite">
        <h2>API status</h2>
        {state.kind === 'loading' && <p>Checking…</p>}
        {state.kind === 'ok' && (
          <p className="ok">
            Healthy — version {state.health.version} ({state.health.env})
          </p>
        )}
        {state.kind === 'error' && (
          <p className="error" role="alert">
            Unreachable: {state.message}
          </p>
        )}
      </section>
    </main>
  )
}
