# Staged workflows

This fork adds workflows above Cairn's existing exploration engine. A workflow contains
several goals. Cairn works toward each goal inside a normal project and unlocks its
successors when it records that goal as complete.

Dependencies support both a chain and parallel branches that join. For example:

1. Investigate a topic.
2. Research two subtopics independently.
3. Synthesize both investigations into a manuscript.
4. Research gaps found during synthesis.
5. Revise and review the manuscript.

Stages can be marked `research`, `work`, `synthesis`, or `review`. These are labels for
readers, not different agent implementations. A `layer` label groups related stages;
`depends_on` determines execution order. Each stage still uses Cairn's bootstrap,
reason, and explore tasks.

## Create and run a workflow

Start the server and dispatcher with the normal Cairn instructions in the README.
For local execution, the configured worker CLI must already be installed and signed in.
No new model account or provider is required for workflows.

Use **New workflow** in the UI and describe the stage goals or output files. Workflow and
stage titles and starting context are optional. Add prerequisites to chain stages or join
branches. A stage can specify a goal, required artifacts, or both. Open a stage's project to inspect its fact graph,
add hints, or read its findings. Use workflow stop/resume to control the whole run.

Agents can create the same definitions from JSON or YAML. See [Agent creation](agent-creation.md)
for minimal requests, CLI commands, schemas, and metadata editing.

The API accepts the same definition at `POST /workflows`. For example, one stage can find
the relevant item and the next can use that finding to produce a specified output:

```json
{
  "title": "Find and use a result",
  "origin": "Find the requested item in the supplied materials and use it to produce the requested deliverable.",
  "stages": [
    {
      "id": "find",
      "title": "Find the item",
      "goal": "Identify the matching item and record evidence supporting the identification.",
      "depends_on": []
    },
    {
      "id": "produce",
      "title": "Use the finding",
      "goal": "Use the identified item to produce the deliverable described in the starting context.",
      "artifacts": [{"path": "artifacts/produce/result.json", "label": "Result"}],
      "depends_on": ["find"]
    }
  ]
}
```

Artifacts are optional required output files, not a special document format. Images, data,
code, and documents use the same mechanism. If a stage specifies artifacts without a goal,
the server derives a goal to produce those files. When both are specified, completion
requires the output files as well as the agent's explanation that the goal was achieved.

This longer example shows how the same mechanism can support research and synthesis:

```json
{
  "title": "Research investigation",
  "origin": "Investigate a topic supplied by the user. Keep source references and distinguish evidence from inference. Store research notes and the manuscript in the shared workspace.",
  "stages": [
    {
      "id": "scope",
      "title": "Define the investigation",
      "kind": "research",
      "goal": "Write scope.md with the questions, exclusions, source strategy, and completion criteria.",
      "depends_on": []
    },
    {
      "id": "evidence",
      "title": "Gather evidence",
      "kind": "research",
      "goal": "Use scope.md to create evidence.md with cited findings and unresolved questions. Preserve source notes under sources/.",
      "depends_on": ["scope"]
    },
    {
      "id": "draft",
      "title": "Write the investigation",
      "kind": "synthesis",
      "goal": "Use scope.md, evidence.md, and sources/ to write manuscript.md. Cite sources for material factual claims and disclose unresolved questions.",
      "artifacts": [{"path": "manuscript.md", "label": "Investigation manuscript"}],
      "depends_on": ["evidence"]
    },
    {
      "id": "review",
      "title": "Review and revise",
      "kind": "review",
      "goal": "Check manuscript.md against its sources and scope. Write corrected final-manuscript.md and review.md with changes and remaining limitations.",
      "artifacts": [{"path": "review.md", "label": "Review notes"}, {"path": "final-manuscript.md", "label": "Final manuscript"}],
      "depends_on": ["draft"]
    }
  ]
}
```

To add a parallel branch, give two stages the same prerequisite. A joining stage must
list both IDs in `depends_on`. Every listed prerequisite must complete before it starts.
The server rejects duplicate stage IDs, missing prerequisites, and dependency cycles.

## Discover goals while working

A stage can opt into generating independent follow-up goals. For example, discover items
in supplied materials, investigate each item, then combine the results:

```yaml
stages:
  - id: discover
    goal: Identify the items in the supplied fixture that need investigation.
    expansion:
      max_goals: 16
  - id: combine
    goal: Combine the completed item investigations into a result.
    depends_on: [discover]
```

After actually achieving `discover`, its worker includes goals in the completion response:

```json
{
  "from": ["f001"],
  "description": "The supplied fixture contains two relevant items.",
  "worker": "worker-name",
  "goals": [
    {"id": "alpha", "goal": "Investigate item alpha in the supplied fixture."},
    {"id": "beta", "goal": "Investigate item beta in the supplied fixture."}
  ]
}
```

This is the body for `POST /projects/{id}/complete`. Worker bootstrap/reason output puts
the same `goals` array inside its `complete` object. An expandable stage must supply an
explicit array; `[]` means there are no follow-up goals. Proposing more work never replaces
achieving the current goal.

The server validates and records completion and appends the new stages in one transaction.
`alpha` and `beta` depend on `discover` and become available in parallel. The still-blocked
`combine` stage now waits for `discover`, `alpha`, and `beta`. Every unstarted direct
successor receives the same join dependencies. Generated stages expose
`generated_by_stage_id` so readers can trace where they came from.

Each proposal requires a nonblank `goal` of at most 8,000 characters. It may also specify
`id`, `title`, `kind`, `bootstrap_enabled`, and `artifacts`. Omitted IDs are deterministic
parent-based IDs. Workers cannot supply dependencies or enable expansion on generated
goals. Declared IDs and artifact paths must remain unique across the workflow; conflicting
proposals are rejected without partially changing the workflow. `max_goals` defaults to
16 and accepts integers from 1 to 64. The complete workflow remains limited to 128 stages.
Limits reject excessive proposals rather than silently dropping them.

Non-expandable stages reject nonempty proposals. Retrying an identical successful
expandable completion returns its original result without creating duplicate stages.
Different results for an already completed stage are rejected. A dispatcher worker's
missing or malformed goal list creates corrective work. After three unsuccessful
correction attempts, Cairn pauses the workflow with a readable `expansion_error`; explicit
resume resets that unfinished stage's recovery budget. These corrections use the existing
worker loop, with no separate model call for planning or metadata.

Generated children receive their own goal and the optional history pointer. The parent's
findings, completion explanation, and other proposals are not copied into child prompts.

## What a stage receives

Each stage starts with the workflow's starting instructions, its own goal and output
requirements, and its own fact graph. Previous stages' findings and completion explanations
are not copied into the new stage's prompt, hints, or graph. A small pointer tells the agent
how to inspect earlier work if it needs it.

Earlier work stays available on demand. The history index contains stage/project references
and counts. The agent can choose a stage, list short finding previews, then retrieve an
individual finding, intent, completion explanation, or artifact record. These reads are
optional; the agent is not instructed to read the history in full before starting.

History is scoped to stages that were already complete in the same workflow when the
current stage started. This includes earlier completed branches, not just immediate
prerequisites. Unrelated workflows, active stages, and future stages are excluded. Evidence
IDs and original records remain available when the agent needs to trace a claim.

The UI uses the same separation. Prior-stage references are cheap to display. Reading
findings and opening a full source record require explicit actions, and polling does not
fetch the historical records.

The dispatcher gives each worker a scoped Python history reader. Its path appears in the
optional-context part of the prompt. It supports `list`, `stage`, `facts`, `fact`, `intents`,
`intent`, `hints`, `hint`, `completion`, and `artifacts`. Lists default to five items. A selected
record defaults to a 4,000-character text window; `--text-offset` reads another window and
`--full` deliberately retrieves the complete selected record. Nothing is fetched just by
installing the reader or preparing the prompt.

The reader uses the Cairn server API. Local workers normally use the dispatcher's `server`
URL. For the shipped Compose setup with host-networked workers, the `cairn-server` hostname
maps to the server's published localhost port. For custom or remote deployments, set an
explicit worker-reachable address:

```yaml
runtime:
  history_server_url: "http://your-cairn-server:8000"
```

The scoped index is `GET /projects/{id}/history?offset=0&limit=20`. Stage metadata supplies
URLs for paged record previews, individual facts/intents/hints, completion, and artifacts.
It does not return all the historical bodies in one response.

Upgrading existing databases removes exact generated copies of old prerequisite handoffs
and replaces copied provenance facts with short pointers while retaining their IDs. Original
source records and user-written notes remain available. For legacy stages, history access
uses completed dependency ancestors because their exact activation order was not recorded.

## Working files and artifacts

Stages in a workflow share a runtime workspace. Files remain available to subsequent
stages. Declared output paths must be distinct across the whole workflow. A revision stage
reads its predecessor's file and writes its own output at a new path. This prevents a stage
from claiming an earlier stage's existing file as its required output. The shared workspace
does not merge conflicting edits to undeclared working files. Existing standalone projects
retain their original behavior.

Workflow workspaces are retained even when standalone project cleanup is configured to
remove completed projects. In container mode, working files remain inside the workflow
container. Required artifacts are additionally collected by the dispatcher into server-side
storage and linked from their stage, so they remain downloadable after the runtime stops.
Other working files are not automatically collected. Preserve those files before manually
deleting a container or workspace.

Artifact paths must name individual regular files relative to the shared workspace.
Absolute paths, traversal, and symlinks are rejected. Each stage can declare up to 32 files,
with a 10 MiB limit per file. Collected bytes, size, SHA-256, path, and stage provenance are
stored in the server's SQLite database. Back up that database to preserve collected outputs.
Downloads are attachments; there is no format-specific renderer.

If an agent attempts completion while an output is missing or cannot be collected, the
dispatcher records the problem and creates a corrective exploration intent. Downstream
stages remain blocked, and completion stays blocked while that corrective work is open.
For a stage with required artifacts, all of its current exploration intents must conclude
before collection and completion, so files are not captured while a worker is still writing.
After three unsuccessful automatic recovery attempts, Cairn pauses the workflow and shows
the artifact problem on the affected stage. Inspect the stage graph and hints, address the
problem, then resume explicitly to reset the recovery budget. Transport failures are retried
without asking an agent to recreate existing files. Once the file is collected and the goal is recorded as complete,
its captured bytes cannot be replaced by a later stage changing the same workspace path.

## Controls and completion

`GET /workflows` lists progress. `GET /workflows/{id}` returns stage goals, dependencies,
current states, project links, handoffs, and recorded completion explanations.
`PUT /workflows/{id}/status` accepts `{"status":"stopped"}` or `{"status":"active"}`.
`GET /projects/{id}/artifacts` lists a stage's collected outputs. The dispatcher uploads
raw file bytes to `POST /projects/{id}/artifacts?path=<declared-relative-path>` before
completion. Each returned artifact record includes its download URL.

Only unlocked stages have projects. Stopping a workflow stops its unfinished projects;
resuming continues them without discarding their graphs. Workflow-owned projects cannot
be independently reopened or deleted in ways that would invalidate later handoffs.

The workflow completes after all its stages complete. A stage with declared artifacts
cannot complete until those files have been collected. Completion still means Cairn has
accepted an agent's explanation and supporting fact references. File collection checks
presence and records the file's bytes; it does not independently verify whether the file
satisfies the semantic goal. Use explicit review stages and inspect the delivered work
when those checks matter.

## Initial scope

This is a dependency-based workflow implementation with opt-in goal expansion.
It does not yet support arbitrary conditional branches, nested reusable workflows,
a source library, or a rendered book/report editor.
Further research can be planned as intermediate stages, and agents can explore new intents
within each stage.

The current stage's own graph can still grow during a long investigation. This change
prevents previous stages from expanding the starting context; it does not summarize or
compact the current stage's local work automatically.

## Development verification

```sh
uv run --project cairn --group dev pytest
```

Tests use isolated databases and mock/local processes. They do not call paid models or
run security testing against external targets.
