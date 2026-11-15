# Local setup (macOS, Apple Silicon)

## 1. Tools

| Tool | Install | Check |
|---|---|---|
| uv (manages Python 3.12) | `brew install uv` | `uv --version` |
| Node 24+ | `brew install node` | `node --version` |
| Podman + podman-compose | `brew install podman podman-compose` | `podman --version` |
| Ollama (used from Step 15) | `brew install ollama` | `ollama --version` |

You don't need to install Python 3.12 yourself. uv downloads it on the first `uv sync`.

## 2. Podman machine

On macOS, Linux containers run inside a small VM:

```bash
podman machine init --cpus 4 --memory 8192 --disk-size 60
podman machine start
podman run --rm quay.io/podman/hello
```

The VM uses about 8 GB of your 24 GB. Stop it when you don't need containers: `podman machine stop`.

## 3. Project

```bash
git clone https://github.com/asadsheikh1/sentinel.git && cd sentinel
cp .env.example backend/.env    # optional; defaults work without it
make install
make dev                        # then: curl localhost:8000/healthz
```

## 4. Podman notes

- **Compose:** `podman compose` hands off to podman-compose. The Makefile always calls it with `-f deploy/compose/compose.yaml`.
- **Testcontainers:** `make test-integration` points `DOCKER_HOST` at the Podman machine's API socket and sets `TESTCONTAINERS_RYUK_DISABLED=true` (Ryuk's cleanup container isn't needed with Podman). To run pytest by hand:
  ```bash
  export DOCKER_HOST="unix://$(podman machine inspect --format '{{.ConnectionInfo.PodmanSocket.Path}}')"
  export TESTCONTAINERS_RYUK_DISABLED=true
  cd backend && uv run pytest -m integration
  ```
- **Images:** always use fully-qualified names (`docker.io/...`, `ghcr.io/...`). Podman doesn't assume Docker Hub.
- **Clock drift after the Mac sleeps:** the Podman VM's clock can fall behind (observed: 37 minutes). Symptoms are wrong container "created" times, TLS "certificate not yet valid" errors, and odd test timings. Check with `date -u; podman machine ssh date -u`. Fix with `podman machine stop && podman machine start`.
- **Multi-arch:** `podman build --platform linux/amd64,linux/arm64 --manifest ...`. The amd64 half is emulated on Apple Silicon, so it's slow (about 7 minutes for the API image).
