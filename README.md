# Wayline

Wayline runs agent workflows as goals with explicit dependencies, shared working files,
and inspectable evidence. Each stage keeps its own fact-and-intent graph. Later stages can
read earlier findings, collect required files, branch on typed results, or join bounded
sets of discovered work. The UI shows stage reports, artifacts, sessions, and reported usage.
Pi is the default local worker; Codex and Claude Code drivers are also available.

## Quick start

Requires Python 3.12 through 3.14, recommended 3.13, [uv](https://docs.astral.sh/uv/), and native Pi.
[Install and sign into Pi](docs/installation.md#sign-into-native-pi) with your own subscription
or API key, then select a model with `/model`. From a checkout:

```sh
uv sync --frozen --project wayline --python 3.13
uv run --project wayline wayline init --runtime-dir "$HOME/.local/share/wayline-local"
source "$HOME/.local/share/wayline-local/runtime.env"
uv run --project wayline wayline doctor --config "$HOME/.local/share/wayline-local/dispatch.yaml"
```

Follow [Installation and operation](docs/installation.md) to start the server and dispatcher,
run the sample workflow, inspect its output, and stop/restart safely. Doctor makes no model
calls; a live workflow establishes provider access separately. Local workers run with your
user account's permissions. The default server listens on loopback. The separate
`python3 scripts/check-install.py` checks an isolated wheel install with deterministic workers.

## Documentation

- [Installation and operation](docs/installation.md): the authoritative setup and runtime guide.
- [Writing workflows](docs/workflow-authoring.md): goals, constraints, dependencies, conditions, and examples.
- [Agent CLI and API](docs/agent-creation.md): JSON/YAML requests, schemas, validation, and metadata.
- [Workflow behavior](docs/workflows.md), [runtime controls](docs/runtime-controls.md), and [pause/trash/restore](docs/workflow-management.md).
- [Completion verification](docs/verification.md), [usage coverage](docs/usage.md), and [Pi sessions/viewer](docs/pi-sessions.md).
- [Status and limits](STATUS.md): capability boundaries and verification evidence.
- [Core changes](docs/core-changes.md): the single change record against the original engine.
- [Server](docs/specs/server-protocol.md), [dispatcher](docs/specs/dispatcher-design.md), and [workflow result](docs/specs/workflow-results.md) contracts.
- [Benchmark procedure](docs/benchmarks.md), [historical pilot results](docs/benchmark-pi-luna-results-2026-10-27.md), and [retained-run UI review](docs/ui-run-review-2026-10-28.md).
- [Worker container](container/README.md): optional Docker execution.

## Upstream and license

Wayline is derived from [Cairn by oritera and its contributors](https://github.com/oritera/Cairn),
using upstream revision `8e7e0ea` as the original-engine comparison baseline. Upstream
history and attribution remain in Git. The optional Pi viewer derives from
[pi-web](https://github.com/ygncode/pi-web) and retains its [MIT license](vendor/pi-web/LICENSE).

Wayline is licensed under [GNU AGPL-3.0](LICENSE). See the license for its terms.
