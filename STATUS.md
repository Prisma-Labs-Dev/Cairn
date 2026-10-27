# Status

This fork adds dependency-based workflows and generic file artifacts above Cairn's existing
single-goal exploration engine. Upstream base: `oritera/Cairn` at `8e7e0ea`.

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
No live model calls or real Docker execution were used for verification. Container file
reading is covered by script-level tests and the existing fake-container tests.

## Current limits

Definitions are fixed at creation. Nested reusable workflows, conditional branches, and
runtime-generated stages are not implemented. Layers are labels over a dependency graph.
Artifacts use distinct output paths across the workflow, up to 10 MiB each, at most 32 declared per stage. File presence is
checked; semantic correctness still depends on the agent and explicit review stages.
The UI downloads artifacts but does not render or edit their contents.
