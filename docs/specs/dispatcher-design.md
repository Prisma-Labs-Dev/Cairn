# Dispatcher

This is the current dispatcher specification for this fork. The [core change record](../core-changes.md)
compares it with upstream `8e7e0ea`. Workflow and artifact behavior is documented separately in
[Staged workflows](../workflows.md).

## Responsibilities

The dispatcher executes Cairn projects. It manages execution environments, assigns explicit
tasks to agents, and writes validated results to the server. Agents receive prompts and return
structured results. They do not claim intents, send heartbeats, or write to the Cairn API.
With independent verification enabled, the dispatcher also starts a fresh completion reviewer
using the existing reason-capable worker pool.

The server stores the graph and enforces protocol consistency. The dispatcher decides what
runs next. Worker drivers invoke Claude Code, Codex, Pi, or deterministic mock processes.
A worker represents a unit of model concurrency quota. Splitting one API key into several
workers does not create independent quota.

```text
Cairn server
  Projects / Facts / Intents / Hints / reason leases
          ^
          | structured scheduling API + graph export
          v
Dispatcher
  Scheduling / worker selection / claims / heartbeats
  Sessions / timeouts / validation / protocol writes
          |
          +--> execution environment A --> agent processes
          +--> execution environment B --> agent processes
```

Container execution creates one container per standalone project. Local execution creates a
working directory and runs processes on the dispatcher host. Workflow stages share their
workflow's environment, while each stage retains its own project graph.

The dispatcher reads structured project endpoints for scheduling and writeback. It reads
`GET /projects/{project_id}/export?format=yaml` to prepare the current graph for agents.
Reason and explore prompts point to a file containing that graph. Bootstrap receives origin,
goal, and current hints instead. Optional earlier-stage context is available through a helper;
it is not copied into the current graph or required to be read.

## Task contracts

The three exploration task types use the same worker pool. Optional completion review is
a separate fresh execution, not a fourth YAML worker capability. Output examples below are the model's result
body after the driver extracts it from any CLI event envelope. The dispatcher parses JSON and
validates the task contract before writing to the server. Drivers may use structured CLI output
for transport and accounting; CLI events are not graph facts.

A worker can decline a task with `{"accepted":false}`. This produces no fact. A normal result
uses `{"accepted":true,"data":{...}}`. The parser also accepts recognized bare task payloads;
the explicit envelope remains the documented form.

| Task | Inputs | Result | Timeout behavior |
| --- | --- | --- | --- |
| `bootstrap` | Origin, goal, hints | Main phase: fact and completion. Conclude phase: fact only | Same-session conclude after timeout or unusable output when a session is available |
| `reason` | Current graph, legal fact IDs, open intents, maximum new intents | Completion, a list of new intents, or no-op while exploration remains open | One phase; timeout produces no graph write |
| `explore` | Current graph, one claimed intent ID and description | One factual conclusion | Same-session conclude after timeout or unusable output when supported |

### Bootstrap

Bootstrap is a direct attempt to solve a new project before splitting it into smaller
explorations. A project is initial when it has only `origin` and `goal` facts and no ordinary
intents. It may have no intents or one reserved open bootstrap intent.

The dispatcher uses bootstrap when it is enabled and a configured worker supports it. It
continues an existing reserved bootstrap attempt if one exists. Otherwise it can use reason,
including when `bootstrap_enabled` is false or no worker supports bootstrap.

The reserved intent is an ordinary server intent with these dispatcher conventions:

```json
{"from":["origin"],"description":"bootstrap","creator":"dispatcher.bootstrap","worker":null}
```

Only one bootstrap task runs per project. The dispatcher tries to keep just one open reserved
bootstrap intent. It claims the intent by heartbeat before launching the worker and renews
that claim during execution.

Bootstrap gets `{origin}`, `{goal}`, and `{hints}`. Hints are encoded as a JSON array. It does
not need the ordinary graph YAML. Its main phase returns only after solving the goal, with a
fact containing the evidence and a completion explaining why the evidence meets the goal:

```json
{
  "accepted": true,
  "data": {
    "fact": {"description": "Found the required result; evidence is in /tmp/proofs/result.txt."},
    "complete": {"description": "The verified result satisfies the goal."}
  }
}
```

The dispatcher first concludes the reserved intent to write the fact, then submits completion
using that new fact as the source. With verification off, the project completes immediately.
With verification on, it creates a pending claim for independent review. If completion
submission fails, the fact remains available for a later reason task.

If the main phase times out or its result cannot be parsed or validated, a driver with a
usable session can resume it with `bootstrap_conclude`. That prompt asks the worker to stop
advancing, avoid waiting for unfinished work, and summarize only confirmed facts:

```json
{"accepted":true,"data":{"fact":{"description":"Confirmed findings from the attempt so far."}}}
```

Conclude does not complete the project. The validator tolerates an extra `complete` in a
bootstrap conclude payload but ignores it. A successful conclude makes the project non-initial
because it now has a new fact. If neither phase yields a usable fact, the
dispatcher releases the intent. It may attempt bootstrap again on a later tick.

| API operation | Use |
| --- | --- |
| `POST /projects/{id}/intents` | Create the reserved intent if missing |
| `POST /projects/{id}/intents/{iid}/heartbeat` | Claim before launch and renew during execution |
| `POST /projects/{id}/intents/{iid}/conclude` | Write the confirmed fact |
| `POST /projects/{id}/complete` | Complete only after a successful main-phase fact and completion |
| `POST /projects/{id}/intents/{iid}/release` | Abandon an unsuccessful attempt |

### Reason

Reason evaluates the whole current project. It can determine that the goal is met, propose
new directions, or leave already-running exploration alone.

Reason requires an active project, no unclaimed intent, and no existing project-level reason
lease. With no checkpoint, it first runs when there are no open intents. Later runs require
one of these changes since the last successful reason checkpoint:

- The fact count increased.
- The hint count increased.
- The project moved from having open intents to having none.

An intent being created, failing execution, losing its heartbeat, or being released does not
by itself trigger another reason task. Existing projects with open intents receive a baseline
checkpoint when first observed, so a later new fact or hint can trigger reason. This applies
to projects present at startup and projects first seen later.

Reason can run alongside explore if there is a new fact or hint, there are no unclaimed
intents, and the project has spare task capacity. One project has at most one reason lease;
several projects can reason concurrently.

Inputs are `{graph_yaml}`, `{fact_ids}`, `{open_intents}`, and `{max_intents}`. The fact ID
list excludes `goal`. Open intents include both claimed and unclaimed unfinished intents.
These prompt aids do not replace server validation.

Completion:

```json
{"accepted":true,"data":{"complete":{"from":["f008"],"description":"The evidence in f008 meets the goal."}}}
```

New exploration directions:

```json
{
  "accepted": true,
  "data": {
    "intents": [
      {"from":["f003"],"description":"Check the remaining hypothesis."},
      {"from":["f002","f004"],"description":"Test the relationship between these findings."}
    ]
  }
}
```

No-op while other exploration remains open:

```json
{"accepted":true,"data":{}}
```

A completion needs `from` and `description` and cannot coexist with new intents. Each new
intent also needs `from` and `description`. The parser accepts the singular `intent` form
as well as `intents`. It validates proposals and uses at most `tasks.reason.max_intents` of
them. If no open intents remain, reason must complete or propose at least one new intent.
An empty result is allowed only while some exploration remains open.

Before launch, the dispatcher claims `POST /projects/{id}/reason/claim`. It renews the lease
through `/reason/heartbeat` while the worker runs. For a completion it calls `/complete` with
the worker's configured name. For proposals it creates unclaimed intents with that name as
`creator`. A no-op writes no fact or intent. It releases its reason lease in cleanup while the
project remains active and it still owns the lease. Stop and completion clear the lease on
the server.

Timeouts, rejections, nonzero command exits, invalid JSON, and invalid payloads normally
produce no graph write. An inactive-project `403` during writeback is treated as a state
change, not an immediate retry. Failed intent writes are logged; a conflicting proposal does
not prevent the dispatcher from attempting the remaining proposals. Explicitly expandable
workflow stages add proposal validation and bounded recovery described in
[Staged workflows](../workflows.md).

### Explore

Explore executes one active project's currently claimable unfinished intent. The dispatcher
selects an intent, claims it through heartbeat, and only then starts the worker. Inputs are
`{graph_yaml}`, `{intent_id}`, and `{intent_description}`.

```json
{"accepted":true,"data":{"description":"Testing ruled out the proposed hypothesis; the recorded measurements show no matching result."}}
```

`data.description` must be a nonempty factual conclusion. An unsuccessful hypothesis is still
a useful fact if it records what was actually tested. A refusal is represented by
`accepted:false`, not by inventing an exploration fact.

| API operation | Use |
| --- | --- |
| `POST /projects/{id}/intents/{iid}/heartbeat` | Claim before launch and renew during execution |
| `POST /projects/{id}/intents/{iid}/conclude` | Atomically append the fact and conclude the intent |
| `POST /projects/{id}/intents/{iid}/release` | Return an abandoned attempt to the unclaimed state |

A valid main-phase result is concluded immediately. It needs no separate release after a
successful conclude. A failed write is logged and the dispatcher attempts to release its claim.

Explore supports a single execution phase or an execution phase followed by same-session
conclude. The second phase is a fallback, not a mandatory step. It is used after timeout or
failure to parse or validate the main-phase output, provided the driver supports conclude
and a session ID is available. The first process is terminated, heartbeats continue, and the
same session receives a prompt to stop exploring and summarize only completed work.

If conclude times out, returns invalid output, declines, or fails to write its fact, the
attempt produces no graph data and the dispatcher releases the intent. A single-phase worker
uses that same failure path directly. A nonzero main command exit or explicit rejection does
not trigger conclude. Cancellation due to stop, completion, deletion, or lease loss also
prevents fallback from continuing work.

## Independent completion review

Verification is off by default. When enabled on a project, producer completion creates a
pending claim with frozen evidence. The dispatcher supplies a unique producer agent/run and
session identity outside model output. It does not continue ordinary bootstrap, reason, or
explore while `pending_review_id` is set.

After the producer task exits, a reason-capable worker gets a fresh execute call, not a resume
of the producer session. A single configured worker can produce and review sequentially. The
reviewer receives the frozen claim, has access to immutable artifact downloads and workspace
files, and returns `decision`, specific `feedback`, and `evidence`. Approval requires observed
evidence and atomically completes the stored claim. Rejection creates corrective exploration.
It does not trust the producer summary as proof or change deliverables during review.

The server rejects producer agent/session reuse and stale review lease tokens. The dispatcher
heartbeats its review lease, records an extracted CLI session when available, and releases the
lease in cleanup. Review execution uses the reason timeout and normal task-time health policy.
It does not use a conclude fallback. All review attempts count against the same worker, project,
and dispatcher task limits and appear as `review_execute` usage. Durable three-attempt execution
and three-rejection budgets pause automatic work for explicit resume. See
[Independent verification](../verification.md) for identities, immutable evidence, retries,
and pause behavior. This is another assessment and does not guarantee correctness.

## Scheduling and concurrency

Each tick reaps finished work, polls saved runtime overrides, reads project summaries,
reconciles admissions, cancels inactive work, queues environment cleanup, dispatches available
tasks, and publishes a runtime snapshot. The dispatcher prioritizes admitted projects with
immediately dispatchable work. It rotates project ordering, then admits other active projects
while slots remain. A project without a runnable worker does not gain a slot merely because
its status is active.

Within a project, a pending completion claim takes priority after existing producer work has
exited. Otherwise, the order is initial bootstrap or reason, then reason for a new situation,
then exploration of unclaimed intents. A claimed initial bootstrap blocks ordinary reason
and explore for that initial project. All task types count toward the same concurrency limits.

| Limit | Scope |
| --- | --- |
| `runtime.max_workers` | All in-flight tasks in this dispatcher |
| `runtime.max_running_projects` | Projects admitted by this dispatcher, including projects with draining in-flight tasks |
| `runtime.max_project_workers` | In-flight bootstrap, reason, explore, and optional review tasks in one project |
| `workers[].max_running` | Tasks assigned to one configured worker |

Saved runtime overrides apply independently to every connected dispatcher. They are not a
shared cross-process task budget. Admission, worker counters, cooldowns, reason checkpoints,
and environment cleanup remain local to each process. Use one scheduling dispatcher per
server for predictable execution; lease checks do not provide a distributed scheduler.

YAML supplies startup defaults. The [runtime controls API](../runtime-controls.md) can override
the three runtime caps without restarting. Increasing the global task cap grows execution
capacity. Decreasing a cap does not cancel existing work. When a project cap falls below the
number of in-flight projects, new dispatch waits for enough projects to drain. Excess idle
admissions are removed so new work does not continue across all previously admitted projects.

`runtime.interval` intentionally controls both scheduler ticks and task heartbeat cadence.
Choose server intent and reason timeouts that exceed the heartbeat interval. Startup validates
this relationship. These are separate from worker execution and health-check timeouts.

## Worker selection and health

Selection filters workers by supported task type, free `max_running` capacity, and local
unhealthy-worker cooldown. It prefers the lowest `priority`, then the fewest running tasks,
then randomly chooses among ties. A successful claim precedes task launch.

Health behavior follows `runtime.worker_healthcheck`:

- `startup_only`, the default, checks workers at dispatcher startup.
- `startup_and_task` also checks before each main task.
- `disabled` skips automatic model API checks.

Container-mode health checks run in the dispatcher process using HTTP, not container-side
curl. They test the configured model endpoint with a small request. HTTP 2xx is considered
healthy; the dispatcher does not inspect the response body. Non-2xx responses, network errors,
and timeout mark a worker unhealthy for a short retry window. Configured HTTP proxy variables
are passed to these checks. A model health check may consume provider quota.

Conclude fallback does not repeat the task health check. Local execution instead probes each
configured CLI with `--help` at startup and uses its existing login. A usable binary is not
proof that its login or chosen model will work. Local mode does not need provider keys in
`dispatch.yaml`.

## Worker drivers

Each driver implements command construction, health checks, session extraction, and result
extraction. The shared interface includes `check_health`, `describe_health`, `prepare_session`,
`build_execute`, `extract_session`, `extract_response_text`, `build_conclude`, and
`supports_conclude`. All bundled drivers support conclude. Resuming still requires a usable
session from the first phase.

| Driver | Container configuration | Session behavior |
| --- | --- | --- |
| `claudecode` | `ANTHROPIC_MODEL`, `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN` | Pre-generated UUID passed with `--session-id`; conclude uses `-r` |
| `codex` | `CODEX_BASE_URL`, `OPENAI_API_KEY`, plus `model` or `CODEX_MODEL` | Extracts the CLI session ID; conclude uses `codex exec resume` |
| `pi` | `PI_MODEL`, `PI_BASE_URL`, `PI_API_KEY`, `PI_PROVIDER_API` | JSON session event; conclude resumes that session |
| `mock` | Optional `MOCK_*` simulation configuration | Synthetic session and deterministic or randomized fixtures |

Claude Code health checks use `{ANTHROPIC_BASE_URL}/v1/messages`. Codex checks use
`{CODEX_BASE_URL}/responses`, matching its Responses provider configuration. Pi chooses
`/v1/messages`, `/responses`, or `/chat/completions` from `PI_PROVIDER_API`.

Claude Code execute and resume use `--output-format stream-json --verbose`. The driver
extracts the final result text from CLI events. Codex execute and resume use `--json`; its
driver extracts the final `agent_message` and thread ID. Pi already uses JSON events. The
task parser receives the extracted assistant text, not the surrounding telemetry events.

Claude Code runs noninteractively with `--dangerously-skip-permissions`. Codex uses
`--dangerously-bypass-approvals-and-sandbox`. In local mode these run with the host user's
permissions, so the dispatcher must run on the machine where the CLIs are installed and
configured. A container isolates its environment but the supplied network mode and Linux
capabilities still determine what it can access.

Codex workers may set `model` and `reasoning_effort` explicitly. The model is trimmed and
must not be blank. Allowed efforts are `minimal`, `low`, `medium`, `high`, and `xhigh`.
These fields currently require `type: codex`; other drivers reject them. For local Codex,
omitted fields preserve host defaults. For container Codex, `model` overrides `CODEX_MODEL`
and an omitted effort keeps the existing `high` setting. Both execute and resume use the
same configured overrides. The selected CLI and provider must support the requested model
and effort; configuration acceptance does not prove provider availability.

Pi container execution writes an isolated provider configuration under `/tmp/cairn-pi`, then
selects its configured model and JSON mode. `PI_MODEL_CONTEXT_WINDOW` is an optional positive
integer. Local Pi reuses host model settings. Both modes disable external extensions, skills,
prompt templates, themes, and context files and enable the bundled file and shell tools.

### Mock behavior

`runtime.prompt_group: mock` uses JSON prompts. Reason includes `phase`, `fact_ids`,
`open_intents`, and `max_intents`; explore includes `phase` and `intent_id`; bootstrap
includes `phase`, `origin`, `goal`, and `hints`.

A mock phase is configured by one JSON environment variable:

```yaml
env:
  MOCK_REASON: '{"delay":[0.05,0.3],"outcomes":{"intent":1.0}}'
```

| Phase | Variable | Available outcomes |
| --- | --- | --- |
| `healthcheck` | `MOCK_HEALTHCHECK` | `ok`, `fail` |
| `bootstrap` | `MOCK_BOOTSTRAP` | `complete`, `fact`, `rejected`, `invalid_json`, `invalid_payload`, `command_fail` |
| `bootstrap_conclude` | `MOCK_BOOTSTRAP_CONCLUDE` | `fact`, `rejected`, `invalid_json`, `invalid_payload`, `command_fail` |
| `reason` | `MOCK_REASON` | `complete`, `intent`, `noop`, `rejected`, `invalid_json`, `invalid_payload`, `command_fail` |
| `explore_execute` | `MOCK_EXPLORE_EXECUTE` | `fact`, `rejected`, `invalid_json`, `invalid_payload`, `command_fail` |
| `explore_conclude` | `MOCK_EXPLORE_CONCLUDE` | `fact`, `rejected`, `invalid_json`, `invalid_payload`, `command_fail` |

`delay` is a two-element nonnegative seconds range with maximum at least minimum. Outcome
probabilities are between zero and one and must sum exactly to one. Unsupported phases and
outcomes are rejected. A simulated delay beyond the outer task timeout exercises the timeout
path. Reason does not sample no-op when no open exploration remains. Optional fixture rules
can force outcomes based on fact-count thresholds or whether open intents are empty;
[the configuration model](../../cairn/src/cairn/dispatcher/config.py) defines their exact keys.

## Usage capture and durable dispatcher state

Each worker process attempt receives a UUID and a persisted running marker before execution.
The dispatcher records available CLI-reported usage after execution, including main phases,
conclude, review, invalid output, timeout, cancellation, failed starts, and retries. It keeps
actual zeros separate from unknown null counters. Model names retain reported/configured/unknown
provenance. Provider health checks and work outside these processes are not included.

Normalized records go through a local SQLite outbox to the server ledger. Claude cumulative
resume accounting uses durable session baselines; unsupported or missing baselines have partial
coverage. The [Token usage guide](../usage.md) defines the cache accounting, CLI format limits,
pagination, reporter health, and retry semantics. The dashboard is not a provider invoice.

The default outbox is `~/.local/share/cairn/usage-outbox.db`. Use `CAIRN_USAGE_OUTBOX` for an
alternate durable path. Every dispatcher requires a separate resolved path on a filesystem
supporting advisory locks. Ownership is checked before startup health probes and worker
execution. A competing process fails configuration rather than recovering another live
process's running records. Threads in the owner process share the lock; exit or crash releases
it. Do not delete the `.lock` sidecar while that dispatcher runs. Compose mounts this state
under its own `./datas/dispatcher/` directory, separate from server storage.

Usage-reporting failure is logged and does not replace an agent result. The durable queue
retries at most three events per pass, preserves UUIDs, and retains rejected records locally.
A hard crash cannot recover CLI counters that were never persisted. Missing or stale reporter
health leaves transfer coverage uncertain.

## Configuration reference

The dispatcher reads one YAML file. Prompts ship as package resources; commands and session
handling live in drivers. The checked-in [container example](../../dispatch.example.yaml)
and [local example](../../dispatch.local.example.yaml) are the runnable configuration references.
`common_env` is merged into each worker's environment before `worker.env` overrides it. Local
processes also inherit the dispatcher's host environment.

| Field | Requirement and meaning |
| --- | --- |
| `server` | Required server base URL |
| `runtime.execution` | `container` by default, or `local` |
| `runtime.max_workers` | Required positive total task cap per dispatcher |
| `runtime.max_running_projects` | Required positive project admission cap per dispatcher |
| `runtime.max_project_workers` | Required positive per-project task cap; YAML value cannot exceed `max_workers` |
| `runtime.interval` | Required positive scheduler and heartbeat interval in seconds |
| `runtime.healthcheck_timeout` | Required positive health-check timeout in seconds |
| `runtime.worker_healthcheck` | `startup_only` by default; also `startup_and_task` or `disabled` |
| `runtime.prompt_group` | Required packaged prompt directory name |
| `runtime.history_server_url` | Optional worker-reachable HTTP(S) base URL for historical context; see [workflows](../workflows.md) |
| `container.image` | Required in container mode |
| `container.network_mode` | Required in container mode |
| `container.completed_action` | Required in container mode: `stop` retains completed standalone containers; `remove` deletes them |
| `container.cap_add` | Optional Linux capabilities list, empty by default |
| `local.workspace_root` | Optional local root; defaults to dispatcher startup directory |
| `local.completed_action` | Local standalone directory cleanup: `keep` by default, or `remove` |
| `tasks.bootstrap.timeout` | Required positive main bootstrap timeout in seconds |
| `tasks.bootstrap.conclude_timeout` | Required positive bootstrap conclude timeout |
| `tasks.reason.timeout` | Required positive reason timeout |
| `tasks.reason.max_intents` | Positive maximum new intents per reason result, default `3` |
| `tasks.explore.timeout` | Required positive main explore timeout |
| `tasks.explore.conclude_timeout` | Required positive explore conclude timeout |
| `workers[].name` | Required unique worker identifier, used as protocol creator or worker |
| `workers[].type` | Required `claudecode`, `codex`, `pi`, or `mock` |
| `workers[].task_types` | Required nonempty unique list of `bootstrap`, `reason`, or `explore` |
| `workers[].max_running` | Required positive worker task cap |
| `workers[].priority` | Required nonnegative selection priority; lower wins |
| `workers[].env` | Optional string environment map; container drivers require the keys listed above |
| `workers[].model` | Optional nonblank explicit Codex model |
| `workers[].reasoning_effort` | Optional explicit Codex effort |

Loading validates the configuration, unique worker names, task types, environment
requirements, mock distributions, and packaged prompt resources. Every required prompt must
exist. Default reason prompts contain `{graph_yaml}`, `{fact_ids}`, `{open_intents}`, and
`{max_intents}`. Default explore and explore conclude prompts contain `{graph_yaml}`,
`{intent_id}`, and `{intent_description}`. Bootstrap prompts contain `{origin}`, `{goal}`,
and `{hints}`. Mock prompts need the same logical inputs but omit graph YAML.

Before dispatch, the driver must exist, the worker must support the task, and all effective
limits and claim requirements must be satisfied. Task-time health checks run only in the
configured mode. Parse failures, rejection, unsuccessful writeback, and lease loss are logged.
The dispatcher does not immediately repeat failed protocol writes as a separate retry loop.

## Stop, reopen, deletion, and cleanup

A non-active project is a hard stop for new dispatch. On the next observation, the dispatcher
cancels its local task processes. Bootstrap and explore do not enter conclude after
cancellation. It then stops the corresponding standalone container. Deleted standalone
projects are treated as deleted rather than stopped, and their managed orphan containers
are removed. Completed standalone containers follow `completed_action`.

Cleanup runs asynchronously and may proceed across several completed projects without
blocking scheduling. A reopened standalone project follows ordinary active-project scheduling,
but waits for any already-queued cleanup of its old environment to finish. The new feedback
fact makes the change visible to reasoning. Workflow management and workspace retention
have separate rules in [Staged workflows](../workflows.md).

Logs favor state changes. Stable polling, successful routine heartbeats, and repeated skips
should not flood the log. Environment creation, task dispatch, worker process launch,
health checks, timeout, conclude, claim release, cooldown, and cleanup remain observable.

Pausing or moving a workflow to recoverable Trash also invalidates review leases. Restore
keeps unfinished workflows paused until explicit resume. Saved graph, artifact, workspace,
usage, and review history remain available. These are workflow management rules; standalone
project deletion retains its original semantics. See [Workflow management](../workflow-management.md).

The intent protocol stores only the current claim holder for open intents, not a complete
worker history. Stopping a project clears open claims, so its graph cannot identify the last
worker on a stopped unfinished intent. Concluded intents keep their producing worker.

## Prompt resources

The operational prompts are already English and remain the authoritative prompt text:

- [Bootstrap](../../cairn/src/cairn/dispatcher/prompts/default/bootstrap.md)
- [Bootstrap conclude](../../cairn/src/cairn/dispatcher/prompts/default/bootstrap_conclude.md)
- [Reason](../../cairn/src/cairn/dispatcher/prompts/default/reason.md)
- [Explore](../../cairn/src/cairn/dispatcher/prompts/default/explore.md)
- [Explore conclude](../../cairn/src/cairn/dispatcher/prompts/default/explore_conclude.md)

The old embedded copies of prompts and example configuration have been replaced by these
links so the specification does not describe a different task contract from the files loaded
by the dispatcher.
