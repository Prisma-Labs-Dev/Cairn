# Status

This fork adds dependency-based workflows and generic file artifacts above Cairn's existing
single-goal exploration engine. Upstream base: `oritera/Cairn` at `8e7e0ea`.
Changes to the original engine are recorded in [Core changes](docs/core-changes.md). The
[server protocol](docs/specs/server-protocol.md) and
[dispatcher specification](docs/specs/dispatcher-design.md) are maintained in English.

## What works

- Chains, parallel branches, joins, and stage grouping by layer label.
- Separate goals and fact graphs for stages, with optional earlier findings and artifact provenance.
- Reference-only handoffs and selective historical reads, without copying the full prior context.
- Goal-only, artifact-only, and combined stages.
- Required output collection before stage completion; missing outputs create corrective work.
- Automatic artifact recovery pauses the workflow after three unsuccessful attempts.
- Durable binary artifact downloads, hashes, sizes, and retained workflow working files.
- Workflow creation, progress, completion summaries, stop/resume, and stage graph navigation.
- Goal-only project creation, JSON/YAML workflow files, and agent-editable optional metadata.
- Explicit goal expansion into bounded generated stages, using all goals or ranked choose-one fallback.
- Unavailable options advance to the next candidate; retries create fresh attempts and retain earlier evidence.
- A [workflow authoring guide](docs/workflow-authoring.md) for chains, joins, generated goals, and ranked choices.
- Live runtime concurrency overrides, dispatcher freshness, and worker pool capacity in the UI.
- Optional independent completion review, off by default, with frozen evidence and bounded rework.
- Pi-first worker examples, genuine live JSON event activity, and scoped original-session history through a read-only pi-web viewer.
- CLI-reported token usage by model and invocation, explicit coverage, durable outbox, and reporter health.
- Workflow Pause/Resume and recoverable Trash, retaining artifacts, graphs, workspaces, usage, and reviews.

## Run

Follow the main README setup. For host-local workers, copy `dispatch.local.example.yaml` to
`dispatch.yaml`, start `uv run --project cairn cairn serve`, then run
`uv run --project cairn cairn dispatch --config dispatch.yaml` in a second terminal. Worker
CLIs must already be configured and signed in. Open `http://127.0.0.1:8000` and select
**New workflow**. Full definitions and API examples are in `docs/workflows.md`.

## Verification

The regression suite covers DAG validation, concurrent joins, pause/resume, restart,
shared workspace retention, binary artifact capture/download, missing-output recovery,
bounded default context, optional source retrieval, legacy context migration, and existing
standalone projects. Run
`uv run --project cairn --group dev pytest`.

Browser checks covered workflow creation, artifact-only stages, stage progress, output
downloads, and desktop/390px layouts. A deterministic three-stage find/produce/check demo
completed against the real local HTTP server and produced a verified downloadable file.
Regression tests use deterministic workers and local subprocesses rather than provider
APIs. Container file reading is covered by script-level tests and fake-container tests.
Runtime control behavior is documented in [Runtime controls](docs/runtime-controls.md).
[Core changes](docs/core-changes.md) links each changed original behavior to its regression
coverage. Documentation translation was checked against current models, routers, drivers,
and scheduling code; the original embedded prompt copies now link to the loaded resources.
Focused tests also cover independent review gates and leases, immutable claim evidence,
workflow Trash/restore races, structured CLI result extraction, usage replay and cumulative
resume accounting, and exclusive outbox ownership across processes. The guides
[verification](docs/verification.md), [usage](docs/usage.md), and
[workflow management](docs/workflow-management.md) describe those contracts.

Live HTTP checks exercised a pending completion claim, same-producer review rejection,
approval before workflow fan-out, Trash hiding and write rejection, and paused restoration.
These are API/state-transition checks. There has been no live Luna benchmark in this work.
Fabricated citations in evaluator tests are injected test cases, not observed model behavior.
[Benchmark setup](docs/benchmarks.md) has no pilot measurements or demonstrated workflow advantage.

## Current limits

Existing stage definitions remain fixed, but explicitly expandable stages may generate
bounded follow-up goals. Choose-one expansion tries ranked options until one succeeds, including
unavailable outcomes and fresh retries. A general conditional-expression language and nested
reusable workflows are not implemented. Layers are labels over a dependency graph. Runtime caps apply independently
per dispatcher, not across all processes sharing a server.
Artifacts use distinct output paths across the workflow, up to 10 MiB each, at most 32 declared per stage. File presence is
checked; semantic correctness still depends on the agent and explicit review stages.
The UI downloads artifacts but does not render or edit their contents. Independent verification
is another agent assessment, not proof of correctness. Token totals cover available CLI reports,
not provider-wide billing; missing counters remain unknown. Each dispatcher requires a separate
durable usage outbox path on a filesystem with advisory-lock support. Trash is recoverable and
does not permanently erase workflow data. Full live research and container/provider validation
remain separate from deterministic regression and local HTTP checks.
