# Changes to the original Cairn core

This is the single record of behavioral changes to the original Cairn engine in this fork.
The comparison baseline is `oritera/Cairn` at `8e7e0ea`. It includes changes merged since that
base, including runtime controls, CLI usage capture, and optional independent verification.
The translated specifications describe current behavior: [server protocol](specs/server-protocol.md) and
[dispatcher](specs/dispatcher-design.md).

Workflow sequencing, generated workflow goals, artifact delivery, shared workspaces, and
selective historical context are extensions above the original engine. Their design belongs
in [Staged workflows](workflows.md). Command syntax belongs in [Agent creation](agent-creation.md),
and runtime API details belong in [Runtime controls](runtime-controls.md).
[Token usage](usage.md) and [Independent verification](verification.md) describe their current
operating contracts. [Workflow management](workflow-management.md) covers recoverable workflow
Trash; it does not change the original standalone project deletion endpoint.

## Outcome-preserving worker guidance

The bootstrap and reason prompt wrapper now explicitly asks workers to keep the requested
outcome and hard constraints unchanged while adapting methods to new evidence. Suggested
methods or ingredients count as preferences only when the user has not made them
requirements. Required independent work can become parallel intents; mutually exclusive
alternatives require selection before execution. This guidance also applies to standalone
projects. It does not rewrite their goal facts, change completion verification defaults,
or authorize replacing a hard requirement.

Selected workflow alternatives additionally receive an evidence-backed `unavailable`
response contract in bootstrap and reason. Only selected choice projects can use the new
API. Other projects retain their ordinary intent/completion lifecycle. Full choice
selection, fallback, manual recovery, and history contracts belong in
[Staged workflows](workflows.md#choose-one-approach-with-ranked-fallback).

Proof is in [workflow choice tests](../cairn/tests/test_workflow_choices.py), including
local deterministic bootstrap and reason execution, and
[cancellation-drain tests](../cairn/tests/test_choice_runtime_drain.py). These fixtures do
not call live models or establish real-world ingredient availability.

## Project creation and metadata

Before, creating a standalone project required nonempty `title`, `origin`, and `goal`.
Now only a nonempty goal is required. Missing, null, or blank title derives a display title
from the goal's first nonempty line, with whitespace collapsed and at most 96 characters.
Missing, null, or blank origin becomes an empty special `origin` fact. Explicit nonempty
metadata is trimmed and retained. The goal itself is not truncated. Unknown creation fields
are now rejected rather than silently ignored.

This lets an agent start work from a goal without inventing redundant metadata. Existing
complete creation bodies still work. Creation still starts an active project, preserves
initial hints, and defaults `bootstrap_enabled` to true.

The new `PATCH /projects/{id}/metadata` can refine title and origin on an active project.
It requires at least one nonempty string, rejects null/blank updates and unknown keys, and
returns full project detail. An origin edit updates the special origin fact and appends a
small metadata hint so reasoning can notice the change even though the fact count is
unchanged. This is an explicit exception to the graph's ordinary append-only fact policy.
Completed source history is not editable through this endpoint. The original title-only
`PUT /projects/{id}/title` remains available in every project state.

The new project CLI accepts a goal shortcut or exact API bodies from JSON, YAML, or stdin.
It validates with the API models before HTTP, emits JSON results and errors, supports offline
schema and dry-run commands, and limits source input to 1 MiB. It does not change worker
selection, start a model itself, or require provider credentials for creation.

Proof is in [creation API tests](../cairn/tests/test_simple_creation.py) and
[CLI tests](../cairn/tests/test_creation_cli.py), including real local HTTP creation,
normalization, offline validation, safe transport errors, and metadata refinement.

## Live runtime policy and project admission

Before, the three concurrency limits came only from YAML loaded at startup. The worker
executor had that fixed startup size. An admitted active project retained its local admission
until it left the active state.

Now the server stores nullable overrides for `max_workers`, `max_running_projects`, and
`max_project_workers`. Each dispatcher polls them on its normal tick. Null restores that
process's loaded YAML value. The UI shows saved settings alongside configured, desired, and
last-applied limits, dispatcher freshness, actual in-flight tasks and projects, and worker
capacity. Worker environment variables and credentials are excluded from these reports.

Raising the total task cap makes additional execution slots available while earlier jobs
continue. Lowering any cap affects new dispatch and does not cancel running tasks. A lowered
project admission cap removes excess idle admissions and waits for excess in-flight projects
to drain. Previously admitted projects therefore cannot all keep receiving new work after a
reduction. Tasks still obey worker `max_running`, supported task types, known unhealthy-worker
cooldowns, and per-project capacity. Setting the total cap to three does not give a single
worker configured for one task three execution slots.

These changes make concurrency adjustable during a run without interrupting work or editing
YAML. The saved policy is shared, but every dispatcher applies it independently. It is not a
cross-process global quota. Admissions, counters, checkpoints, and cleanup remain local;
one scheduling dispatcher per server remains the predictable deployment. Stale reports are
historical observations, not proof of a currently running process.

Proof is in [runtime tests](../cairn/tests/test_runtime_controls.py) and the existing
[scheduler tests](../cairn/tests/test_scheduler_logic.py). The runtime tests cover persistence,
reset, strict override validation, freshness, secret-free snapshots, per-process scope,
raising a real executor while tasks are running, and reducing project admissions without
cancellation. Exact endpoints and bounds are in [Runtime controls](runtime-controls.md).

## Explicit Codex model routing

Before, local Codex execution always inherited the host CLI's model and effort settings.
Container Codex took `CODEX_MODEL` from worker environment and requested high effort.
Now a Codex worker can set `model` and `reasoning_effort` explicitly. Execute and resume both
receive the selection. Container health checks use the same selected model. An explicit model
takes precedence over `CODEX_MODEL`, so that environment key can be omitted when `model` is set.

Omitting the new fields preserves local host defaults and container high effort. Other
worker drivers reject these fields so an explicit selection cannot be silently ignored.
This provides reproducible model selection for tasks such as a model-specific benchmark.
It does not make every model compatible with every CLI or provider; the selected backend
must support the requested model and effort.

Proof is in [Codex routing tests](../cairn/tests/test_codex_worker_model.py) and
[configuration tests](../cairn/tests/test_config_and_adapters.py). These inspect execute/resume arguments
and health-check model selection without calling a provider.

## Structured CLI output and automatic usage capture

Before, Claude Code and Codex returned their ordinary noninteractive text output. The
baseline Pi driver already used JSON events. The fork now invokes Claude Code execute and
resume with `--output-format stream-json --verbose`, and Codex execute and resume with
`--json`. Drivers extract the final assistant result from those events before the existing
task JSON parser runs. Codex also obtains its thread ID from the structured session event.
Bootstrap, reason, and explore result contracts retain their existing meaning.

This transport change enables automatic token accounting for every worker invocation,
including conclude, independent review, retries, failed starts, cancellation, timeout, and
invalid model output. Each process attempt has one UUID. Sanitized records retain available
CLI-reported input, cache reads, uncached input, cache writes, output, model provenance,
phase, and outcome. Unknown values remain null; a reported zero remains zero. Mock workers
produce invocation records without invented token counts. Provider health probes and work
outside these worker processes are excluded.

Codex input already includes cache reads. Claude and Pi input is normalized by adding their
separate fresh, cache-read, and cache-write counts. For supported cumulative Claude resume
formats, a durable session baseline yields invocation deltas instead of recounting earlier
work or subagents. Missing baselines, unknown resumed versions, and interrupted streams have
explicit partial coverage. The Token usage page sums only available readings and shows
coverage and reporter health. These are CLI telemetry totals, not an account bill or proof
of complete provider usage.

The dispatcher writes a running marker and final record to a local SQLite outbox before
reporting to the server ledger. Final records and Claude baselines are persisted together.
Retries retain their UUID, identical replay is idempotent, and conflicting or permanently
rejected reports are retained locally for inspection. Replay is bounded to three events per
pass. Reporting failure does not replace or change an agent's task result.

This adds durable dispatcher state at `~/.local/share/cairn/usage-outbox.db`, overridable with
`CAIRN_USAGE_OUTBOX`. The Compose dispatcher now mounts its own durable data directory.
Each scheduling process requires a separate outbox path. An exclusive operating-system
advisory lock rejects another process using the same resolved path before startup probes
or worker execution. Threads reuse the owner's lock, and exit or crash releases it. Do not
remove the lock sidecar while a dispatcher is running. A hard crash cannot recover token
counters that were never persisted, and disk failure can still prevent accounting.

Proof is in [usage tests](../cairn/tests/test_usage.py) and
[startup ownership tests](../cairn/tests/test_usage_startup_guard.py), including synthetic
CLI streams, real harmless subprocesses, resume deltas, unknown-versus-zero fields,
multimodel readings, duplicate replay, and separate-process lock recovery. See
[Token usage](usage.md) for exact field meanings, supported formats, API, and limitations.
No live model benchmark or provider invoice reconciliation has established coverage beyond
those implementation and fixture checks.

## Optional independent completion verification

Before, a valid producer `/complete` request directly created the completion edge and marked
its project completed. This remains the default. Creation now accepts the strict boolean
`verification_enabled`, default false, for standalone projects and workflow stages. The
creation UI and JSON/YAML schema expose the same choice.

When enabled, `/complete` instead requires producer agent/session identities and returns a
pending `CompletionClaim`, rather than a completion `Intent`. The project stays active with
`pending_review_id`. All exploration must be concluded before submission. The claim freezes
its completion request, source facts, hints, producer identity, artifact metadata and a copy
of the artifact bytes. Further producer writes and artifact replacement are blocked while
review is pending. The graph has no goal-completion edge yet.

The dispatcher starts a fresh verifier execution, without resuming the producer session.
It uses the existing reason-capable worker pool and counts against normal worker, project,
and dispatcher limits. The same configured worker and model can perform the later review;
independence here means a new agent run and session. The server rejects matching producer
agent or session identities and requires the current review lease token. Approval requires
specific nonblank feedback and at least one observed evidence item. Identities are audit
fields, not authentication credentials.

Approval completes the stored claim atomically. For workflow stages, only that approval
unlocks successors or creates stored generated goals. Generated goals inherit their parent
stage's verification setting; a worker proposal cannot disable it. Rejection retains the
history and adds an actionable rework intent. Review execution and repeated rejection have
durable budgets of three before automatic work pauses for explicit operator resume. Stop,
Trash, and lease replacement invalidate old reviewer authority.

This separates a producer's assertion from the gate that accepts it, while preserving
ordinary completion when verification is off. It adds another assessment, not a correctness
guarantee. The reviewer must still inspect evidence and relevant tests; a nonempty evidence
field alone does not prove the result. Reviews are paginated summaries by default, with full
frozen context and immutable artifact downloads available on opening a claim.

Proof is in [verification API tests](../cairn/tests/test_verification.py) and
[review runtime tests](../cairn/tests/test_review_runtime.py), covering default behavior,
pending completion, fresh execution, self-review guards, leases, immutable evidence,
approval/rejection, concurrency, and bounded recovery. The [verification guide](verification.md)
has the current request fields and operational limits. Fabrication cases in evaluator tests
are deliberately injected fixtures. There has been no live Luna benchmark or observed Luna
fabrication in this work.

## Empty HTTP responses

The dispatcher client previously attempted JSON decoding whenever a response declared a JSON
content type, even when its body was empty. It now leaves empty responses unparsed. This
allows the new `204` runtime heartbeat response to succeed even if a server or proxy attaches
that content type. Nonempty JSON responses still use the existing decoding path.

Proof is the empty-204 regression in
[runtime client tests](../cairn/tests/test_runtime_controls.py).

## Preserved behavior and removals

The original Fact, Intent, Hint, and project reason-lease model remains. The dispatcher still
owns agent claims, heartbeats, process lifecycle, output validation, and graph writeback.
Bootstrap still attempts the whole goal and records evidence before completion. Reason still
checks completion or proposes exploration. Explore still handles one claimed intent and
records one fact. Same-session conclude remains a timeout or parsing fallback, and a hard
stop still cancels local tasks rather than continuing fallback.

Several corrections in the English specifications describe behavior that already existed at
the baseline. They are not new engine features: Pi support, local execution, multiple intents
per reason result with `max_intents`, file-based current-graph snapshots, startup-only health
checks by default, and explicit standalone reopen with external feedback.

No original task type, standalone project lifecycle endpoint, lease setting, or worker driver
has been removed. The intentional tightenings are rejection of unknown project-creation
fields and extra completion gates only when verification is explicitly enabled.
The new active-only metadata route does not replace or restrict the existing title-only route.
The ordinary exploration facts remain append-only; origin refinement is the stated management
exception. The server still relies on consumers to assess whether evidence actually proves a
goal, and the scheduler still lacks cross-process admission and worker-quota coordination.
