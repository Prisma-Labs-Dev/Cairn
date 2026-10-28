# Installation and operation

This is the authoritative setup guide for Wayline. Feature guides link here for startup;
[workflow authoring](workflow-authoring.md) owns definition design. The local Pi route is the
default. Docker is optional and described in [Worker container](../container/README.md).

## Prerequisites

- macOS or Linux, Git, Python 3.12 through 3.14, and [uv](https://docs.astral.sh/uv/).
- A filesystem with advisory locks for durable dispatcher outboxes. SQLite data, workspaces,
  and session copies need writable persistent storage.
- Native [Pi](https://github.com/earendil-works/pi) installed and authenticated for live work,
  with Node.js 22.19 or newer. Node.js 24 is the recommended shared toolchain.
  The shipped adapter was exercised with Pi 0.78.0; that is the recorded tested version,
  not a promise that every future release has the same JSON/session protocol.
- The optional conversation viewer additionally needs Go 1.26.5, Node.js 24, npm, and Git.
  Its pinned upstream module declares Go 1.25.5 with toolchain Go 1.26.5.
  The dashboard and captured activity work without the viewer build.

Python 3.13 is the recommended version and is pinned in `wayline/.python-version`. The
package requires `>=3.12,<3.15`. Python 3.15 is unsupported because the locked `watchfiles`
PyO3 extension failed to install there; no ABI override is part of this setup.
The Python lockfile defines the reproducible application dependency set. Use `--frozen`.
Do not copy someone else's Pi configuration, OAuth files, API keys, sessions, or runtime data.
Each operator signs into their own provider account.

## Clone and validate the installation

Clone the repository:

```sh
git clone https://github.com/Prisma-Labs-Dev/Wayline.git
cd Wayline
uv sync --frozen --project wayline --python 3.13
python3 scripts/check-install.py
```

The smoke check builds and installs a wheel into a temporary isolated environment, runs the
installed CLI and HTTP server outside the checkout, and checks a two-stage deterministic
mock workflow with an explicitly preseeded artifact and exact downloaded bytes. The fixture
does not represent a model-authored output. It requires no provider login, paid calls, Docker, or existing runtime state. A pass
establishes packaging and deterministic execution for that machine. It does not prove live
provider access or container compatibility. Use `--python 3.13` to select the smoke
interpreter explicitly and `--output /absolute/new/directory` to retain a local receipt.
If clone access fails, obtain repository access
before proceeding; this document does not establish that the remote is public.

All source commands below run from the checkout root. `uv run --project wayline wayline`
is the source CLI; an installed wheel provides the shorter `wayline` command.

## Prepare isolated local storage

Use one dispatcher per server for predictable scheduling. Every dispatcher needs its own
usage outbox and session spool. Never point a second dispatcher at an already owned outbox.
Keep working files out of the source checkout and away from unrelated agent workspaces.

Initialize a new instance outside the checkout:

```sh
uv run --project wayline wayline init --runtime-dir "$HOME/.local/share/wayline-local"
```

`init` generates `dispatch.yaml` and `runtime.env`, refuses to overwrite existing files, and
sets explicit database, workspace, usage outbox, session spool, and viewer mirror paths.
It does not create a provider account, copy credentials, install Pi, or run workers.
Use a different directory and server port for another independent instance.

Load the generated environment in each shell used for server, doctor, or dispatcher:

```sh
source "$HOME/.local/share/wayline-local/runtime.env"
```

The generated configuration uses loopback `http://127.0.0.1:8000`, local execution, and Pi.
If changing the port, update its `server` URL and pass that port to `serve`. Native workers
inherit the dispatcher's environment and Pi account. `local.workspace_root` selects managed
project directories; stages share one workflow directory. `completed_action: keep` retains
their working files. Environment references inside YAML are not expanded for you.

`WAYLINE_DB_PATH`, `WAYLINE_USAGE_OUTBOX`, `WAYLINE_SESSION_OUTBOX`, and
`WAYLINE_PI_VIEWER_DIR` in the generated environment keep this instance separate. Without
overrides, database/usage/session data live under `~/.local/share/wayline/`, the database is
`wayline.db`, and the viewer mirror is `pi-viewer` beside it. Use the generated environment
instead of relying on shared defaults. `serve --db-path` can override the database explicitly.

## Sign into native Pi

Install the tested Pi release with Node.js and npm if `pi` is not already available:

```sh
npm install -g --ignore-scripts @earendil-works/pi-coding-agent@0.78.0
pi
```

For a subscription, use `/login` in interactive Pi and select your provider. For a ChatGPT
subscription, choose **ChatGPT Plus/Pro**. Complete the provider/browser confirmation, then
use `/model` to select an available model. Pi stores and refreshes its native credentials.

For an API-key provider, use Pi's `/login` flow to enter your own provider key, then `/model`
to select that provider's model. Pi also accepts its documented provider environment
variables, such as `ANTHROPIC_API_KEY` or `OPENAI_API_KEY`, inherited by the dispatcher.
Load those privately in the shell that starts dispatch. Keep keys in Pi's native credential
store or your private environment; do not add them to Wayline YAML, workflow files, or Git.
See [Pi's provider guide](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/providers.md)
for other providers and their native setup. Wayline adds no account or credential format.

Check `pi --version` and `pi --offline --list-models` locally. They prove the CLI and catalog are
available, not that an authenticated model request succeeds. By default the template keeps
Pi's configured model selection. To pin a model, set worker `provider`, `model`, and optional
`reasoning_effort` using exact entries from your own catalog. An unavailable or ambiguous
explicit model fails; Wayline does not substitute another model. The Luna configuration and
historical access proof in [Pi sessions](pi-sessions.md#explicit-models) are specific to the
recorded setup and do not establish access for another account.

## Start and preflight

Run readiness checks from a prepared shell after configuring native Pi:

```sh
uv run --project wayline wayline doctor --config "$HOME/.local/share/wayline-local/dispatch.yaml"
```

Doctor checks the installed package/resources, configuration, Pi availability, offline catalog,
and optional viewer. It makes zero model calls and reports `live_auth_verified: false`.
Credential presence, where reported, does not prove that credentials are valid or the selected
model can be called. A missing optional viewer can be addressed later. Resolve required
configuration, executable, or exact-model errors before dispatching.

Start the server in one prepared shell:

```sh
uv run --project wayline wayline serve --host 127.0.0.1 --port 8000
```

In another prepared shell, start live dispatch:

```sh
uv run --project wayline wayline dispatch --config "$HOME/.local/share/wayline-local/dispatch.yaml"
```

The lower-level `dispatch --startup-healthcheck-only` check remains available. In local mode
it probes CLI availability with `--help`; it does not check credentials or make model calls.
An explicit model is checked against the offline catalog before its model-bearing invocation.

Open [the local dashboard](http://127.0.0.1:8000). The server API is also at `/openapi.json`.
The server alone creates no worker activity; a running dispatcher picks up active goals.
Dispatch may consume your provider allowance and run tools with your local permissions.

## Run and inspect the sample workflow

The [sample definition](examples/sample-workflow.yaml) needs no input files or network access.
It writes an exact answer, then checks it and produces a report. Validate it offline first:

```sh
uv run --project wayline wayline workflow create --file docs/examples/sample-workflow.yaml --dry-run
```

Once your own Pi login is ready and dispatch is running, create it:

```sh
uv run --project wayline wayline workflow create --file docs/examples/sample-workflow.yaml --server http://127.0.0.1:8000
```

Creation returns the workflow ID and stage detail. It starts active work and is not an
idempotent request. If the response is interrupted, inspect the dashboard before retrying.
Open the workflow, read both stage summaries and findings, inspect invocation activity, and
download **Answer** and **Check report**. Confirm the downloaded answer contains exactly
three bytes, `42` and a newline. Its SHA-256 is
`084c799cd551dd1d8d5c5f9a5d593b2e931f5e36122ee5c793c1d08a19839cc0`.

The second stage's `kind: review` is a display label. Independent completion verification is
still off by default. Add `verification_enabled: true` to a stage to require a separate fresh
agent assessment, as described in [Completion verification](verification.md). A completed
worker report, downloaded file, and independent correctness check are different evidence.
A missing viewer build does not prevent this sample from running.

For your own workflows, use [Writing workflows](workflow-authoring.md) and
[Agent CLI and API](agent-creation.md). Definitions do not upload input files. Place any
inputs in the managed workflow workspace before dispatch starts, or describe a permitted
input acquisition step explicitly. Do not put provider secrets in goals or artifacts.

## Optional full Pi conversation viewer

Build the pinned read-only viewer:

```sh
python3 scripts/build-pi-web.py
```

Restart the server after changing viewer configuration. The default binary is
`~/.cache/wayline/pi-web/<pinned-commit>/pi-web`; `WAYLINE_PI_WEB_BINARY` can select an absolute
path to another build. It must match the server's operating system and architecture. A macOS
binary cannot run inside a Linux server container. The server starts a loopback child on
demand when you open a saved conversation. [Pi sessions](pi-sessions.md) documents the pin,
read-only restrictions, retained transcript bounds, build-tool advisories, and failure states.
The viewer receives mirrored managed sessions and no native worker authentication.

## Stop, restart, back up, and upgrade

Pause a workflow in the UI before stopping a long-running installation. Stop the dispatcher
with Ctrl-C, then stop the server with Ctrl-C. For a restart, reuse the same configuration,
database, workspaces, outboxes, and session mirror. Start the server first, then the dispatcher.
Interrupted attempts remain visible; durable captured events and usage replay from the outboxes.
Bytes never captured before a crash cannot be recovered. Inspect state before retrying work
that could already have had external effects.

For a consistent backup, stop both processes and copy the entire instance directory. Pi's
private login remains separate and should be managed with your own credential backup policy.
The instance contains prompts, transcripts, artifacts and possibly sensitive worker output;
protect it accordingly. Trash retains data and is not permanent erasure.

Before upgrading, stop processes, make a backup, and ensure the checkout has no unrelated
local changes. Then update the source and lockfile environment:

```sh
git pull --ff-only
uv sync --frozen --project wayline --python 3.13
python3 scripts/check-install.py
```

Rebuild the optional viewer when its pin or patch changes. Resume using the same instance
layout. Keep the backup if rollback is needed; do not run an older binary against a database
already changed by a newer schema without restoring the matching backup.

The Cairn-to-Wayline rename is a clean deployment change. Old `CAIRN_*` variables and default
directories are not imported automatically. An old database can be selected explicitly with
`--db-path`, but that alone does not migrate its session mirrors, session files, workspace
references, or outboxes. Use fresh Wayline data unless you have separately planned and
verified a complete migration. Keep existing Cairn data intact.

## Permissions and current limits

The server defaults to loopback and provides no multi-user authentication boundary. Do not
publish it directly to an untrusted network. Local workers run as your account with its tools,
network, files and provider configuration. The managed Pi adapter disables auto-loaded
extensions, skills, prompt templates, themes and context files, and explicitly enables its
read/write/edit/bash/grep/find/ls tools. A managed working directory is file
organization, not a sandbox. Use a dedicated account or isolated host if the work requires it.

A definition's success criteria must state permitted actions and required evidence. Artifact
capture checks file presence, bounds and hashes; it does not establish semantic correctness.
Independent review is optional and fallible. CLI usage counters are reported coverage, not
measured provider billing. See [Status and limits](../STATUS.md) for the remaining boundaries.
