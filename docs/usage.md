# Token usage

The separate **Token usage** screen shows CLI-reported token counts by model and invocation. Filter by UTC time window or project, inspect reporting coverage, and open recent attempts. It does not estimate tokens from prompt size or calculate billing costs.

Each worker process gets one UUID before it starts. Bootstrap, reasoning, exploration, conclude fallbacks, review tasks, and retries are separate attempts, including invalid agent output, failed starts, cancellation, and timeouts. Metrics emitted before a failure are retained with partial coverage. Requests made outside these worker processes, including provider health probes, are outside this ledger.

## Counts and coverage

- **Input** includes fresh input, cache reads, and cache writes.
- **Cached input** means cache reads.
- **Uncached input** includes fresh input and cache writes. Cache writes also have their own column and must not be added to input again.
- **Output** is reported output tokens. Any reasoning tokens included by the CLI remain in that CLI's output count; no extra estimate is added.

Unknown fields are `null`, while an actual reported zero stays `0`. If a CLI reports input but omits cache reads, uncached input remains unknown. A model name is labeled `reported`, `configured`, or `unknown`; host CLI defaults are not guessed. A configured name is the invocation's explicit selection, not proof that a provider resolved that alias unchanged.

`reported` means all four main fields were available: input, cache reads, uncached input, and output. Cache-write reporting is tracked independently. `partial` means some counters or invocation coverage are unavailable. `unreported` means no token counters were available. Totals are sums of available reported counters, with a report count for each metric; they are not a claim of complete account usage. One invocation can contain multiple model readings, such as Claude subagents. Model-row invocation counts therefore need not add up to the total number of invocations.

## CLI support

| CLI | Captured data | Scope and limits |
| --- | --- | --- |
| Codex | `exec --json` and `exec resume --json`; new `turn.completed`/`turn.failed` usage only | Input already includes cache reads. Uncached input is input minus reported cached input. Session snapshots are ignored. Missing cache-write counts stay unknown. The final `agent_message` is still the agent result. |
| Claude Code | `--output-format stream-json --verbose`; result `modelUsage`, including subagents | API input excludes cache reads and writes, so these are added for total input. From version 2.1.277, resumed model counters are cumulative: a durable per-session baseline produces invocation deltas. Without a baseline, or with an unknown resumed version, only available per-turn main-loop usage is counted and coverage is partial. Interrupted assistant streams can recover deduplicated input/cache counters; placeholder output counts are ignored. A session reset inside an invocation is marked partial. |
| Pi | Finalized assistant `message_end` usage; wrapper events are deduplicated | Input, cache reads, cache writes, and output come from Pi's finalized message usage. New process events are counted, not restored session history. Error/aborted all-zero defaults are treated as unreported. Actual `responseModel` takes precedence over the configured model alias. |
| Mock | Invocation records only | No synthetic token counts are represented as actual usage. |

These are CLI telemetry readings, not an independent provider invoice. Future CLI format changes may reduce coverage; they must not create invented values.

Format references: [Codex non-interactive JSON events](https://learn.chatgpt.com/docs/non-interactive-mode), [Claude headless output](https://code.claude.com/docs/en/headless), [Claude usage and cost tracking](https://code.claude.com/docs/en/agent-sdk/cost-tracking), and [Anthropic cache accounting](https://platform.claude.com/docs/en/build-with-claude/prompt-caching). Pi fields and event sequencing were checked against the installed `@earendil-works/pi-coding-agent`, `pi-agent-core`, and `pi-ai` sources; see [Pi source](https://github.com/badlogic/pi-mono).

## Persistence and reporting health

Sanitized attempts are stored in the server SQLite database. No prompts, raw stdout, raw stderr, session contents, credentials, or local paths are stored in that ledger. Usage notes are bounded codes. Session baseline keys are hashes, with only normalized model counters retained locally.

The dispatcher first persists a running marker, then atomically stores the finished attempt and any Claude baseline in a local SQLite outbox. The default is `~/.local/share/cairn/usage-outbox.db`; set `CAIRN_USAGE_OUTBOX` to choose another durable path. The shipped Compose dispatcher mounts `./datas/dispatcher/` at that default directory, separately from server storage. Keep that data across dispatcher replacement to preserve queued reports and resumed Claude baselines.

Each dispatcher must use a separate outbox database path. A lifetime advisory lock on the resolved database path prevents a second process from sharing it, including containers that both report PID 1. Ownership is checked before startup health probes and again before worker execution; a collision stops startup with a `CAIRN_USAGE_OUTBOX` configuration error. Multiple threads and helper instances in the owning process reuse the same lock. The operating system releases ownership on exit or crash, so the next owner can safely recover prior running markers. Do not delete or replace the `.lock` sidecar while a dispatcher is alive. Use a filesystem that supports advisory locks; separate paths and durable mounts are required when running multiple dispatchers.

At most three queued events are sent per replay pass, at process completion and each dispatcher tick. Transient failures retain the original UUID. Replaying an identical UUID/payload is idempotent; conflicting payloads return 409. Permanently rejected events remain quarantined locally and are reported as rejected instead of retrying indefinitely. Reporting errors are logged and do not change the worker's result. An ownership collision is a startup/configuration error and prevents worker execution.

An interrupted dispatcher leaves an unreported attempt with no fabricated completion time. A hard crash cannot recover counters never persisted by the CLI capture. Disk failure can prevent persistence; this is logged explicitly, so the dashboard is not proof against all data loss.

The screen shows the latest reporter state: pending, running, rejected, and last seen. These states are global, independent of the selected date/project filter. Missing or stale reporter health means transfer coverage is uncertain. A healthy empty queue describes known queued events, not provider-wide completeness.

## API

- `POST /usage/attempts`: sanitized invocation record; UUID/payload idempotency.
- `GET /usage/summary?since=<ISO8601>&until=<ISO8601>&project_id=<id>`: model totals, per-metric report counts, invocation coverage, phase counts, and latest global reporter health. Timezones are required; `since` is inclusive and `until` exclusive, applied to invocation start time.
- `GET /usage/attempts` accepts the same filters plus `offset` and `limit` (default 25, maximum 100). Follow `next_offset` until it is `null`.
- `POST /usage/reporters`: sanitized current outbox health.

The attempt ID identifies a process invocation, not a source result or a model reading. Benchmark reconciliation should count attempts once and group the nested `readings` separately. Use `reasoning_effort` when explicitly selected; unknown effort stays unspecified. Ledger rows intentionally have no raw-log pointer.

## Verification

Usage tests exercise realistic CLI fixtures, cumulative resume deltas and missing baselines, multi-model totals, zero versus unknown, duplicate event replay, date/project filters, bounded pagination, rejected reports, interrupted attempts, exclusive ownership across independent processes with identical PID markers, crash/restart lock recovery, multithreaded helper reuse, and actual local subprocesses across execute/conclude/retry phases. They also verify that usage metadata does not replace structured agent output. These tests use synthetic CLI streams and do not call paid models.
