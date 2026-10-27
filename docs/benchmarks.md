# Small Cairn comparison suite

Status: setup and offline checker tests only. No model benchmark has run. There are no
performance results. Sol designs and checks the tests; **Luna executes every benchmark
worker call**, including bootstrap, planning, reason, exploration, conclusion and retries.

The suite compares the original Cairn engine at `8e7e0ea`, this fork as a standalone
project, and this fork as a workflow. Each receives identical fixture bytes, starting
context and final goal. The workflow adds discovery, independent generated verification
goals, and a synthesis stage. The original and standalone runs may also plan parallel
intents. This measures the cost and benefit of explicit workflow boundaries, including
their overhead.

## Cases

| Case | Task | Checker |
| --- | --- | --- |
| `needle` | Find one row in a 32-row TSV, normalize text, multiply a quantity, hash a canonical string. | Exact fields and SHA-256. A tiny overhead control. |
| `research` | Discover three synthetic products, verify current claims, resolve archived price conflicts, retain one unknown export claim, produce a cited comparison. | Exact candidate fields, recommendation, conflicts, gap, supporting quotes and report links. |
| `inventory` | Probe four explicitly allowlisted loopback ports, discover three independent HTTP services and one closed port, inspect each reachable service, produce a cited inventory. | Compare actual reachable origins, identity/status fields, source quotes and the unreachable origin against the withheld manifest. |

Research uses generated saved HTML served on loopback. Inventory starts three real,
independent loopback HTTP listeners with synthetic identity and status pages. Its fourth
candidate port stays bound without listening, so connections fail and another process
cannot take it during the run. The worker may probe only the four listed ports. There
are no real external targets, exploits, external web requests,
API keys or paid calls in fixture creation, serving, validation or preflight. A seed makes
fixture generation repeatable; changing it changes selected values. These are small
engineering cases, not evidence about general research quality or security capability.

## Prepare without a model

From the repository root:

```sh
python3 -m unittest discover -s benchmarks -p 'test_*.py' -v
uv run --project cairn --group dev pytest cairn/tests/test_benchmark_setup.py cairn/tests/test_codex_worker_model.py
python3 benchmarks/bench.py prepare --case research --seed pilot-1 \
  --workspace /tmp/cairn-pilot-research-worker \
  --private /tmp/cairn-pilot-research-evaluator
python3 benchmarks/bench.py serve \
  --public /tmp/cairn-pilot-research-worker/input/public --port 8765
```

Run `prepare` separately for `needle` and `inventory` with fresh directories. Reuse the
same case and seed across the three comparison modes. Serving accepts only generated
public files. It has no checker route, directory listing or private manifest endpoint.
Needle needs no HTTP server. For inventory use a separate command instead of `serve`:

```sh
python3 benchmarks/bench.py prepare --case inventory --seed pilot-1 \
  --base-url http://127.0.0.1:8765 \
  --workspace /tmp/cairn-pilot-inventory-worker \
  --private /tmp/cairn-pilot-inventory-evaluator
python3 benchmarks/bench.py serve-inventory \
  --lab-config /tmp/cairn-pilot-inventory-evaluator/lab.json
```

Here the base port allocates a block; candidates are ports 8766 through 8769. The worker
gets only `input/public/targets.json`, which lists those four allowed origins, not which
are reachable. The listener mapping and response material stay in private `lab.json`.
An occupied candidate makes lab startup fail. Do not scan for alternatives or silently
change ports mid-attempt. Choose a fresh explicit base port, regenerate every mode's
identical fixture bundle, then restart the attempt before any worker begins.

The worker bundle contains `input/public/` and `task.json`. The separate evaluator bundle
contains `expected.json`, a hash manifest, `standalone.json`, and `workflow.json`. Expected
answers include source quotes, so an operator can inspect the checker before running.
Do not submit the evaluator bundle, this repository, generation code, seed, or expected
answer to a worker. Run evaluation after workers stop and withhold its results until the
attempt ends. There is no evaluation feedback loop for workers.

Disjoint paths prevent accidental copying; they are not an operating-system security
boundary. Cairn's local workers run with host permissions. For a protected evaluation,
use a dedicated isolated machine or sandbox with only the worker bundle mounted and the
loopback fixture available inside it. Keep the evaluator on the outside. Never describe
a same-user local run as a protected hidden-answer evaluation. The supplied local config
is a routing template for such a disposable environment, not an isolation mechanism.

## Workflow contract

The generated `workflow.json` is a request matching the actual `CreateWorkflowRequest`
model. Its first stage has `"expansion": {"max_goals": 16}`. At actual discovery completion,
the worker returns child definitions in `complete.goals`, for example:

```json
{
  "accepted": true,
  "data": {
    "fact": {"description": "Discovered the candidate list and saved notes/discovery.json."},
    "complete": {
      "description": "Discovery is complete; each listed candidate needs independent verification.",
      "goals": [
        {
          "id": "verify_aster",
          "title": "Verify Aster",
          "kind": "research",
          "goal": "Inspect Aster's current and archived pages. Verify the fields required by the final task. Write notes/checks/aster.json with cited evidence and remaining gaps.",
          "artifacts": [{"path": "notes/checks/aster.json"}]
        }
      ]
    }
  }
}
```

This shows the response shape, not a prewritten answer or the complete generated child
set. The model discovers the candidates and proposes one independent goal per item.
Children do not supply `depends_on` or further `expansion`. The server supplies their
dependencies, runs them within the common worker limit, and makes the existing synthesis
stage wait for every generated child. Discovery cannot complete until its own required
artifact is collected. The tiny needle case deliberately returns `goals: []`.

Prior history is optional. Stages may read shared notes or retrieve selected prior facts
on demand; there is no instruction to load all previous history. Only synthesis declares
`answer.json` and `report.md`, so children cannot satisfy final artifact requirements by
reusing each other's output paths.

## Pin Luna before any pilot

`benchmarks/dispatch.luna.local.yaml` explicitly selects `model: gpt-6-luna` and
`reasoning_effort: low` for all task types. Replace its workspace placeholder in an
external run copy, then check it:

```sh
uv run --project cairn python benchmarks/preflight.py --config /tmp/cairn-pilot-dispatch.yaml
```

Preflight checks the actual config and adapter commands for both execute and resume,
CLI flag support and the exact model slug in the local CLI model catalog. It does not
invoke a model. Cached discovery is not proof of live access. On the setup machine the
catalog listed `gpt-6-luna`; no live access test has been made. If a future invocation
rejects that slug, stop and report the failure. Never silently substitute another model
or allow a worker to inherit the host's default model.

The fork supports optional per-worker model and effort fields. Old Cairn's local adapter
ignores `CODEX_MODEL` environment variables and inherits host CLI defaults. A baseline
must therefore receive the same minimal model-routing and usage-recording transport
patch in a disposable checkout at `8e7e0ea`. Record that exact diff and both revision
hashes. Do not apply workflow, prompt, scheduler or history changes to the original
baseline. Until this equivalent transport and accounting are present, the three-way
comparison is not ready to run.

## Pilot procedure

Start with one attempt per case per mode, nine attempts total. Do not run repeats until
the first results show whether they would answer a useful question. Keep every result,
including failures. Do not tune prompts for a losing mode after seeing its answer.

1. Integrate and verify the feature work, routing, and usage recording first. Use a fresh
   database, runtime directory and CLI session store for each attempt. Record the exact
   original/fork commits, baseline transport diff, CLI version and fixture hashes.
2. Freeze the same Luna model, low effort, prompts for common phases, tools, sandbox,
   network access and budgets in all three modes. Use two global workers, two running
   projects and two workers per project; reason may propose at most two intents. The
   workflow may generate up to sixteen goals but receives no extra worker capacity.
3. Use the template's same per-call timeouts in every mode. Apply a common whole-attempt
   limit of 15 minutes, 24 model invocations and 50,000 measured input-plus-output tokens.
   These aggregate limits need an external supervisor; the YAML alone does not enforce
   them, and this setup does not yet include that supervisor. Before a pilot, arrange a
   process deadline and a watcher of the actual invocation/usage ledger. At a limit,
   mark the workflow or project stopped through its status endpoint, then stop the
   dispatcher and verify no worker processes remain. Record any overshoot from
   in-flight calls and count it. Stop on the first limit
   or normal completion. A timeout is a result, not permission for an uncounted rerun.
4. Start only the API server. Create the original/standalone project from `standalone.json`
   or the fork workflow from `workflow.json`. The CLI supports `cairn project create
   --file ...` and `cairn workflow create --file ...`; the original API accepts
   `POST /projects` with the same title, origin and goal fields.
5. Before starting the dispatcher, copy only the worker bundle into its runtime directory
   at `<workspace_root>/<project_id>` or `<workspace_root>/<workflow_id>`. All workflow
   stages use that shared directory. Start the research static server or the inventory
   listener lab before workers. Keep the inventory server process outside the worker's
   filesystem sandbox while sharing its controlled loopback network namespace. A
   container with separate loopback networking needs an explicit equivalent arrangement
   before it can use these URLs. The directory and
   services must be ready before bootstrap; do not let fixture timing vary
   across modes. Run the dispatcher with the frozen config, retaining full logs.
6. Capture attempt start before dispatcher startup and finish after its workers stop.
   Preserve all invocation records, source/citation artifacts, graph exports and output
   bytes. For workflows, evaluate the final collected artifact bytes and record their
   hashes. For standalone runs, capture the files at stop with the same hash procedure.
7. Evaluate outside the worker environment. Keep an evaluator-side bundle with the
   original `input/public/` plus captured `answer.json` and, when required, `report.md`:

```sh
python3 benchmarks/bench.py evaluate \
  --workspace /tmp/cairn-pilot-research-captured \
  --private /tmp/cairn-pilot-research-evaluator
```

The checker rejects wrong values, unknown fields, invented or missing source quotes,
missing Markdown citations and changed public input fixtures. Candidate/service list ordering
does not matter. It checks the structured answer, not the truth of arbitrary report
prose. A human must also check whether the report accurately describes the evidence and
uncertainty. Cairn's own completed status and artifact presence do not replace either
check.

## Account for all work

Use the dispatcher's actual invocation usage records and retain raw CLI JSON events.
Reconcile the ledger against every process started, including failed, cancelled,
bootstrap, reason, explore, conclusion, retry and planning calls. A missing usage event
means unknown usage, not zero. Report the comparison as incomplete until reconciled.
Input tokens include cached input; do not add cached tokens again. Output totals include
reasoning tokens when the provider includes them. If reported separately, expose the
reasoning subset without adding it twice. Do not compare only the final answer call.

For an independent aggregation check, export the measured records to JSONL with this
shape. Values below illustrate the schema and are not benchmark measurements:

```json
{"call_id":"example-only","phase":"bootstrap","model":"gpt-6-luna","effort":"low","input_tokens":100,"cached_input_tokens":20,"output_tokens":10,"started_at":1.0,"finished_at":4.0,"status":"success","raw_log":"call-example.jsonl"}
```

```sh
python3 benchmarks/bench.py summarize --ledger /tmp/cairn-pilot-calls.jsonl
```

`summarize` rejects missing usage, a different model/effort, duplicate calls and an empty
ledger. Its wall time spans the earliest call start through latest call finish. Also
report the full dispatcher-start-to-stop time, which includes initialization, scheduling
and cleanup. Preserve failed attempts, call count, total input/output/cached tokens,
budget hits, semantic pass/fail, citation pass/fail, missing artifacts and human report
review. A successful attempt is not cheaper if it hides its planning or retries.

There are currently no pilot measurements to populate a results table. Offline fixture
and schema tests verify setup only; they do not establish a workflow advantage.
