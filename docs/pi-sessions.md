# Pi sessions and live activity

Pi is the primary worker in `dispatch.local.example.yaml` and `dispatch.example.yaml`.
Wayline's dispatcher still owns claims, graph writes, cancellation, runtime limits, artifacts,
review gates, and ranked-choice recovery. The viewer cannot control a worker.

Open **Agent sessions** from a workflow, stage, project, fact, or intent edge. The list retains
finished, failed, cancelled, timed-out, and interrupted invocations as well as current work.
Selecting a graph record filters through stored IDs, including source facts and accepted
result facts or intents. Earlier choice attempts keep their original project/stage association.
Pause and recoverable Trash retain this history.

Activity groups streamed message fragments into the latest message preview and combines each
tool's start, partial output and finish. **Show raw events** restores the captured event order.
The sequence range identifies the loaded window, which retains at most 100 raw events;
**Load earlier activity** and **Latest activity** move through it. This is a bounded activity
preview, not full conversation history. Open the saved Pi conversation for that history.
Capture limits and unavailable bytes remain explicit. On phones the invocation list collapses
after selection and can be reopened with **Choose another invocation**. Graph links and the
three session identifiers remain under **Graph links and session IDs**.

Each process has an invocation ID matching its usage-ledger UUID. Execute and same-session
conclude share one Wayline conversation ID, but have separate invocation records and events.
The original Pi header ID is stored separately and shown as **Pi session ID**. Wayline never
rewrites the header, message IDs, timestamps, or content to make those IDs agree.

## Build and run the read-only viewer

Follow [Installation](installation.md#optional-full-pi-conversation-viewer) for prerequisites
and build/start commands. This page owns the viewer contract and evidence boundaries.

The build checks out [pi-web](https://github.com/ygncode/pi-web) at
[`4a77ea5d662b2ef4fa20563b90dac823581614b7`](https://github.com/ygncode/pi-web/tree/4a77ea5d662b2ef4fa20563b90dac823581614b7),
applies the checked-in [read-only patch](../vendor/pi-web/readonly.patch), builds its frontend
from the upstream lockfile, runs the billing-display component and route-denial tests, and
builds its Go executable.
The upstream MIT [license](../vendor/pi-web/LICENSE) is retained. Nothing installs a Pi
extension, launch agent, global service, or Tailscale configuration. npm lifecycle scripts are
disabled during dependency installation. Rebuild after changing the pin or patch.

By default the executable is `~/.cache/wayline/pi-web/<commit>/pi-web`.
`WAYLINE_PI_WEB_BINARY` on the server can select another locally built copy. A missing build
returns a clear 503 when its conversation link is opened; captured activity/history remains
available in Wayline.

The binary must match the server's operating system and architecture. A containerized server
needs a Linux build copied or mounted inside that server and selected with
`WAYLINE_PI_WEB_BINARY`; the existing Python application image does not bundle Go/Node or
a viewer executable. The local build used for browser checks is a macOS binary.

The server starts a child viewer on demand, bound to a random loopback port, and stops it
with the Wayline server. The child receives a separate HOME/agent directory and no worker
authentication environment. It reads only Wayline's mirrored session files. The default directory
is `pi-viewer` beside the server SQLite database. `WAYLINE_PI_VIEWER_DIR` can override it, but
host Pi configuration, credential files, unrelated session directories, and symlinks are rejected.

The original pi-web frontend uses root URLs. Wayline reserves `/session`, `/events`, selected
`/api/*` GET routes, `/custom-themes.css`, and `/static/assets/*` for an allowlisted same-origin
proxy registered before Wayline's static mount. Wayline's dashboard `/` and existing `/settings`
API retain their behavior. Transcript links open a separate tab, including on phones, and
**Back to Wayline** performs a full navigation to the dashboard. No JavaScript URL rewriting is
used. The patch disables service-worker registration on the shared Wayline origin and removes
upstream's interactive break/bedtime component, which otherwise blocks transcript input at night.

Both the gateway and child reject writes, new/forked sessions, chat, model loading, commands,
external sharing, updates, and arbitrary host-file routes. Autonomous title generation,
queues, schedules, model-cache warmup, update checks, and Tailscale setup are disabled.
Write-only controls are hidden, chat is disabled in the response, and the upstream recent-file
mtime fallback cannot claim a worker is running. Wayline's invocation records own runtime state. Viewer SSE connections send five-second
comment keepalives, use a finite ten-second upstream read timeout, and explicitly close
the upstream response on browser disconnect so idle tabs cannot strand the server child.
The viewer remains a local source viewer; it is not an independent authentication system.

The read-only viewer shows **Billing: Not measured** instead of dollar estimates in the
session header and Usage sheet. Custom catalog prices, including zero prices, do not
establish subscription charges or credit consumption. Token displays and original JSONL
cost metadata remain unchanged; the viewer does not invent billing rates.

## Frontend dependency audit

The pinned frontend lockfile was audited on 2026-10-27. The complete development install
reports 12 affected packages, including nine high and three moderate package ratings.
These advisories remain in the pinned build/test toolchain; this is not a clean full audit.
The affected packages are `@vitest/mocker`, `brace-expansion`, `devalue`, `nanoid`, `postcss`,
`postcss-selector-parser`, `smol-toml`, `source-map-js`, `undici`, `vite`, `vitest`, and `zod`.

The same lockfile's `npm audit --omit=dev` reports zero advisories. Because development flags
alone do not prove what a browser receives, both production builds were also inspected using
Vite's emitted chunk module lists. None of those 12 packages occurs in either the 1,068-module
application build or 156-module export build. Svelte's browser runtime is present; the flagged
`devalue` dependency is imported by Svelte's server renderer/hydration serializer and is absent
from these client builds. The Go server embeds static frontend output and runs no Node/Vite,
Vitest, jsdom, or knip process. The builder does not start their development servers or process
worker sessions as build input. The build-only advisory set is retained explicitly rather than
applying unreviewed dependency upgrades to the pinned viewer. Re-audit the lockfile and emitted
modules when updating the pin or build configuration.

## Capture, bounds, and recovery

Every managed Pi execution uses JSON print mode and an explicit source path:

```text
pi --mode json --session wayline-sessions/<conversation UUID>.jsonl ... -p <prompt>
```

The path is relative to the managed runtime workspace. Conclude resumes that exact file.
Host and Docker backends read it incrementally with directory-descriptor traversal, reject
symlinks and nonregular files, and never search `~/.pi`. A capture thread copies actual bytes
while the process runs and finishes the local copy before runtime cleanup. A changed source
header stops capture and retains earlier bytes instead of mixing conversations.

The dispatcher separately records genuine Pi JSON stdout events as they arrive. These power
the live activity stream, including text/tool progress before Pi flushes a durable message.
They are never converted into fabricated Pi session entries. A first session file can be absent
until Pi has produced its first assistant message. **Process running**, **waiting for transcript**,
**transcript updating**, **saved**, **unavailable**, and **truncated** therefore have different meanings.
Process completion and local source capture do not establish server delivery. Metadata reports
`delivery_status: pending` with raw/event byte targets until every captured byte is acknowledged.
Only then can the UI say **transcript saved**. Selected event streams remain subscribed after
process completion so delayed replay and every final event page can arrive.
Graph refreshes are not presented as activity logs. A dispatcher heartbeat arrives every
five seconds even during quiet model work. After 45 seconds without confirmation, the UI
labels a last-reported running process **unconfirmed**; it does not infer death from silence.
Restart replay records interrupted work without dropping already uploaded bytes/events.

Transport chunks are at most 256 KiB. A conversation retains at most 16 MiB of original JSONL;
each invocation retains at most 4 MiB of JSON events. Oversized events and partial stdout tails
are explicitly marked incomplete. Default event reads return 50 records, capped at 100; the
UI retains at most 100 displayed records and supports earlier pages. Source and event limits
are independent. A cap, missing file, unreadable source, or failed final capture is visible
rather than reported as a complete transcript.

A separate durable dispatcher spool lives under `~/.local/share/wayline/session-outbox`,
partitioned by server URL. Set `WAYLINE_SESSION_OUTBOX` to another persistent directory per
dispatcher. An exclusive advisory lock prevents simultaneous owners of that partition.
The server directory and dispatcher spool both require persistent storage in containers.
The source reader supports Docker, but a live provider-backed Docker session has not been
used as proof for this change. Tests exercise the actual reader script and fake Docker API.

API outages do not change worker results. Copies remain in the spool after workspace cleanup
and replay on later scheduler ticks. Byte offsets, verified overlapping replay, immutable
invocation identity, and delivery receipts make retries idempotent without starving newer
captures. Server replay reconciles fsynced files after a SQLite commit interruption. A restarted
dispatcher marks an unfinished invocation interrupted; bytes/events never captured before a
hard crash cannot be recovered. Disk failure can still prevent persistence. There is no
automatic history expiry or permanent deletion in this change.

Raw message content is separate from the metrics-only usage ledger. Usage parsing, cache-read
and uncached-input distinctions, coverage, claim rules, review identity checks, and runtime
caps keep their existing behavior. Non-Pi legacy invocations have usage records but no invented
session transcript. There is no new Claude viewer.

## Explicit models

Local Pi reuses its configured credentials. Optional `provider`, `model`, and
`reasoning_effort` worker fields select an exact model and Pi thinking level. Before an explicit
local model executes, an offline bounded `pi --list-models` check requires one exact catalog
match. The guard executes that exact matched provider/model pair using Pi's canonical
reference encoding, rather than letting Pi reinterpret the original selector against unrelated
configured or builtin providers. Fuzzy-only, ambiguous, or unavailable IDs fail visibly. Omitted fields retain Pi defaults.
Container Pi uses its isolated `wayline` provider configuration, with `model` overriding `PI_MODEL`.

## Historical Luna setup, 2026-10-27

This records one operator-local setup under the former Cairn name. Its account access and
local evidence are not distributed or prerequisites for another installation. Use your own
native Pi login and catalog as described in [Installation](installation.md#sign-into-native-pi).

Pi 0.78.0 could add the exact Luna ID to its built-in `openai-codex` provider through
`~/.pi/agent/models.json`. This keeps Pi's native ChatGPT subscription OAuth flow and endpoint.
A custom catalog entry proves configuration only. Successful authenticated requests and
measured response usage must establish live access separately.

```json
{
  "providers": {
    "openai-codex": {
      "models": [{
        "id": "gpt-6-luna",
        "name": "GPT-6 Luna",
        "reasoning": true,
        "thinkingLevelMap": {"off": null, "minimal": null, "low": "low"},
        "input": ["text", "image"],
        "contextWindow": 272000,
        "maxTokens": 32768
      }]
    }
  }
}
```

Merge that entry with existing providers rather than replacing the file. The context value
comes from the local Codex model catalog; the output value is a conservative catalog setting,
not a measured server limit or an enforced request budget. No `apiKey`, custom endpoint, token copy, or fallback provider
belongs in this configuration. Pi stores OAuth in its own private `~/.pi/agent/auth.json`;
`/login` then **ChatGPT Plus/Pro** establishes or repairs that login, and native Pi refreshes
its own credentials. Do not commit or include auth files in benchmark captures.

Use [the Pi benchmark template](../benchmarks/dispatch.pi-luna.local.yaml), replacing its
workspace root with a new isolated directory, then run the routing preflight:

```sh
uv run --project wayline python benchmarks/preflight.py --config /absolute/path/to/dispatch.yaml
pi --offline --provider openai-codex --model openai-codex/gpt-6-luna --thinking low \
  --no-extensions --no-skills --no-prompt-templates --no-themes --no-context-files \
  --mode json --session /absolute/path/to/proof-session.jsonl -p 'Your bounded test prompt'
```

Use the same session path on a second print invocation to verify resume. Retain raw JSONL,
provider error events, tool results and response IDs. Pi's `message.model` is populated from
the selected local model, so label it configured unless independently observed response
metadata supplies the server model. Pi usage `input` excludes `cacheRead`; inclusive input
is their sum. Pi can exit zero with `stopReason: "error"` and zero counters after an auth
failure. Those counters do not establish measured zero usage or a successful invocation.
The preflight makes no model calls and deliberately reports live access unverified.

On 2026-10-27 the local Pi 0.78.0 setup passed an authenticated tool-and-resume smoke test
using its own native OAuth login. Two print invocations read a fixture, wrote the computed
answer, resumed the same session and read that answer. A separate minimal request captured
HTTP 200 and server `response.completed.model: "gpt-6-luna"`, with request reasoning `low`.
Across these three CLI invocations, six provider responses measured 4,202 input tokens,
107 output tokens and zero cached input tokens. These are setup measurements, not benchmark
results or billing amounts. Two earlier authentication failures remain recorded separately.
Raw nonsecret events and the receipt are retained locally under
`~/.cache/cairn/pi-luna-proof-20261027T2230/`, with totals in `summary.json`.

Pi's current [SDK](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/sdk.md)
and [RPC protocol](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/rpc.md)
are independent alternatives for future integrations. This implementation keeps Python's
process boundary and does not require a core rewrite.

## API and verification

- `POST /agent-sessions` registers or updates one bounded invocation record.
- `POST /agent-sessions/{id}/chunks` and `/event-chunks` append base64 bytes at an explicit offset.
- `POST /agent-conversations/{id}/results` records accepted graph IDs after writeback.
- `GET /agent-sessions` filters by workflow, project, intent, or fact with bounded pages.
- `GET /agent-sessions/{id}/events` reads selected events by sequence cursor.
- `GET /agent-sessions/{id}/stream` sends real changes through SSE and comments for keepalives.
- `GET /session?id=<Wayline conversation UUID>.jsonl` opens the pinned read-only viewer.

Tests cover real harmless host subprocesses, live file append, source identity, cancellation,
final capture before cleanup, outbox restart/outage replay, overlap/crash recovery, exact-model
selection, source/result filters, old choice attempts, paused/Trash retention, bounds,
unknown sources, callback failures, viewer scope, and static-route ordering. Browser fixture
checks cover desktop and phone layouts, actual SSE append, final state, and same-origin pi-web.
Fixtures are labeled and do not represent a model run or independent research result.
