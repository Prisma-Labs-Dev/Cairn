<div align="center">

<img src="./README/banner.png" alt="Cairn Banner"/>

# Cairn
### General state-space search, first tested in penetration testing

<p>
  <a href="https://zc.tencent.com/hackathon" target="_blank" rel="noopener noreferrer">
    <img src="./README/tencent.png" alt="Tencent Security Testing" height="55" />
  </a>
  <a href="https://zc.tencent.com/hackathon" target="_blank" rel="noopener noreferrer">
    <img src="./README/tch.png" alt="TCH" height="55" />
  </a>
  <a href="https://wiki.chainreactors.red" target="_blank" rel="noopener noreferrer">
    <img src="./README/c.png" alt="ChainReactors" height="45" />
  </a>
</p>

Cairn is a general-purpose problem-solving engine. <br/>Given an origin and a goal, it searches for a path through an unknown state space. <br/>This fork adds staged workflows above the exploration engine. AI Penetration Testing is the upstream project's first validated domain.

<p>
  <a href="https://discord.gg/nDSy4NZVP" target="_blank" rel="noopener noreferrer">
    <img src="https://img.shields.io/badge/Discord-5865F2?style=flat-square&logo=discord&logoColor=white" alt="Discord" />
  </a>
  <a href="https://x.com/le1xia0" target="_blank" rel="noopener noreferrer">
    <img src="https://img.shields.io/badge/X-000000?style=flat-square&logo=x&logoColor=white" alt="X" />
  </a>
</p>

</div>

## Staged workflows in this fork

Chain several goals into a workflow, or run independent research stages in parallel and
join their results. A stage starts when all its prerequisites satisfy their declared completion gates. Each stage
keeps its own Cairn fact graph; later stages can inspect earlier findings on demand and share
the workflow's working files. Earlier findings are not copied into every stage's prompt.
A stage can require a goal, specified output files, or both. Required artifacts
are collected before the next stage unlocks. The UI shows goals, progress, completion
explanations, and artifact downloads. Typed results and server-evaluated conditions can
make later work conditional. Stages that do not apply retain their reasons, while named
collections expose results and artifacts from bounded generated work.

See [Staged workflows](docs/workflows.md) for the creation flow, a research-to-manuscript
example, API usage, file retention, and current limits. Existing single-project runs still work.
For agents, start with [Writing workflows](docs/workflow-authoring.md) to choose the right
dependencies, alternatives, and recovery behavior. [JSON/YAML creation](docs/agent-creation.md) supports goal-only projects and
stage-only workflow definitions. Titles and starting context are optional.
Expandable stages can discover new goals for Cairn to execute before later stages continue.
[Runtime controls](docs/runtime-controls.md) expose concurrency limits, connected dispatchers,
and worker capacity in the UI without editing YAML.
Optional [independent verification](docs/verification.md) requires a fresh agent review before
accepting completion. It is off by default. The separate [Token usage](docs/usage.md) page
shows CLI-reported counters and their coverage. Workflow [Pause, Resume, and recoverable
Trash](docs/workflow-management.md) preserve results and history.

## Documentation

- [Writing workflows for agents](docs/workflow-authoring.md) is the self-contained starting
  point for goals, constraints, parallel requirements, ranked choices, examples, and validation.
- [Core changes](docs/core-changes.md) records changes to the original engine against upstream
  `8e7e0ea`, separate from the workflow extensions.
- [Server protocol](docs/specs/server-protocol.md) and [dispatcher design](docs/specs/dispatcher-design.md)
  describe current behavior in English.
- [Workflow and artifact guide](docs/workflows.md), [agent creation](docs/agent-creation.md),
  and [runtime controls](docs/runtime-controls.md) cover the fork's user-facing additions.
- [Pi sessions and live activity](docs/pi-sessions.md) explains scoped history, the pinned read-only viewer, and exact model selection.
- [Independent verification](docs/verification.md), [token usage](docs/usage.md), and
  [workflow management](docs/workflow-management.md) document completion review, accounting,
  and recoverable deletion.
- [Benchmark procedure](docs/benchmarks.md) defines evaluation setup; no live pilot results
  are available yet.
- [Worker container](container/README.md) explains the image and its environment instructions.
- [Current status and limits](STATUS.md) records verification and outstanding boundaries.

<p align="center">
  <a href="https://www.bilibili.com/video/BV1a8R5BhEVi/" target="_blank" rel="noopener noreferrer">
    <img src="./README/cairn.png" alt="Cairn runtime screenshot" width="900" />
  </a>
</p>

## What is Cairn?

Penetration testing is fundamentally a **directed search through a near-infinite state space**:

- **Origin**: known (target IP, target system)
- **Goal**: defined (get a shell, capture the flag)
- **Path**: unknown

This structure is not unique to penetration testing. Vulnerability research, mathematical proof, CTF challenges — any problem with a clear starting point, a clear success condition, and an unknown path in between shares the same shape.

Cairn is built for this class of problems. Penetration testing is the first domain it has been validated on.

The engine is built on a **Blackboard Architecture** with an explicit fact-intent graph. Three primitives are all it needs:

| Concept | Meaning |
|---------|---------|
| **Fact** | A confirmed, objective finding written to the board |
| **Intent** | A declared direction of exploration, not yet executed |
| **Hint** | Human judgment injected at any time; absorbed by agents on the next read |

The graph grows from `origin` toward `goal`. Every new Fact is a stepping stone; every Intent is a step into the unknown.

Agent workers perform the tasks chosen by the dispatcher from the current graph: observe findings, decide on exploration, and record new facts. Workers have no fixed roles. Their supported task types, priority, and concurrency come from configuration. Reason and explore read the current project graph; earlier workflow stages are optional historical context.

Agents coordinate through the shared board. The dispatcher manages task claims and process execution. Workflow stages can also share working files and inspect completed earlier stages on demand.

## Cairn in Action

https://github.com/user-attachments/assets/e557b1ac-dda4-41cb-87dd-9d56dbf05133


## How It Works

The original engine has three task types, selected from the configured worker pool:

| Task | What it does | Output |
|------|-------------|--------|
| **Bootstrap** | At project start, attempts to solve the problem directly | Fact + possible Complete |
| **Reason** | Reads the full graph: is the goal met? What should be explored next? | Complete / new Intents / no-op |
| **Explore** | Executes one intent claimed by the dispatcher and reports findings | One Fact |

With verification enabled, producer completion becomes a pending claim. A separate fresh
review uses a reason-capable worker and accepts or rejects that claim before the goal can
complete. With verification off, completion retains its original behavior.

System architecture:

```
          ┌──────────────────────────────────┐
          │           Cairn Server           │
          │    Facts + Intents + Hints       │
          └─────────────────┬────────────────┘
                            │
                     Read / Write API
                            │
          ┌─────────────────┴────────────────┐
          │             Dispatcher           │
          │   Schedules tasks, manages       │
          │   containers, writes protocol    │
          └──────────┬───────────────┬───────┘
                     │               │
     ┌───────────────┴──┐     ┌──────┴──────────────┐
     │  Worker Container│     │  Worker Container   │
     │   (Project A)    │     │   (Project B)       │
     │  ┌────┐  ┌────┐  │     │  ┌────┐  ┌────┐     │
     │  │ W. │  │ W. │  │     │  │ W. │  │ W. │     │
     │  └────┘  └────┘  │     │  └────┘  └────┘     │
     └──────────────────┘     └─────────────────────┘
```

**Cairn Server** stores the graph and enforces claims, leases, and project state. This fork also stores workflow dependencies, artifacts, runtime policy, completion reviews, and sanitized usage records.

**Cairn Dispatcher** reads the graph, schedules tasks, spins up and tears down worker containers, and is the sole writer to the protocol. Each standalone project gets its own worker container; workflow stages share a workflow container. Multiple agent workers can run concurrently inside it. Agent Workers only receive a prompt and return structured output.

Workers can also run directly on the dispatcher host instead of in per-project containers — **local mode**, no Docker required. See [Local mode](#local-mode-no-docker) below.

Pi is the primary worker in the shipped examples. Cairn captures its live JSON events and
original session files for the read-only [Pi session viewer](docs/pi-sessions.md).
Existing **Codex** and **Claude Code** drivers remain available. Claude Code and Codex
use structured CLI event output; drivers extract the final task result and record reported
usage separately. Each dispatcher must use its own durable usage outbox path, configurable
with `CAIRN_USAGE_OUTBOX`; sharing one path is rejected at startup. See [Token usage](docs/usage.md).

## Results

**Tencent Cloud Hackathon · AI Penetration Testing Challenge · 2nd Edition**

610 teams · 1,345 participants · top universities and security firms across China

| Metric | Value |
|--------|-------|
| Problems solved | **54 / 54 — only team to AK** |
| Final ranking | 3rd |

> The system had never been tested before the competition. The full pipeline came online for the first time at 4 AM on race day. No training, no tuning, no domain-specific tooling. Zero MCP tools, zero RAG, zero predefined agent roles.

## Further Reading

- <a href="https://mp.weixin.qq.com/s/DlpEH7bVr0xi0VawPJs3XA" target="_blank" rel="noopener noreferrer">The Strongest AI Penetration Testing Agent: Postmortem of the Only Team to Achieve AK at the TCH Tencent Cloud Hackathon Intelligent Penetration Testing Challenge (2nd Edition)</a>
- <a href="https://mp.weixin.qq.com/s/2rEqFLvkxvYWM3gW170C2w" target="_blank" rel="noopener noreferrer">The Pathless Path: Cairn AI from Penetration Testing to General Problem Solving</a>

## Getting Started

**Prerequisites**
 
- macOS or Linux
- Python ≥ 3.12
- Docker (container execution only — not needed for local mode)


### Pull required images
 
Container execution requires the worker image. Skip this step for host-local execution:
 
```bash
docker pull --platform=linux/amd64 ghcr.io/oritera/cairn-worker-container:latest
```

Create your local dispatcher configuration and fill in your LLM endpoints and API keys:

```bash
cp dispatch.example.yaml dispatch.yaml
```
 
### Docker Compose (recommended)
 
Pull the base image used to build Cairn:
 
```bash
docker pull ghcr.io/astral-sh/uv:python3.13-trixie
```
 
```bash
docker compose up --build
```
 
This starts `cairn-server` on port `8000` and `cairn-dispatcher` once the server passes its health check. The dispatcher mounts `dispatch.yaml` from the project root and connects to Docker via the host socket. Server data is persisted to `./datas/cairn/`. The dispatcher's usage outbox and session-accounting baselines are persisted separately under `./datas/dispatcher/`.
 
### Manual
 
```bash
# Start the server
uv run --project cairn cairn serve
 
# Run the dispatcher
uv run --project cairn cairn dispatch --config dispatch.yaml
 
# Run startup health checks only
uv run --project cairn cairn dispatch --config dispatch.yaml --startup-healthcheck-only
```

### Local mode (no Docker)

Instead of one container per project, workers can run directly on the dispatcher host, reusing the machine's already-configured `claude` / `codex` / `pi` CLIs — no Docker, and no API keys in the config.

```bash
cp dispatch.local.example.yaml dispatch.yaml

# Start the server
uv run --project cairn cairn serve

# Run the dispatcher on the same host, where the CLIs are installed and logged in
uv run --project cairn cairn dispatch --config dispatch.yaml
```

Local mode is selected by `runtime.execution: local` (see `dispatch.local.example.yaml`). On startup the dispatcher checks each configured worker CLI is installed and runnable, and reminds you they must already be logged in. Each standalone project gets a separate working directory under `local.workspace_root` (default: the dispatcher's current directory). Workflow stages share a workflow directory. Optional Pi `provider`, `model`, and `reasoning_effort` fields select an exact configured model and thinking level. Codex also accepts `model` and `reasoning_effort`. Omitting selectors preserves host defaults. Explicit local Pi models must exist exactly in its offline catalog; unavailable models fail without substitution. Run the dispatcher directly on the host — not inside Docker — since the agents run with your user's permissions and no sandbox.

### Tests

Run the fast regression suite without Docker or live model endpoints:

```bash
uv run --project cairn --group dev pytest
```

## Disclaimer

Cairn is a general-purpose problem-solving engine. Although it supports penetration testing, CTF solving, security assessment, and vulnerability research workflows, it is intended to be used only in environments where you have explicit authorization to operate.

You are solely responsible for how you use this project. Do not use Cairn against systems, networks, applications, or data without clear prior permission from the owner or operator. Unauthorized security testing, exploitation, or data access may be illegal and may cause harm.

The developers and contributors of this project do not endorse or accept responsibility for any misuse, abuse, damage, loss, or legal consequences arising from its use. By using this project, you agree to ensure that your activities comply with all applicable laws, regulations, contractual obligations, and professional or organizational policies in your jurisdiction.

## Star History

<a href="https://www.star-history.com/#oritera/Cairn&Date" target="_blank" rel="noopener noreferrer">
  <img src="https://api.star-history.com/svg?repos=oritera/Cairn&type=Date" alt="Star History Chart" />
</a>

## ⚖️ License
This project is licensed under **GNU AGPLv3** for personal and educational use.

**Commercial Use**: If you wish to use this project in a commercial or proprietary environment without the AGPL-3.0 open-source obligations, **please contact me to obtain a commercial license.**

**Contributions**: By submitting a Pull Request, you agree that your contributions may be used under both the AGPL-3.0 and the project's commercial license.
