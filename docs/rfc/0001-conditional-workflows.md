# RFC 0001: Conditional stages, typed results, and collections

Status: adopted with revisions. Scope: workflow definitions and accepted completion results.
Existing definitions remain valid. See [Writing workflows for agents](../workflow-authoring.md)
for creation commands and complete examples.

## Why these additions exist

A stage often discovers whether a later step is useful. Leaving that decision only in the
later goal text asks the worker to enforce the dependency itself. Cairn can instead validate
small result fields and evaluate declared conditions before starting work.

This adoption adds:

- `result`, a flat contract for a stage's completion values.
- Conditional dependencies evaluated by the server against accepted results.
- `not_applicable`, a terminal stage outcome with a recorded reason.
- `emits`, a named collection of explicitly recorded generated members.

These describe claims and scheduling. Types do not prove that a finding is true. Evidence
and optional independent review remain necessary. Verification stays off by default.

## Typed results

A definition declares required fields:

```yaml
- id: ownership
  goal: Inspect the supplied records and count confirmed and ambiguous assets.
  result:
    confirmed_count: {type: integer, minimum: 0}
    ambiguous_count: {type: integer, minimum: 0}
    source_checked: {type: boolean}
```

The normal `POST /projects/{id}/complete` body carries `result` beside `from`,
`description`, and `worker`:

```json
{
  "from": ["f001"],
  "description": "The supplied records support two assets; one remains ambiguous.",
  "worker": "worker-name",
  "result": {"confirmed_count": 2, "ambiguous_count": 1, "source_checked": true}
}
```

Bootstrap and reason workers put the same map inside their `complete` object. Verification
adds its existing producer identity requirements. A definition's `result` describes a
schema; a completion's `result` contains values. Neither is a file upload.

Fields support `integer`, `number`, `boolean`, and `string`. An `enum` can accompany a type
or infer a homogeneous scalar type. Numeric fields can set `minimum` and `maximum`.
Every declared field is required; undeclared fields and incompatible values are rejected.
Booleans do not count as integers. Numbers must be finite. Nested objects, computed fields,
and arbitrary schema programs are not supported. Contracts have at most 32 fields and
16 KiB of encoded data; result values also have a 16 KiB limit. Scalar strings are bounded
to 2,048 characters and enums to 64 values. Malformed worker results enter bounded
correction and pause after three correction attempts.

A successful negative finding is still completion. For example, `confirmed_count: 0` does
not mean the investigation failed or that the investigation itself was inapplicable.

## Conditional dependencies

`depends_on` accepts existing string IDs or structured entries:

```yaml
- id: exposure
  goal: Assess the confirmed assets using only the supplied fixtures.
  depends_on:
    - stage: ownership
      when: ownership.result.confirmed_count > 0
```

The condition references a declared field on that entry's predecessor. Supported operators
are `==`, `!=`, `>`, `>=`, `<`, `<=`, and `in`, with scalar literals or a literal list for
membership. Conditions do not execute Python, shell, JavaScript, or model-generated code.
The authoring validator checks references, types, operators, and compatible values.
There are no boolean operators or arbitrary cross-stage expressions in this version.
Each dependency target appears once. A combined test on one predecessor can be an explicit
boolean result field with evidence requirements stated in its goal.

Every listed prerequisite must resolve. There is no implicit OR between dependencies.
Array order does not create dependencies. A stage with no prerequisites is an independent
root. Existing string dependencies keep their ordinary all-required meaning.

## Not applicable is different from waiting

A false condition resolves the dependent stage as `not_applicable`. It creates no worker
project and does not claim that the stage goal was achieved. The recorded reason and
resolution time survive restart and appear in the workflow UI.

`blocked` keeps its existing meaning: this stage is waiting for prerequisites. A stopped
project is paused work, not an inapplicable stage. A choose-one option's `unavailable`
outcome remains a distinct evidence-backed failed alternative.

Inapplicability propagates unless a dependency explicitly permits it:

```yaml
- id: combine
  goal: Summarize completed research and state which branches did not apply.
  depends_on:
    - stage: exposure
      allow_not_applicable: true
    - ownership
```

This allows absence of `exposure`; it does not waive `ownership`, accept failed work, or
turn a false condition on the current stage into a true condition. The default remains
strict. Not-applicable stages remain visible and are counted separately from achieved
goals. A workflow can finish when every required stage either completed or did not apply.

## Verification and accepted results

A pending completion claim does not publish a usable stage result. With verification
enabled, the claim freezes the proposed values with its evidence. Approval publishes the
accepted completion and result atomically, then evaluates downstream eligibility. Rejection
creates corrective work while downstream stages remain waiting.

Accepted results are not mutable routing variables. This version does not retract an
accepted result, restart completed successors, or undo effects already performed. Use an
explicit revised workflow when new evidence changes a completed premise.

## Named collections

An `all` expansion can declare a collection and a result contract for its generated members:

```yaml
stages:
  - id: discover
    goal: Find the supplied records that need checking and propose one independent goal per record.
    result:
      records: {type: integer, minimum: 0}
    expansion: {mode: all, max_goals: 8}
    emits:
      collection: checked_records
      result:
        record_id: {type: string}
        valid: {type: boolean}
  - id: summarize
    goal: Read the checked_records manifest and report valid records and unresolved issues.
    depends_on:
      - collection: checked_records
```

`discover.result` describes the discovery finding. `discover.emits.result` describes each
member's result. Generated goals inherit that member contract and the parent's verification
setting. Each proposal can declare distinct required artifact paths using the existing
`artifacts` field. It cannot supply another expansion or its own dependency graph.

Collection membership comes from recorded generated stage IDs, never from a directory glob.
The emitter's accepted completion seals membership. A successful empty discovery uses
`goals: []`, produces a sealed empty collection, and permits its collection-dependent join.
Otherwise, the collection becomes ready only when its recorded members have resolved.
Paused or unfinished members do not count as complete.

`GET /workflows/{workflow_id}/collections/{name}` returns the explicit manifest:

- Collection name and emitter stage ID.
- Whether discovery has sealed membership and whether the collection is ready.
- Each member's stage ID, project ID, outcome, accepted result, and artifact metadata.
- Reasons for members or an emitter that did not apply.

Artifact metadata includes download references and hashes through Cairn's ordinary artifact
records. Missing or inapplicable work remains visible rather than silently disappearing.
A dependency may explicitly permit an inapplicable emitter with
`{collection: checked_records, allow_not_applicable: true}`. An inapplicable emitter is
not the same as a successful empty discovery.

The workflow detail includes collection data for the UI. Workers receive compact references
and can read detailed history when useful. Collections do not copy every previous session
into the next worker's prompt.

## Compatibility and limits

Existing string dependencies, ordinary completions, ranked choose-one expansion, artifact
gates, pause/resume, and recoverable Trash retain their behavior. Unknown authoring fields
remain errors. Old servers reject new definitions instead of silently ignoring conditions.

Collections belong to `expansion.mode: all`. Choose-one alternatives retain their existing
selection and fallback lifecycle. A workflow remains limited to 128 stages, with bounded
one-level expansion. Recursion, reusable nested workflows, arbitrary predicates, and
mutation of completed results are outside this adoption.

## Changes from the draft

The draft reused `blocked` as a terminal outcome. This adoption uses `not_applicable` and
keeps waiting separate. `allow_blocked` becomes `allow_not_applicable`.

The draft conflicted between all-required dependencies and an implicit alternative
satisfiable dependency. All dependencies are required unless a specific absence is allowed.

The draft proposed undoing downstream state after review rejection. This adoption gates
publication on review approval, preserving the existing review lifecycle.

The draft's `emits.record` glob becomes explicit membership plus a typed member result
contract. Actual artifacts still use declared per-goal paths.

The draft's claim that typed results alone prevent false findings was too strong. This
adoption validates shape and scheduling; it does not establish truth. A restricted grammar
is chosen for a small reviewable contract, without claiming that other expression languages
are inherently undecidable.

## Examples and validation

[The fixture access-model example](0001-example.yaml) uses this adopted format. It is
restricted to supplied local inputs and does not authorize contact with live systems.
The authoring guide also gives a generic research example.

Validate before creating work:

```sh
uv run --project cairn cairn workflow create --file docs/rfc/0001-example.yaml --dry-run
```

Dry-run checks the definition. It does not run workers, validate the truth of findings, or
supply the example's input files.
