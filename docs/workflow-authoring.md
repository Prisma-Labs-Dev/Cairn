# Writing workflows for agents

Use this guide to turn a requested outcome into a Wayline workflow. Write an exact JSON or
YAML API object, validate it locally, then create it through the CLI or `POST /workflows`.
Wayline executes each stage as a goal, with its own fact graph and exploration. A definition
is not a shell script or a fixed list of agent tool calls.

## Start with the outcome

Write what must be true at completion, what evidence would establish it, and any required
output files. Distinguish three things in the goal and shared `origin` text:

- The outcome is the result the user requires. Do not silently substitute a different result.
- Hard constraints must remain true for every permitted method and fallback. Include scope,
  safety restrictions, deadlines, or required data coverage when they matter.
- Preferences rank acceptable methods. State which substitutions are allowed, rather than
  making an incidental method look mandatory.

For example, "make peanut-free pancakes; maple syrup preferred, honey allowed" permits
honey when syrup is unavailable. It does not permit peanut ingredients or ignoring an
allergy. A pancake goal cannot become an eggs goal merely because eggs are easier. If the
user accepts either breakfast, make the outcome "one peanut-free breakfast" and choose
between pancake and egg options. The selected pancake method can still adapt its topping
within those constraints.

For digital work, "produce a source-backed inventory covering every supplied host" is an
outcome. "Prefer the CSV snapshot, accept the JSON snapshot if it covers every required host"
is a method preference. "Do not contact live hosts or invent missing observations" is a
hard constraint. A schema cannot infer these distinctions for you; write them explicitly.

## Smallest workflow

This is a complete creation body. The input file is an example fixture that you must supply
in the worker workspace before execution.

```yaml
stages:
  - goal: Read inputs/observations.json and identify every record that lacks a source reference. Report record IDs and the checks performed; do not invent missing references.
```

The same body in JSON is:

```json
{"stages":[{"goal":"Read inputs/observations.json and identify every record that lacks a source reference. Report record IDs and the checks performed; do not invent missing references."}]}
```

Titles and starting context are optional. Wayline derives a workflow title from the first
stage's title and stage titles from goals. Artifact-only stages derive their goal and title
from their outputs. Missing stage IDs become `stage_1`, `stage_2`, and so on. Use explicit,
unique IDs when other stages reference them. IDs start with a letter and contain at most
64 letters, digits, underscores, or hyphens. Unknown fields and invalid dependencies are
rejected. A workflow accepts up to 128 stages, including later generated goals.

`origin` supplies shared instructions and input locations. It defaults to empty; it is not
a required planning essay. A workflow definition does not upload its input files. Place
fixtures in the runtime workspace before starting a dispatcher. Standalone projects use a
project directory; stages share a workflow directory. The local directory is normally
`<local.workspace_root>/<workflow_id>`. Container files belong in that workflow container's
workspace. File preparation is separate from schema validation.

## Choose the dependency shape

| Shape | Use it when | Definition |
| --- | --- | --- |
| One stage | The agent can achieve one coherent outcome with internal exploration | One goal; no dependency list needed |
| Chain | A later outcome needs an earlier result | Give the later stage `depends_on: [earlier_id]` |
| Parallel required work and join | Every branch is required and can work independently | Separate roots or a shared prerequisite; the join lists every required branch |
| Dynamic required work | Research discovers tasks that must all finish | Discovery stage with `expansion: {mode: all, max_goals: N}` |
| Dynamic alternatives | Research ranks methods and exactly one successful option is needed | Discovery stage with `expansion: {mode: one, max_goals: N}` |

Missing `depends_on` means an independent root, not an implicit sequence. Listing stages
in an array does not chain them. Every listed prerequisite must satisfy its completion gate
before a dependent starts, or explicitly permit a not-applicable outcome. `kind` can be `research`, `work`, `synthesis`, or `review`; `layer`
groups stages visually. Both are labels, not execution rules. In particular, `kind: review`
does not enable independent completion verification.

Fetching milk and fetching honey are parallel required tasks if both ingredients are
needed. Do not model them as alternatives. Pancakes versus eggs are alternatives if either
satisfies the accepted breakfast outcome. Do not put both into required branches and expect
Wayline to know it should cook only one. Several small actions can also remain intents inside
one stage; create a separate stage when it needs its own goal, gate, output, or visible
lifecycle.

## Required parallel research and a join

This complete definition uses supplied network inventory fixtures. Both research results
are required before reconciliation. It performs no live scanning.

```yaml
title: Reconcile supplied network inventory
origin: >-
  Use only inputs/hosts.csv and inputs/service-observations.json supplied in the shared
  workspace. Preserve source row or record IDs. Do not contact live hosts or fabricate
  observations. Describe missing coverage explicitly.
stages:
  - id: hosts
    kind: research
    layer: evidence
    goal: Read inputs/hosts.csv and write evidence/hosts.json containing each supplied host, its source row ID, and duplicate or missing identifiers.
    artifacts:
      - path: evidence/hosts.json
        label: Host evidence
  - id: services
    kind: research
    layer: evidence
    goal: Read inputs/service-observations.json and write evidence/services.json containing each recorded service, its host reference and source record ID, plus any malformed records.
    artifacts:
      - path: evidence/services.json
        label: Service evidence
  - id: reconcile
    kind: synthesis
    depends_on: [hosts, services]
    verification_enabled: true
    goal: Join evidence/hosts.json and evidence/services.json by host identifier. Write outputs/inventory.json with source references and outputs/gaps.md listing unmatched hosts, malformed records and uncertainty. Account for every input record without inventing a service.
    artifacts:
      - path: outputs/inventory.json
        label: Reconciled inventory
      - path: outputs/gaps.md
        label: Coverage gaps
```

Both roots are eligible immediately; actual concurrency depends on dispatcher and worker
capacity. The final stage waits for both, then requires its own files and independent review.
For a chain, give `services` a dependency on `hosts` only if its work really needs that
result. Avoid inventing serial dependencies just to control resource usage.

## Discover one approach and retain fallbacks

This complete definition asks research to choose a usable source, then produce and check one
inventory. It is suitable only when either source format is acceptable under the stated
coverage requirement.

```yaml
title: Choose a source-backed inventory method
origin: >-
  The workspace contains inputs/hosts.csv and inputs/hosts.json as candidate snapshots.
  Required outcome: one normalized inventory covering every host in inputs/required-hosts.txt,
  with a source record ID for every entry. Hard constraints: use supplied fixtures only,
  do not contact live systems, do not invent records. Prefer CSV when both snapshots meet
  the requirements; JSON is an allowed fallback. Missing coverage is not success.
stages:
  - id: choose_source
    kind: research
    verification_enabled: true
    goal: Inspect the supplied snapshots and required host list. Establish which methods can produce the required inventory, rank up to two viable alternatives, and explain the selection. Give each proposal a distinct output path and explicit ID. Do not claim that merely inspecting the inputs produces the inventory.
    expansion:
      mode: one
      max_goals: 2
  - id: check_inventory
    kind: review
    depends_on: [choose_source]
    verification_enabled: true
    goal: Find the successful selected option's collected inventory through its artifact metadata or optional historical context. Check every required host and source reference against the supplied fixtures. Write outputs/inventory-check.md with the inspected artifact path and hash, checks and observed results, and unresolved issues.
    artifacts:
      - path: outputs/inventory-check.md
        label: Inventory checks
```

`choose_source` must finish its research before proposing methods. It has not produced the
inventory merely by planning one. In `one` mode, only the selected generated option receives
a project; other options remain inactive. The `check_inventory` dependency waits for a
successful selected option, not just for discovery to finish. If verification is enabled,
claims wait for approval before either transition.

The proposal array records fallback order. If the selected option becomes unavailable, Wayline
tries the highest-ranked untried option. After one succeeds, remaining options are skipped.
In `all` mode every generated goal is required, and unstarted direct successors wait for all
of them. `all` is the default expansion mode. Do not omit `mode: one` when the children are
alternatives.

## Branch on accepted findings

Use typed results when a later stage needs a specific finding. Put the contract in the
stage definition and actual values in the worker's completion. Wayline evaluates conditions
against the accepted result; a condition is not an instruction for the next agent to
reinterpret.

This complete example investigates supplied records only when research found relevant ones,
then writes a report in either case:

```yaml
title: Investigate relevant supplied records
origin: >-
  Use only inputs/records.json in the shared workspace. Preserve record IDs and source
  references. Do not invent observations. Produce a report even when nothing is relevant.
stages:
  - id: select
    kind: research
    verification_enabled: true
    goal: Read the supplied records, identify the relevant records, and write evidence/selection.json with IDs and reasons. Zero relevant records is a valid finding.
    result:
      relevant_count: {type: integer, minimum: 0}
    artifacts:
      - path: evidence/selection.json
  - id: investigate
    kind: research
    depends_on:
      - stage: select
        when: select.result.relevant_count > 0
    goal: Use the accepted selection to propose one independent investigation goal per relevant record. Each goal must cite the supplied source and declare its own distinct evidence file.
    expansion: {mode: all, max_goals: 8}
    emits:
      collection: investigations
      result:
        record_id: {type: string}
        supported: {type: boolean}
  - id: report
    kind: synthesis
    depends_on:
      - select
      - collection: investigations
        allow_not_applicable: true
    goal: Read the investigations manifest and its referenced evidence, plus the selection artifact. Write outputs/report.md covering every selected record and its evidence. If investigation did not apply, explain the negative selection finding. Distinguish unsupported findings from missing work.
    artifacts:
      - path: outputs/report.md
```

An accepted `select` completion includes `result: {"relevant_count": 2}`. With verification
enabled, this result remains pending until another agent approves the frozen claim. A
rejected claim does not start investigation.

If the accepted count is zero, `select` still completed successfully. `investigate` becomes
`not_applicable` with a durable reason and never starts a worker. The report explicitly
permits that absence. Without `allow_not_applicable: true`, inapplicability propagates to
the report. `blocked` continues to mean waiting; it is not a negative finding.

Every listed dependency must resolve. A successful unrelated prerequisite never overrides
a false condition. A dependency can be a string, `{stage, when?, allow_not_applicable?}`,
or `{collection, allow_not_applicable?}`. A `when` expression must reference a declared field
on that entry's stage. Supported comparisons are `==`, `!=`, `>`, `>=`, `<`, `<=`, and `in`.
There are no boolean operators or arbitrary executable expressions. Each dependency target
appears once. If one predecessor needs a combined test, declare a boolean result such as
`eligible` in that predecessor and explain its evidence requirements in the goal.

Result contracts are flat maps. Fields support `integer`, `number`, `boolean`, or `string`,
with homogeneous scalar `enum` values and optional numeric bounds. Enum-only fields infer
their type. All declared fields are required. Extra fields, invalid values, booleans passed
as integers, and non-finite numbers are rejected. Contracts have at most 32 fields and
16 KiB of encoded data; completion results also have a 16 KiB limit. Strings have at most
2,048 characters and enums at most 64 values. Malformed worker results enter bounded
correction, pausing after three correction attempts for inspection and explicit resume.
A typed `supported: true` is still a
worker claim; use evidence and verification to assess accuracy.

## Collect generated results explicitly

In the example, `investigate.emits.result` is the result contract inherited by every
generated investigation. It is different from the discovery stage's own optional `result`.
Generated goals retain ordinary artifact requirements, so proposals should give each member
a distinct explicit output path:

```json
{
  "id": "record_alpha",
  "goal": "Check record alpha against its supplied source; cite the source and report record_id and supported.",
  "artifacts": [{"path": "evidence/alpha.json", "label": "Record alpha evidence"}]
}
```

After completing this goal, its worker includes
`"result": {"record_id": "alpha", "supported": false}` beside its normal completion
fields. `false` can be a valid completed finding. It does not mean that the investigation
itself failed.

Read membership through
`GET /workflows/{workflow_id}/collections/investigations`, not a directory glob. The manifest
records the emitter, whether membership is sealed, readiness, and each member's stage,
project, outcome, accepted result, and artifact references. The UI shows these under Result
collections. Earlier findings remain optional reads through the stage history.

Membership seals when discovery completion is accepted. A successful `goals: []` produces a
ready empty collection. An inapplicable discovery is recorded separately and requires
explicit `allow_not_applicable` on a collection dependency. Paused or unfinished members do
not silently become successful results. Collections require `expansion.mode: all`; ranked
choose-one options keep their selection and fallback semantics. Generated members cannot
create recursive workflows or replace their inherited result contract.

The UI's New workflow form supports simple stages. Use **Edit full JSON definition** for
conditions, result contracts, and collections, or create the same object from a JSON/YAML
file with the CLI. Creating a revised advanced workflow preserves those fields in the JSON
editor instead of reducing them to simple dependency checkboxes.

## Initial definitions and worker completion are different formats

Author the `stages` object above. Do not put a precomputed `goals`, `selected_goal`, or
`selection_reason` at the workflow root or stage root. Those are discovery completion fields,
produced only after the worker has actually established its findings. A definition's `result`
is a field contract; the completion's `result` map contains the observed values.

For the choice example, a discovery worker might return this reason-task result, using a
real source fact ID from its current graph. This is not a workflow creation body and is not
a recorded run result:

```json
{
  "accepted": true,
  "data": {
    "complete": {
      "from": ["f001"],
      "description": "Inspection established that both supplied snapshots cover every required host and retain source record IDs.",
      "goals": [
        {
          "id": "csv_inventory",
          "goal": "Use inputs/hosts.csv to write alternatives/csv-inventory.json covering every host in inputs/required-hosts.txt with source row IDs. Use only supplied fixtures; do not contact live systems or invent records.",
          "artifacts": [{"path":"alternatives/csv-inventory.json"}]
        },
        {
          "id": "json_inventory",
          "goal": "Use inputs/hosts.json to write alternatives/json-inventory.json covering every host in inputs/required-hosts.txt with source record IDs. Use only supplied fixtures; do not contact live systems or invent records.",
          "artifacts": [{"path":"alternatives/json-inventory.json"}]
        }
      ],
      "selected_goal": "csv_inventory",
      "selection_reason": "Both pass the coverage constraints, so choose the preferred CSV method and retain JSON as fallback."
    }
  }
}
```

The dispatcher adds its worker identity and calls `POST /projects/{id}/complete` with the
inner completion fields. Direct API clients also supply `worker`, plus producer identities
when verification is enabled. Bootstrap wraps a confirmed `fact` alongside `complete`;
reason uses the form above. The task prompt supplies the current output contract.

An expandable completion must contain an explicit `goals` array. In `all` mode `[]` means
no further tasks were found. In `one` mode an empty array with `selection_reason` means no
viable option was found and pauses the choice; it is not successful delivery of the overall
outcome. Nonempty `one` completions require `selected_goal` matching a proposed ID and a
nonblank `selection_reason`. Give option IDs explicitly to avoid guessing generated IDs.
Selection fields are rejected in `all` mode.

`max_goals` defaults to 16 and permits 1 through 64. Each proposal needs a nonblank goal,
at most 8,000 characters. Optional proposal fields are `id`, `title`, `kind`,
`bootstrap_enabled`, and `artifacts`. Workers cannot supply dependencies, nested expansion,
or a verification override. Generated goals inherit the discovery stage's verification
setting. IDs and artifact paths must remain unique across the entire workflow. Invalid or
excessive proposals are rejected without partially appending stages. Repeated malformed
expansion results enter bounded correction and pause after three unsuccessful attempts.

## Report unavailable with evidence, then recover deliberately

First try allowed method adaptations within the selected goal. An unavailable preferred
topping is not proof that allowed pancakes are impossible. Missing proof or a failed test
usually means more work or correction, not automatic selection of an easier goal.

When observed evidence establishes that the selected option cannot achieve its immutable
goal, record that fact and report unavailable. For example, a selected CSV method might find
that its source has been removed and cannot be recovered, while a previously ranked JSON
method remains usable. The reason-task output could be:

```json
{
  "accepted": true,
  "data": {
    "unavailable": {
      "from": ["f003"],
      "description": "The selected CSV-source inventory goal cannot be achieved: the required CSV snapshot is no longer present and the fixture owner confirmed it cannot be restored within this run. The evidence is recorded in f003."
    }
  }
}
```

The dispatcher calls `POST /projects/{id}/unavailable` with `from`, `description`, and its
`worker` identity. `from` references actual project facts, not invented IDs. Bootstrap can
instead return observed evidence in `data.fact.description` with the unmet goal and reason
in `data.unavailable.description`; the dispatcher writes the fact first. Unavailable is
accepted only for the current selected choice project, not ordinary required stages or an
option awaiting completion review. It never creates a goal-completion edge. Wayline records
the evidence and activates the next untried alternative atomically. An ordinary rejected
independent review creates corrective work on the same option; it is not an unavailable
report or permission to bypass verification.

When all options are unavailable, the workflow pauses with `choice_error`. Plain Resume
does not restart an exhausted choice or invent another option. If new circumstances make an
existing unavailable option viable, an operator may call
`POST /workflows/{workflow_id}/choices/{parent_stage_id}/retry` while paused:

```json
{"goal_id":"csv_inventory","reason":"The fixture owner restored the required CSV snapshot and confirmed its source references are intact."}
```

This records the explanation and prepares a fresh attempt. The workflow stays paused until
explicit Resume through `PUT /workflows/{id}/status` with `{"status":"active"}`. The fresh
attempt receives a new project ID and retains earlier evidence; old workers cannot switch
or complete it through an earlier ID. Retry is for an existing unavailable option in an
exhausted nonempty choice, not an edit to the original goal.

For empty choices, changed hard constraints, new alternatives, or a wrong original outcome,
use **Create revised workflow**, edit the copied original definitions, then explicitly
create the new workflow. API authors likewise submit a revised `POST /workflows` body.
The old run remains intact. Do not claim unavailable as successful completion, repeatedly
resume without addressing the reason, or weaken a user's hard constraints to make a gate pass.

## Artifacts, verification, and context

`artifacts` is optional. Each entry names one required regular file relative to the shared
workspace, with an optional display `label`. A stage can require a goal, artifacts, or both.
Paths must be distinct across every stage and generated alternative, even when alternatives
will not all execute. Do not overwrite a predecessor's declared output; write a new revision
path. Absolute paths, `..`, directory paths, and symlinks are not valid artifacts. Up to 32
files can be declared per stage, at most 10 MiB per collected file.

Wayline collects required files before accepting completion. Missing outputs create corrective
work and keep successors blocked. After three unsuccessful automatic collection recoveries,
the workflow pauses for inspection and explicit resume. Collected artifacts are downloadable
with hashes and stage provenance. File presence does not establish semantic correctness.
Undeclared working files are shared but are not automatically collected into server storage.

`verification_enabled` is false by default. Set it true on each initial stage that needs an
independent completion gate. A fresh reviewer must approve its frozen evidence before the
stage counts as complete. Generated children inherit their parent's setting, so enabling it
on discovery also enables it for its generated options. A later static stage has its own
setting; it does not inherit one merely through `depends_on`. Review failures and repeated
rejections have bounded budgets before pause. This adds an assessment, not a guarantee.

Each stage receives its own goal, current graph, shared starting instructions, and required
outputs. Earlier findings are not all copied into its prompt. A compact optional history
pointer lets it list completed same-workflow references and selectively retrieve facts,
intents, hints, completions, and artifact metadata. The reader defaults to small pages and
bounded text windows; full history is not mandatory reading. Choice history also exposes
selected evidence from earlier unavailable attempts on demand. For authoring, specify what
the next stage must accomplish and which file or evidence it needs, not a pasted transcript
of every earlier step.

Runtime concurrency belongs in dispatcher configuration or live **Execution** controls,
not in the workflow object. Worker pool size may serialize eligible parallel branches.
Increasing a runtime cap cannot override a worker's own capacity. Caps apply per dispatcher,
not as a cross-process global quota. Use one scheduling dispatcher per server for predictable
execution. Creating a workflow does not itself start a dispatcher or make model calls.

## Validate and create without a conversation

Install and start the server and dispatcher with [Installation](installation.md). A running
server is needed for creation; execution also needs a configured dispatcher and signed-in
worker CLI. Prepare inputs before starting dispatch. Offline validation makes no model calls.

Save one of the complete YAML bodies above as `workflow.yaml`.

```sh
uv run --project wayline wayline workflow schema > workflow-schema.json
uv run --project wayline wayline workflow create --file workflow.yaml --dry-run
uv run --project wayline wayline workflow create --file workflow.yaml --server http://127.0.0.1:8000
```

The schema and dry run are offline. Dry run returns JSON with `dry_run`, `resource`, and the
normalized `request`; it creates nothing. Creation returns workflow detail and IDs and makes
root stages active. Use a dry run when the next action must remain reviewable before creation.
A live dispatcher will pick up eligible work. Place input files before running it.

JSON, YAML, and stdin use the same exact API object, with no wrapper:

```sh
uv run --project wayline wayline workflow create --file - --dry-run < workflow.yaml
```

Input is UTF-8, at most 1 MiB. Commands return JSON and nonzero status on parse, validation,
network, or server errors. Schema validity proves the definition's shape, not the availability
of files or the truth of a research premise. Inspect server state before retrying a creation
request after an ambiguous network failure; creation is not an idempotent retry operation.

For direct API use, send the same object as JSON to `POST /workflows`. Read `GET /workflows/{id}`
for stage and choice states, `GET /projects/{id}` for a selected graph, and the server's
`/openapi.json` for request/response schemas, including completion and retry contracts.
Use `PUT /workflows/{id}/status` with `stopped` or `active` to pause or resume. Titles or
starting context can later be refined through `PATCH /workflows/{id}/metadata`; changing
context affects future stages, not already-started ones. Goals and completed history are
not rewritten by metadata editing.

Detailed operational references are [workflow behavior](workflows.md),
[creation commands](agent-creation.md), [independent verification](verification.md),
[working-run management](workflow-management.md), and [runtime controls](runtime-controls.md).
