# Agent map

Wayline supports Python 3.12 through 3.14. Use Python 3.13 for the default environment.
Work from the repository root with `uv`.

Read [installation](docs/installation.md) before running workers. It owns setup, storage,
authentication, startup, sample execution, and upgrade instructions. Read
[workflow authoring](docs/workflow-authoring.md) before creating your own definitions.
[STATUS.md](STATUS.md) records limits and verification boundaries.

## Commands

```sh
uv sync --frozen --project wayline --python 3.13
uv run --project wayline --group dev pytest
python3 -m unittest discover -s benchmarks -p 'test_*.py' -v
python3 scripts/check-install.py
uv run --project wayline wayline doctor --config /absolute/path/to/dispatch.yaml
```

The regression suites and installation smoke make no model calls. Live dispatch executes
workers with the configured permissions and may consume provider usage. Do not run live
benchmarks merely to validate documentation or packaging.

## Where changes belong

- `wayline/src/wayline/`: server, dispatcher, CLI, prompts, and static dashboard.
- `wayline/tests/`: runtime and API regressions.
- `docs/`: current feature guides and specifications. Keep setup in `installation.md`.
- `docs/core-changes.md`: sole behavioral change record for the original engine.
- `docs/archive/`: historical illustrations, not installation examples.
- `benchmarks/`: synthetic benchmark tools and evaluator contracts.
- `container/`, `docker-compose.yaml`, `scripts/`: deployment and build tooling.
- `vendor/pi-web/`: pinned read-only viewer patch and attribution.

Keep runtime databases, outboxes, workspaces, raw sessions, credentials, and host diagnostics
outside Git. Preserve historical benchmark names, commit hashes, and evidence paths as
historical facts. Do not claim worker-reported completion or CLI token totals establish
independent correctness or measured billing. Add independent checks when the task requires them.
