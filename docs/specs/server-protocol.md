# Wayline collaborative exploration protocol

This document describes the current project graph protocol. The [dispatcher specification](dispatcher-design.md)
describes its bundled consumer. The [core change record](../core-changes.md) compares this fork
with upstream `8e7e0ea`. Workflows, historical context, and artifact APIs are documented in
[Staged workflows](../workflows.md), outside this core protocol description. The running
server's `/openapi.json` provides the exact request and response schemas.

## Model and design

Wayline models goal-directed exploration as a directed acyclic graph. Consumers start from
known facts, explore directions, and append new facts until some findings together satisfy
the goal. Its name comes from the stones hikers stack as route markers. Each fact records
progress that another consumer can build on.

The API maintains graph consistency. It does not decide whether a hypothesis is useful or
whether evidence proves the goal. Consumers make those decisions. A consumer can also attempt
a simple problem directly at the start; the attempt still records an intent and its resulting
fact before declaring completion.

The design follows the blackboard architecture used by Hearsay-II. Independent consumers
read shared state and contribute findings. Facts are the board's contents, intents record
exploration, and hints are annotations. Intent source and destination links retain causal
relationships as well as results. This form of indirect coordination is also called
stigmergy. Each consumer's read, evaluate, propose, execute, and record cycle follows an
Observe-Orient-Decide-Act loop. A goal specifies the result to achieve; hints can express
human priorities while consumers choose their next actions.

The graph expresses knowledge and causality. `Project.reason` is separate coordination
state that makes a project-wide reasoning task mutually exclusive and visible. It is not a
fact or an edge and does not participate in graph reasoning. The bundled dispatcher centrally
schedules worker processes; the protocol's shared-board design does not imply that those
processes independently claim work.

### Recording changed conditions

Ordinary facts are appended, not edited or marked obsolete. A changed condition is another
fact. For example:

- `f003`: "Obtained a root shell on host-A."
- `f007`: "Discovered subnet 10.0.2.0/24 through host-A."
- `f025`: "The host-A shell disconnected."

Consumers can use the later observation to plan a new route without deleting the earlier
finding. Fact descriptions should summarize observations and reference larger evidence
files, rather than paste entire logs. For example: "Ports 22, 80, 443, and 8080 are open;
80 and 8080 serve HTTP. Full scan: /tmp/scans/nmap_192.168.1.10.xml."

Two explicit management operations are exceptions to immutable graph history. An active
project's starting `origin` can be refined through the metadata endpoint. Reopening a
completed standalone project removes its completion edge and appends corrective feedback.
Both operations are described below.

## Concepts and data

### Project

A project contains one goal and its graph.

| Field | Meaning |
| --- | --- |
| `id` | Server-generated project ID |
| `title` | Display title |
| `status` | `active`, `stopped`, or `completed` |
| `bootstrap_enabled` | Whether consumers may start with a direct bootstrap attempt, default `true` |
| `verification_enabled` | Require independent acceptance of completion, default `false` |
| `pending_review_id` | Pending completion claim ID, or `null` |
| `created_at` | Creation time |
| `reason` | Current project-level reason lease, or `null` |
| `workflow_id`, `workflow_stage_id` | Owning workflow and stage IDs, or `null` for standalone projects |

`active` projects accept exploration writes. `stopped` projects reject them and can resume.
An active project with `pending_review_id` also blocks producer exploration writes with `409`
until independent review resolves its claim.
`completed` projects reject them; standalone projects can be explicitly reopened after
external feedback disproves completion. Workflow-owned stage lifecycle is controlled through
the workflow rather than standalone stop, deletion, or reopen operations.

A reason lease contains `worker`, consumer-defined `trigger`, `started_at`, and
`last_heartbeat_at`. Only one live reason lease exists per project.

### Fact

```text
id             "origin", "goal", or a generated ID such as "f001"
description    Factual observation or special origin/goal text
```

Every project starts with `origin` and `goal` facts. A goal-only creation request gives
`origin` an empty description. Ordinary facts have no validity flag and are created by
concluding intents or recording external feedback.

### Intent

```text
id
from                 One or more source fact IDs
to                   Destination fact ID, or null while unfinished
description          Exploration direction or conclusion edge description
creator              Consumer that declared the intent, immutable after creation
worker               Current holder while open; producing consumer after conclusion
last_heartbeat_at    Last heartbeat, retained even when an open claim is released
created_at
concluded_at         Conclusion time, or null
```

An unfinished intent has `to=null`. It may be unclaimed with `worker=null` or held by a
consumer. A concluded intent has a destination and is immutable. Its `worker` permanently
identifies the producer of the conclusion. An expired open claim clears `worker` but retains
`last_heartbeat_at`. The protocol does not store every previous holder of an unfinished intent.

`creator` identifies the consumer that proposed the direction. Creation can claim the intent
at the same time, but then `worker` must equal `creator` and the first heartbeat is recorded.
`from` cannot contain `goal`.

Multiple source facts form a hyperedge. They jointly support the same exploration, description,
creator, and worker:

```text
f002 ---+
        +--- intent ---> f006
f004 ---+
```

The server does not assign special task types to intents. The bundled dispatcher reserves
`from=["origin"]`, `description="bootstrap"`, and `creator="dispatcher.bootstrap"` for
its first direct attempt. The ordinary heartbeat, release, and conclude APIs still apply.
This makes the first attempt and its evidence visible in the same graph as later exploration.

### Hint

```text
id
content
creator       Author of the hint
created_at
```

Hints are external guidance, not factual graph nodes or causal edges. They can be added in
all project states, except for workflow-owned projects in recoverable Trash. A hint on a
stopped project becomes available when it resumes; a hint on
a completed project remains stored but does not reopen it. Consumers may also use hints for
concise assessments such as "The first two hypotheses were ruled out; focus on file upload."

### Validation and lease settings

The server trims request text such as descriptions, creators, workers, triggers, and source
fact IDs. Empty values after trimming produce `422`. Creation metadata has a deliberate
exception: omitted, null, or blank `title` and `origin` are accepted. The title is derived
from the goal's first nonempty line, with whitespace collapsed and a maximum of 96 characters;
origin defaults to an empty string. Unknown project-creation fields are rejected.

`GET /settings` returns lease durations in seconds:

```json
{"intent_timeout":5,"reason_timeout":5}
```

`PUT /settings` accepts the same object. Both fields are required and at least 5 seconds.
An unfinished intent's holder expires after `intent_timeout` without heartbeat. A reason
lease expires after `reason_timeout`. Reading project state or graph exports also performs
lease expiry cleanup. These settings do not control worker count or execution timeout.
[Runtime controls](../runtime-controls.md) use separate endpoints.

## Project API

### List and create

`GET /projects` returns all project summaries, not full graphs. Each includes the project
metadata plus `fact_count`, `intent_count`, `working_intent_count`,
`unclaimed_intent_count`, and `hint_count`. Workflow stage projects are included and identified
by their workflow fields.

`POST /projects` creates an active standalone project. Its minimum request is:

```json
{"goal":"Find and verify the required result."}
```

A complete request can include optional metadata, bootstrap preference, and initial hints:

```json
{
  "title": "Investigation",
  "origin": "Start from the supplied target and evidence directory.",
  "goal": "Find and verify the required result.",
  "bootstrap_enabled": true,
  "verification_enabled": false,
  "hints": [{"content":"Check the existing evidence first.","creator":"human"}]
}
```

`verification_enabled` defaults to false. Setting it to true adds the independent completion
gate described below, for a standalone project or a workflow stage.

The response is full `ProjectDetail`, including the two special facts, empty intents, and
any supplied hints. `bootstrap_enabled=false` skips the direct attempt for the bundled
consumer. A consumer without bootstrap support can use reason even when it is enabled.
See [Agent creation](../agent-creation.md) for JSON/YAML files, stdin, validation, schemas,
and command-line creation.

### Read

`GET /projects/{project_id}` returns a full project:

```json
{
  "project": {
    "id": "proj_001",
    "title": "Investigation",
    "status": "active",
    "bootstrap_enabled": true,
    "verification_enabled": false,
    "pending_review_id": null,
    "created_at": "2026-03-21T10:00:00Z",
    "reason": null,
    "workflow_id": null,
    "workflow_stage_id": null
  },
  "facts": [
    {"id":"origin","description":"Start from the supplied evidence."},
    {"id":"goal","description":"Verify the result."},
    {"id":"f001","description":"Found a candidate in the source material."}
  ],
  "intents": [
    {
      "id": "i001",
      "from": ["origin"],
      "to": "f001",
      "description": "Inspect the source material.",
      "creator": "agent-A",
      "worker": "agent-B",
      "last_heartbeat_at": "2026-03-21T10:02:00Z",
      "created_at": "2026-03-21T10:01:00Z",
      "concluded_at": "2026-03-21T10:02:00Z"
    }
  ],
  "hints": [],
  "required_artifacts": [],
  "expansion": null,
  "workflow_deleted_at": null
}
```

### Metadata and title

`PATCH /projects/{project_id}/metadata` refines an active project's title, origin, or both:

```json
{"title":"Revised investigation","origin":"Use the newly supplied source directory."}
```

At least one field is required. Provided values must be nonempty strings; null, blank, and
unknown fields are rejected. The response is full `ProjectDetail`. This operation also works
for the active stage of a workflow. Changing origin updates its special fact and adds a small
`metadata` hint so the dispatcher notices the change without repeating the origin text in
the hint. It does not rewrite completed historical stages.

The original `PUT /projects/{project_id}/title` remains available. It accepts
`{"title":"Revised investigation"}`, returns `ProjectMeta`, and works in active, stopped,
and completed states. It does not alter the graph, claims, reason lease, or status.

### Stop and resume

`PUT /projects/{project_id}/status` accepts `{"status":"stopped"}` or
`{"status":"active"}` for standalone projects. It returns `ProjectMeta`. The operation
is idempotent when the requested state already applies. Pausing also invalidates pending
review leases; resume resets exhausted review budgets but keeps their history. Completed projects cannot change
state through this route; use reopen instead.

Stopping clears all unfinished intent holders and the reason lease immediately. Further
exploration writes return `403`. Consumers must treat this as a hard stop, cancel their local
processes, and stop the execution environment. Resume permits immediate new claims without
waiting for old leases to expire. Because open-intent worker history is not retained, the
stopped graph cannot identify the last holder of an unfinished intent.

### Delete

`DELETE /projects/{project_id}` deletes a standalone project and its graph, returning `204`.
The server does not directly control its runtime container. A dispatcher later observing the
deletion cancels its local tasks and removes the orphan container. Workflow-owned projects
must be managed through the workflow API.

## Reason lease API

All reason lease operations are exploration writes and require an active project. They return
`ProjectMeta` and produce no graph fact or intent.

| Endpoint | Body | Behavior |
| --- | --- | --- |
| `POST /projects/{id}/reason/claim` | `{"worker":"agent-A","trigger":"new_facts"}` | Claim an empty or expired lease; same-holder requests return the current lease; another live holder gives `409` |
| `POST /projects/{id}/reason/heartbeat` | `{"worker":"agent-A"}` | Renew the current holder; no holder or another live holder gives `409` |
| `POST /projects/{id}/reason/release` | `{"worker":"agent-A"}` | Release one's own lease; an already-empty lease is idempotent; another live holder gives `409` |

Stop and completion clear reason state on the server. Project list and detail responses expose
it so humans and consumers can see that a reasoning task is in progress.

## Hint API

`POST /projects/{project_id}/hints` appends a hint and returns the created `Hint`:

```json
{"content":"Prioritize the strongest remaining hypothesis.","creator":"human"}
```

Active, stopped, and completed projects accept hints unless the owning workflow is in Trash.
A later hint on a project awaiting verification does not change the frozen claim snapshot.

## Intent API

All intent operations below require an active project. Stopped and completed projects reject
exploration writes with `403`. Pending independent review blocks producer exploration writes
with `409`. A trashed owning workflow also blocks mutations with `409`. Concluded intents
cannot be claimed, released, or concluded again.

### Declare

`POST /projects/{project_id}/intents` creates an unfinished intent:

```json
{
  "from": ["f002", "f004"],
  "description": "Test the relationship supported by both findings.",
  "creator": "agent-A",
  "worker": null
}
```

Sources must be nonempty and reference existing facts in that project; `goal` is not a valid
source. `worker=null` declares without claiming. Setting `worker` equal to `creator` claims
on creation. Other worker values are rejected. The response is the created `Intent`. The
server treats `description="bootstrap"` as ordinary text; its reserved interpretation belongs
to the consumer.

### Claim and heartbeat

`POST /projects/{project_id}/intents/{intent_id}/heartbeat` accepts:

```json
{"worker":"agent-B"}
```

It claims an unheld or expired unfinished intent or renews the current holder. Another live
holder produces `409`. Consumers should continue heartbeats at a regular interval and poll
project status. An inactive-project response invalidates the old claim and requires local
work to stop.

### Release

`POST /projects/{project_id}/intents/{intent_id}/release` accepts the same worker body and
returns the updated `Intent`. Only the current holder can release a live claim. An already
unclaimed intent returns its current state. Another live holder produces `409`.
`last_heartbeat_at` is retained.

### Conclude

`POST /projects/{project_id}/intents/{intent_id}/conclude` atomically appends a fact and
concludes the intent:

```json
{"worker":"agent-B","description":"The candidate passes all recorded checks."}
```

The current holder can conclude. An unclaimed or expired intent can also be concluded by the
requesting worker. Another live holder produces `409`. The response contains `fact` and
`intent`. The new fact receives a generated ID; the intent's `to` points to it,
`concluded_at` is set, and its producing worker is permanently retained.

## Completion and external feedback

### Complete

`POST /projects/{project_id}/complete` atomically declares that source facts satisfy the goal:

```json
{"from":["f007","f012"],"description":"Both required checks passed.","worker":"agent-A"}
```

With verification off, it creates a concluded intent whose sources are the supplied facts and whose destination is
`goal`. Both `creator` and `worker` use the supplied worker. The project becomes completed
and its reason lease is cleared. The response is the completion `Intent`. Sources must exist
and cannot contain `goal`.

Ordinary completion with verification off requires an active project; a repeated standalone
completion gives `403`.
Workflow-owned projects additionally enforce their artifact and goal-expansion contracts.
Those extensions, including expandable-completion idempotence, are described in
[Staged workflows](../workflows.md). They do not change ordinary standalone completion with verification off.

### Completion with verification enabled

When `verification_enabled=true`, `/complete` takes two additional producer identity fields:

```json
{
  "from": ["f007", "f012"],
  "description": "Both required checks passed.",
  "worker": "agent-A",
  "producer_agent_id": "producer_run_001",
  "producer_session_id": "producer_session_001"
}
```

The server requires all exploration intents to be concluded and all required artifacts to be
uploaded. It validates any proposed workflow goals before accepting the request. The response
is a `CompletionClaim`, not an `Intent`. The project remains active, exposes
`pending_review_id`, and has no goal-completion edge. Producer mutation and artifact replacement
are blocked while the claim is pending. Identical completion submission returns its recorded
claim; a different submission while a claim is pending gives `409`.

The claim stores the completion request, producer identity, and frozen facts, hints, artifact
metadata, and artifact bytes. Its artifact URLs point to immutable claim downloads. Read
`GET /reviews` for paginated summaries, optionally filtered by `project_id`, and
`GET /reviews/{id}` for selected full context. Public reads omit the private lease token.

A fresh reviewer claims `/reviews/{id}/claim` with `agent_id`, `session_id`, and `worker`.
It renews or releases through `/heartbeat` and `/release` using the same identity plus the
returned `lease_token`. `/decision` requires those fields, `decision` of `approved` or
`rejected`, nonblank `feedback`, and an `evidence` array. Approval requires at least one
nonblank observed evidence item. An optional `cli_session_id` records the actual reviewer
CLI session when available. Matching producer agent/session identities, including an
extracted producer CLI session, are rejected. These IDs are audit identities, not credentials.

Approval applies the frozen completion atomically. A workflow's successors and proposed goals
wait until that approval. Rejection leaves the goal active and adds an actionable rework
intent. Three rejected claims or an exhausted three-attempt review execution budget pauses
automatic work until explicit resume. Pausing or moving the workflow to Trash invalidates
old review leases. Complete field bounds, lease timing, idempotence, and recovery semantics
are in [Independent verification](../verification.md).

### Reopen

`POST /projects/{project_id}/reopen` is a standalone-project management operation allowed
only for completed projects:

```json
{"description":"External validation disproved the submitted result; continue investigating.","creator":"reviewer"}
```

The server removes the single current completion edge and preserves its source fact IDs.
It creates a corrective fact and a concluded intent with `description="external_feedback"`,
those same sources, and the new fact as destination. Both `creator` and `worker` use the
request's creator. The project returns to active and its reason lease is cleared.
The response contains `project`, `fact`, and `intent`.

The removed completion edge is not retained as a past completion event in the graph. The
corrective fact supplies the new evidence for further exploration. This is explicit external
feedback rather than mutation of an ordinary finding.

## Exports

### YAML graph

`GET /projects/{project_id}/export?format=yaml` returns a YAML snapshot for consumers. It
includes project title, origin, goal, bootstrap preference, hints, facts, and intents, including
unfinished intents. It excludes heartbeat timestamps and the reason lease because those are
coordination state. The server expires old claims before export, so `worker` reflects the
current holder. Creation and conclusion timestamps use the server's local timezone and
`YYYY-MM-DD HH:mm:ss` format.

```yaml
project:
  title: Investigation
  origin: Start from the supplied evidence.
  goal: Verify the result.
  bootstrap_enabled: true
hints:
  - content: Check independent evidence.
    creator: human
    created_at: "2026-03-21 10:00:00"
facts:
  - id: origin
    description: Start from the supplied evidence.
  - id: goal
    description: Verify the result.
  - id: f001
    description: Found a candidate.
intents:
  - from: [origin]
    to: f001
    description: Inspect the source material.
    creator: agent-A
    worker: agent-B
    created_at: "2026-03-21 10:01:00"
    concluded_at: "2026-03-21 10:02:00"
  - from: [f001]
    to: null
    description: Verify the candidate independently.
    creator: agent-A
    worker: agent-C
    created_at: "2026-03-21 10:03:00"
    concluded_at: null
```

Workflow stage exports add bounded workflow metadata and a historical-context index reference.
They do not embed the earlier stages' full graphs or completion prose.

### Timeline

`GET /projects/{project_id}/export?format=timeline` returns chronological plain text. The YAML
export shows graph structure; the timeline shows event order and causal references. Event
types include `PROJECT CREATED`, `HINT`, `INTENT DECLARED`, `INTENT CONCLUDED`, and
`PROJECT COMPLETED`. A completion withdrawn by reopen is absent from the resulting timeline.

## Other current APIs

[Runtime controls](../runtime-controls.md) documents `GET /runtime`, the lightweight
`GET /runtime/settings` poll, `PUT /runtime/settings`, and safe dispatcher heartbeats. Its
nullable overrides apply independently per dispatcher and do not replace `/settings` leases.

[Token usage](../usage.md) documents `POST /usage/attempts`, `GET /usage/summary`,
`GET /usage/attempts`, and `POST /usage/reporters`. Counts are available CLI telemetry with
explicit reporting coverage. They are not provider billing totals.

[Workflow management](../workflow-management.md) documents `GET /workflow-trash`, recoverable
workflow `DELETE`, and `POST /workflows/{id}/restore`. Workflow Trash preserves data and blocks
writes. It does not change permanent standalone `DELETE /projects/{id}` semantics. A trashed
stage remains readable with `workflow_deleted_at` in its project detail.

## Typical consumer flow

1. Read structured project state for scheduling, claims, and valid fact references.
2. Export the current graph when needed to prepare a reason or explore prompt.
3. For an initial project, use bootstrap if enabled and supported. Create the reserved intent
   if missing, claim it, maintain heartbeat, and conclude any confirmed result. Complete only
   if the new fact proves the goal; otherwise continue from the updated graph. Release failed
   attempts.
4. For an unclaimed ordinary intent, claim before launching exploration, maintain heartbeat,
   and conclude the factual result. Release an abandoned attempt.
5. When reason is due and no unclaimed intent remains, claim the project reason lease and
   maintain its heartbeat. Complete, declare new unclaimed intents, or make no graph change
   while other work continues. Release reason after the attempt while still active.
6. If completion returns a pending claim, stop producer work and let a fresh reviewer decide
   it through the review API. Do not treat the claim as completed or unlock successors.
7. Stop local execution when project state becomes inactive or a lease is lost. Resume from
   fresh server state rather than trusting an old claim.
8. If external validation disproves a completed standalone result, reopen with factual
   feedback, then return to the normal active-project loop.
