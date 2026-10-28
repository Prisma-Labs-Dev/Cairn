# Status and limits

Wayline adds staged workflows, artifact delivery, conditional results and session inspection
above the original Cairn exploration engine. The comparison baseline is
`oritera/Cairn` at `8e7e0ea`. [Core changes](docs/core-changes.md) is the sole record of changes
to that engine. [Installation](docs/installation.md) owns setup and operation.

## Available behavior

- Independent roots, dependency chains and joins, with separate stage fact graphs and shared workflow files.
- Required file capture before completion, binary downloads with hashes, bounded recovery, and retained workspaces.
- Typed accepted results, restricted server-evaluated conditions, and explicit not-applicable outcomes.
- Bounded discovery of all-required goals or ranked choose-one alternatives, with recorded collection membership and retries.
- Optional fresh-agent completion review, frozen claims and bounded rework. Verification is off by default.
- Runtime concurrency controls, workflow pause/resume and recoverable Trash/restore.
- Native Pi workers, captured live activity and original managed sessions, plus an optional pinned read-only conversation viewer.
- CLI-reported usage by model/invocation, durable replay, explicit coverage and missing-usage navigation.
- JSON/YAML creation and offline validation through the CLI and API. Codex and Claude Code drivers remain available.

## Validation evidence

The deterministic Python suites cover API/CLI creation, DAG and conditional gates, collection
membership, ranked recovery, cancellation, restart, artifact capture, outbox ownership and
replay, session bounds and identity, independent reviews, and usage coverage. They use
synthetic CLI streams and harmless local subprocesses rather than paid models.
[Installation](docs/installation.md#clone-and-validate-the-installation) describes the separate
wheel-install and deterministic HTTP/workflow smoke check.

On 2026-10-28, the final local macOS Python 3.13 regression run passed 577 tests and
29 benchmark subtests. The bare `python3 scripts/check-install.py` invocation ran from an
unsupported Python 3.15 host and selected supported Python 3.13.17 for the isolated install.
Wayline 0.3.0 installed from the final source snapshot into a fresh virtual environment and
HOME, outside the checkout, without PYTHONPATH or an activated virtual environment on PATH.
Dependencies used frozen, hash-checked public-PyPI requirements.

The installed CLI, init/doctor, API, 10 static assets, packaged default and mock prompts,
and a two-stage mock workflow passed. Its explicitly preseeded artifact downloaded with
the expected bytes and hash. Two mock invocations made zero model calls; no child processes
remained afterward. This verifies installation, resource delivery and deterministic
scheduling. It does not verify model-authored output or live authentication. Native doctor
separately passed offline against Pi 0.78.0 with 46 available catalog models;
`live_auth_verified` remained false. The rebuilt pinned viewer passed nine frontend component
tests and its `TestWayline` Go checks, and the renamed binary reported its Wayline build
version. These are build and deterministic checks, not a new live provider run. Portable
outcomes are recorded here; operator-local receipts and build logs are not distributed.

GitHub CI is configured for Python 3.12, 3.13 and 3.14 plus application and worker source
container builds; results are pending. Local container image building was unavailable because
the Docker daemon was not running. Live provider/container execution remains unverified,
and no worker image publication is claimed.

The [2026-10-27 Luna pilots](docs/benchmark-pi-luna-results-2026-10-27.md) and
[2026-10-28 retained-run UI review](docs/ui-run-review-2026-10-28.md) are historical evidence
from the Cairn-named build. The nine live model attempts had six strict passes, two capped
workflow failures and one report citation presentation failure. Seven attempts had complete
usage reconciliation; the capped attempts did not. One attempt per comparison cell does
not establish general workflow benefit. Raw operator-local evidence is not distributed.

The retained-run browser review checked genuine workflow/stage reports, exact artifact
download bytes, graph/session links, captured-event grouping, missing-usage navigation and
390-pixel layouts. Those checks do not establish a new machine's provider login, current
model availability, measured billing, or live container execution. Worker-reported results
remain distinct from independent verification.

## Current limits

- Python 3.12 through 3.14 on macOS/Linux, with 3.13 recommended. Python 3.15 is excluded after a locked watchfiles/PyO3 installation failure. Native Pi and optional viewer toolchain requirements are in the installation guide.
- The server defaults to loopback and has no multi-user authentication boundary. Local workers run with the dispatcher user's permissions; working directories are not sandboxes.
- Runtime caps apply per dispatcher. Use one scheduling dispatcher per server; give each dispatcher its own persistent outboxes on a filesystem with advisory-lock support.
- Workflows have at most 128 stages, including bounded one-level generated goals. Nested reusable workflows, arbitrary expressions and mutation of accepted results are not implemented.
- Conditions compare declared scalar predecessor results. Layers and stage kinds are display labels. A stage named review does not enable independent verification.
- Artifact paths must be distinct across a workflow. Files are limited to 10 MiB each and 32 declarations per stage. The UI downloads artifacts and does not render/edit their contents.
- Artifact presence, worker completion and optional agent review do not establish correctness. State the required evidence in each goal and inspect the result.
- Token totals cover available CLI reports, not provider-wide billing. Missing counters remain unknown. The Pi viewer labels billing as not measured.
- Captured transcript/event limits are finite. Data never captured before a crash cannot be recovered. Persistent outboxes replay captured bytes; server delivery has its own receipt state.
- Trash is recoverable and retains data. There is no automatic permanent erasure or history expiry.
- Fresh Wayline storage is the recommended deployment. The name change does not automatically migrate Cairn databases, environment variables, outboxes, session mirrors or workspace references.
- Live provider/container behavior is separate from deterministic regressions and wheel smoke checks. The worker image is now a generic Pi environment; add task-specific tools explicitly.
