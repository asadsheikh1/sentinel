# SENTINEL docs — START HERE

You do **not** need to read everything. This page tells you what SENTINEL is, what to read and in what order, and what you need to do next.

---

## 1. SENTINEL in 30 seconds

SENTINEL is an AI SRE assistant. It:

1. **Connects** to your systems (servers, containers, apps, websites, GitHub).
2. **Detects** problems such as errors, slowness, a full disk, or a crashed process.
3. **Investigates** using a free AI model running on your Mac, and explains the likely cause **with evidence you can click**.
4. **Suggests fixes** (rollback, GitHub issue, code patch). It **never acts without your approval**.

There are no paid AI APIs. It runs locally on your M4 and is deployed cheaply on AWS.

---

## 2. Reading order

Folders are numbered in reading order, and so are the files inside them.

| Order | Folder | What's inside | Do you need to read it? |
|---|---|---|---|
| 1 | `1-product/` | **What** we're building | ✅ Skim `1-prd.md` (10 min) |
| 2 | `2-architecture/` | **How** it works | Optional. Look at the diagrams in `1-system-architecture.md` and `2-integrations.md` |
| 3 | `3-quality/` | How we prove the AI works (measured, not claimed) | Optional |
| 4 | `4-plan/` | **When and in what order** we build it, costs, risks | ✅ Read `1-roadmap.md` (milestone table at the top is enough) |
| — | `adr/` | Short "why we chose X" notes | ❌ Reference only, for interviews or later |

**Minimum reading (about 20 minutes):** this page → [PRD](1-product/1-prd.md) → the first table of the [Roadmap](4-plan/1-roadmap.md).

### Full file list

| # | File | One-line purpose |
|---|---|---|
| 1.1 | [1-product/1-prd.md](1-product/1-prd.md) | Goals, users, scope, success metrics |
| 1.2 | [1-product/2-features-moscow.md](1-product/2-features-moscow.md) | Every feature, marked Must, Should, Could, or Won't |
| 2.1 | [2-architecture/1-system-architecture.md](2-architecture/1-system-architecture.md) | Components and workflow diagrams |
| 2.2 | [2-architecture/2-integrations.md](2-architecture/2-integrations.md) | How SENTINEL connects to servers, apps, clouds, and GitHub |
| 2.3 | [2-architecture/3-ai-agents.md](2-architecture/3-ai-agents.md) | The 3 AI agents and the local model choice |
| 2.4 | [2-architecture/4-database.md](2-architecture/4-database.md) | Tables |
| 2.5 | [2-architecture/5-api.md](2-architecture/5-api.md) | Endpoints |
| 2.6 | [2-architecture/6-security.md](2-architecture/6-security.md) | Threats and protections |
| 3.1 | [3-quality/1-testing-and-evaluation.md](3-quality/1-testing-and-evaluation.md) | Tests, AI test scenarios, metrics |
| 4.1 | [4-plan/1-roadmap.md](4-plan/1-roadmap.md) | **The 35 build steps** |
| 4.2 | [4-plan/2-aws-and-costs.md](4-plan/2-aws-and-costs.md) | AWS setup and monthly cost |
| 4.3 | [4-plan/3-tech-stack.md](4-plan/3-tech-stack.md) | Every technology and why |
| 4.4 | [4-plan/4-risks-and-licences.md](4-plan/4-risks-and-licences.md) | What could go wrong, licence warnings |
| 4.5 | [4-plan/5-repository-structure.md](4-plan/5-repository-structure.md) | Folder layout and CI pipelines |

---

## 3. How the build works

The build has 35 small steps in 7 milestones. After **every** step I stop, show you what changed and how to test it, and wait for your OK.

| Milestone | Steps | What you'll have at the end |
|---|---|---|
| M1 Foundations | 1–3 | Project skeleton, database, login |
| M2 Telemetry & integrations | 4–9 | A demo shop app, data collection, a server agent, website checks |
| M3 Detection | 10–14 | Incidents created automatically, a web UI, an "Add integration" screen |
| M4 AI core | 15–19 | **v0.1:** the AI explains incidents with evidence |
| M5 Code & memory | 20–23 | Finds the guilty commit, remembers past incidents, writes reports |
| M6 Response & extras | 24–31 | **v0.2** after step 24: approvals. Steps 25–31 are optional extras (AWS connector, code repair) |
| M7 Production | 32–35 | **v1.0:** live on AWS with published results |

---

## 4. What YOU need to do now

### Step A — Answer these decisions ✅ Done
**All recommendations approved on 2026-11-15.** Each recommendation below is now the decision.

| # | Question | My recommendation |
|---|---|---|
| D1 | Put the demo shop **AVIRO** in its own GitHub repo (`aviro-shop`)? | Yes |
| D2 | Default AI model | **Qwen3 8B** (decided 2026-11-25 after the Step 15 benchmark: 14B swaps next to the stack on the M4; [benchmark](benchmarks/models-m4.md)). Revisit with the Step 26 quality evaluation |
| D3 | AI on AWS (no GPU) | Small model live, plus labelled examples computed on your Mac |
| D4 | AWS region, and always-on or scheduled? | Your region choice. Scheduled (~$35–45/mo) until public, then always-on (~$135–180/mo) |
| D5 | OK to use Loki for logs (AGPL licence, used unmodified)? | Yes |
| D6 | GitHub access method | Fine-grained personal tokens (one read, one write) |
| D7 | Container runtime | ✅ **Decided: Podman** (no Docker). Used on your Mac, in CI, and on AWS |
| D8 | Public repo with an Apache-2.0 licence? | Yes |
| D9 | Allow self-approval on the public demo? | Only with a visible "demo mode" banner |
| D10 | Keep code repair and the extra integrations optional (steps 25–31)? | Yes |
| D11 | One organisation per installation (no public sign-up)? | Yes |
| D12 | Second small AWS server to act as an "external" monitored machine? | Yes, scheduled (~$3–5/mo) |
| D13 | First integrations: Linux + container (Podman/Docker) agent, Python apps, website checks, deploy and GitHub webhooks | Yes |
| D14 | Use the OpenTelemetry Collector as the agent (no custom agent, never runs commands)? | Yes |
| D15 | Agent supports Ubuntu/Debian and Amazon Linux first? | Yes |

### Step B — Install these on your Mac (before step 1) ✅ Done
Verified on 2026-11-15:
- Podman 6.2.0: the machine is running (libkrun, 4 CPU / 8 GiB / 60 GiB) and the hello container ran.
- podman-compose 1.6.0.
- uv 0.13.8.
- Ollama 0.40.16: installed. The Ollama server isn't started yet; that happens in step 15.

Repos (private for now, Apache-2.0):
- https://github.com/asadsheikh1/sentinel
- https://github.com/asadsheikh1/aviro-shop

1. **Homebrew**, if you don't already have it: https://brew.sh
2. **uv** (manages Python 3.12): `brew install uv`
3. **Podman** (container runtime): `brew install podman podman-compose`, then `podman machine init --cpus 4 --memory 8192 --disk-size 60` and `podman machine start`. On a Mac, Podman runs a small Linux VM in the background. That's unavoidable for Linux containers, but there is no Docker Desktop app.
4. **Ollama** (local AI): `brew install ollama`. Models are downloaded in step 15, not now.
5. **A GitHub account** with an empty repo for SENTINEL (and a second one for the demo app if D1 = yes).

AWS is **not needed until step 33**.

### Step C — Tell me "start Step 1"
I'll build only step 1, test it, report back, and wait.
