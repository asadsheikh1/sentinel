import type { AgentSetup } from '@/api/client'

import { CopyBlock } from './parts'

/** Install steps for a Linux or container host. The token never goes on a command line. */
export function AgentInstructions({ setup }: { setup: AgentSetup }) {
  return (
    <div className="space-y-4 text-sm">
      <ol className="list-decimal space-y-4 pl-5">
        <li className="space-y-2">
          <p>On the machine, save the token to a file only you can read:</p>
          <CopyBlock
            label="Save the token"
            text={'umask 077; cat > ~/sentinel-token   # paste the token, then press Ctrl-D'}
          />
        </li>
        <li className="space-y-2">
          <p>
            Run the installer. It downloads agent {setup.agent_version}, checks its checksum and
            signature, and starts it as a service:
          </p>
          <CopyBlock
            label="Install command"
            text={`${setup.install_command} --token-file ~/sentinel-token`}
          />
          <p className="text-xs text-muted">
            Or run it as a container instead (Podman or Docker):{' '}
            <code className="font-mono break-all">{setup.container_command}</code>
          </p>
        </li>
        <li>Watch the checklist below: it ticks as data arrives, usually within a minute.</li>
      </ol>
      <ul className="list-disc space-y-1 pl-5 text-xs text-muted">
        {setup.notes.map((note) => (
          <li key={note}>{note}</li>
        ))}
      </ul>
      <details className="text-xs text-muted">
        <summary className="cursor-pointer">Uninstall</summary>
        <CopyBlock label="Uninstall command" text={setup.uninstall_command} />
      </details>
    </div>
  )
}

export function AppInstructions({
  endpoint,
  token,
  service,
}: {
  endpoint: string
  token?: string
  service: string
}) {
  const secret = token ?? '<the ingest token>'
  return (
    <div className="space-y-3 text-sm">
      <p>Install OpenTelemetry’s auto-instrumentation in the app’s environment:</p>
      <CopyBlock
        label="Install instrumentation"
        text={
          'pip install opentelemetry-distro opentelemetry-exporter-otlp\nopentelemetry-bootstrap -a install'
        }
      />
      <p>Then start the app with these settings (they send traces, logs and metrics here):</p>
      <CopyBlock
        label="App settings"
        text={[
          `export OTEL_SERVICE_NAME=${service}`,
          `export OTEL_EXPORTER_OTLP_ENDPOINT=${endpoint}`,
          'export OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf',
          `export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Bearer ${secret}"`,
          'export OTEL_PYTHON_LOGGING_AUTO_INSTRUMENTATION_ENABLED=true',
          'opentelemetry-instrument python app.py   # your usual start command',
        ].join('\n')}
      />
      <p className="text-xs text-muted">
        Keep the token in a secret store or a file the app reads, not in source control. If an agent
        already runs on the same machine, point the app at the agent instead (
        <code className="font-mono">http://127.0.0.1:4318</code>, no token needed).
      </p>
    </div>
  )
}

export function DeploymentInstructions({ origin, secret }: { origin: string; secret?: string }) {
  const key = secret ?? '$SENTINEL_DEPLOY_KEY'
  return (
    <div className="space-y-3 text-sm">
      <p>Add one step after each deploy in your CI or deploy script:</p>
      <CopyBlock
        label="Deployment event"
        text={[
          `curl -fsS -X POST ${origin}/api/v1/ingest/deployments \\`,
          `  -H "X-Sentinel-Key: ${key}" \\`,
          '  -H "Idempotency-Key: deploy-$GIT_SHA-$SERVICE-$RUN_ID" \\',
          "  -H 'Content-Type: application/json' \\",
          `  -d '{"service": "'$SERVICE'", "version": "'$VERSION'", "commit_sha": "'$GIT_SHA'", "environment": "prod"}'`,
        ].join('\n')}
      />
      <p className="text-xs text-muted">
        Store the key as a CI secret. The same Idempotency-Key makes retries safe.
      </p>
    </div>
  )
}

export function AlertmanagerInstructions({ origin, secret }: { origin: string; secret?: string }) {
  return (
    <div className="space-y-3 text-sm">
      <p>
        Save the key to a file Alertmanager can read (for example{' '}
        <code className="font-mono">/etc/alertmanager/sentinel-key</code>), then add a receiver:
      </p>
      <CopyBlock
        label="Alertmanager receiver"
        text={[
          'receivers:',
          '  - name: sentinel',
          '    webhook_configs:',
          `      - url: ${origin}/api/v1/ingest/alertmanager`,
          '        send_resolved: true',
          '        http_config:',
          '          http_headers:',
          '            X-Sentinel-Key:',
          '              files: [/etc/alertmanager/sentinel-key]',
        ].join('\n')}
      />
      {secret && <p className="text-xs text-muted">The key goes in that file, on its own line.</p>}
      <p className="text-xs text-muted">
        Route the alerts you want to the <code className="font-mono">sentinel</code> receiver. Label
        them with <code className="font-mono">severity</code> and the entity they’re about (
        <code className="font-mono">service_name</code>,{' '}
        <code className="font-mono">host_name</code>
        …) so SENTINEL can link them.
      </p>
    </div>
  )
}
