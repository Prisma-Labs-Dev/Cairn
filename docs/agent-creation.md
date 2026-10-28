# Create projects and workflows from agents

For goal design, constraints, parallel requirements, and ranked alternatives, read
[Writing workflows for agents](workflow-authoring.md) before creating the definition.

Wayline accepts JSON requests at its API and JSON or YAML definitions through the CLI.
The CLI prints JSON results and errors and does not prompt for missing metadata.
Commands below assume `wayline` is on your PATH. From this repository, use
`uv run --project wayline wayline` in its place.

## A project needs only a goal

```json
{"goal": "Find the relevant evidence and explain the result."}
```

Submit it to `POST /projects`, or use the command directly:

```sh
wayline project create --goal "Find the relevant evidence and explain the result."
```

The server derives a short display title from the goal. Omitted starting context remains
unspecified, rather than becoming an invented fact. You can provide `title` and `origin`
when they are useful; omission, null, and blank values all use the defaults.

The goal remains required. A title is a label and starting context is supporting input;
neither needs to be supplied just to start work.

## A workflow needs its stages

Save this as `workflow.yaml`:

```yaml
stages:
  - id: find
    goal: Identify the relevant item and record supporting evidence.
  - goal: Use the identified item to produce the requested result.
    depends_on: [find]
    artifacts:
      - path: outputs/result.json
```

```sh
wayline workflow create --file workflow.yaml
```

The same structure works as JSON or as a `POST /workflows` body. Workflow and stage titles
are optional. Stage IDs default to `stage_1`, `stage_2`, and so on when omitted. Give a stage
an explicit ID when it makes dependencies easier to read. An explicit ID must be valid and
must not collide with another explicit or generated ID.

Each stage needs a goal or a required artifact. An artifact-only stage can omit its goal;
Wayline derives the instruction to produce those files. Omitted `depends_on` still means no
prerequisites. List dependencies to create a chain or a join; independent stages may run in
parallel. File order alone does not create dependencies.

## Let a stage discover parallel goals

Opt in on a workflow stage when its work should determine what comes next:

```yaml
stages:
  - id: discover
    goal: Identify the items in the supplied fixture that need investigation.
    expansion: {max_goals: 16}
  - goal: Combine the item investigations into the requested result.
    depends_on: [discover]
```

On actual completion, the worker adds an explicit `goals` array to its `complete` object:

```json
{"goals": [{"goal": "Investigate item alpha."}, {"goal": "Investigate item beta."}]}
```

The completion API accepts that array alongside `from`, `description`, and `worker`.
Use `goals: []` when there are no follow-up goals; omission is invalid for an expandable
stage. Each proposed item requires `goal` and may specify `id`, `title`, `kind`,
`bootstrap_enabled`, or `artifacts`. It cannot supply dependencies or further expansion.
The server starts the new goals in parallel after the parent and makes its unstarted
direct successors wait for every generated child.

`max_goals` defaults to 16, accepts 1 through 64, and the workflow stays bounded to 128
stages. Invalid or conflicting proposals roll back the completion. Dispatcher correction
attempts are bounded to three before the workflow pauses for attention. See
[Staged workflows](workflows.md#discover-goals-while-working) for the full completion
example, idempotent retries, join semantics, and recovery behavior. Prior context remains
optional for generated stages, just as for stages defined at creation.

## Files, pipelines, and schemas

```sh
wayline project create --file project.json
printf '%s\n' '{"goal":"Explain the supplied result."}' | wayline project create --file -
wayline workflow create --file workflow.yaml --server http://127.0.0.1:8000
wayline project schema
wayline workflow schema
wayline workflow create --file workflow.yaml --dry-run
```

Schemas come from the same request models as the API. The server also exposes its OpenAPI
schema at `/openapi.json`. File parsing uses safe YAML loading; importing a definition
does not execute scripts or expand shell variables.

Definitions are limited to 1 MiB. `--dry-run` validates without contacting the server or
starting work; its JSON result contains the normalized API body under `request`. Without
`--dry-run`, creation starts active work just like the API, and a running dispatcher can
pick it up. Successful commands return the full project or workflow response. Errors return
a JSON `error` object and a nonzero exit status.

## Refine optional metadata later

Agents can write a clearer title or supply observed starting context after creation:

```sh
wayline project metadata proj_001 --title "Evidence review"
wayline project metadata proj_001 --origin "The supplied materials are in inputs/."
wayline workflow metadata workflow_001 --title "Evidence to result"
```

The corresponding endpoints are `PATCH /projects/{id}/metadata` and
`PATCH /workflows/{id}/metadata`, with `title`, `origin`, or both. Metadata updates require
at least one nonempty value. They do not change goals.

Project metadata can be refined while the project is active. Changing its starting context
adds a small change notice so the dispatcher reconsiders it. Workflow metadata can be
refined while active or stopped. A workflow context change applies to stages that start
after the change; it does not rewrite the starting context of existing or completed stages.

No model call is needed to derive the initial labels or create the definition. Agents may
provide better metadata through the same API or CLI when appropriate.

## Alternative versus required follow-up goals

Goal expansion defaults to `mode: all`: every proposed child is required and can run in
parallel. Use `expansion: {mode: one, max_goals: 3}` for a research stage that will rank
mutually exclusive routes. Its worker completion supplies `goals`, one member's
`selected_goal` ID, and `selection_reason`. Only that option executes. Discovery alone
does not unlock successors; the choice must succeed, after optional verification.
See [ranked choice and recovery](workflows.md#choose-one-approach-with-ranked-fallback)
for machine-readable examples, unavailable evidence, exhaustion, and explicit retry.
Titles and origin remain optional in either mode.
