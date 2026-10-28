# Independent completion verification

Verification is optional and off by default. Enable **Require another agent to verify completion** when creating a standalone goal or workflow stage. The machine-readable JSON and YAML creation schemas expose the same `verification_enabled: true` field. Generated follow-up goals inherit their discovery stage's verification setting. A worker cannot turn it off in its proposals. Existing definitions and workers keep their current behavior when the field is absent.

The [historical Luna pilot](benchmark-pi-luna-results-2026-10-27.md) is separate from deterministic verification tests. Fabricated quotations in evaluator fixtures are deliberately injected regression cases. They are not observed model misconduct.

## Completion and review

With verification enabled, a producer's `/complete` request creates a durable pending claim. The project remains active and exposes `pending_review_id`. Its goal is not completed, dependent stages remain blocked, and proposed follow-up goals are not created yet. The server requires all declared artifacts to be uploaded and all open exploration intents to finish before accepting the claim. It validates proposed goals before storing the claim.

A claim freezes the completion description, source fact IDs, proposed goals, producer identity, facts, hints and artifact records including hashes. Each claim keeps an immutable copy of its artifact bytes and its own download URLs. Later correction attempts cannot rewrite the earlier claim evidence. Producer work and artifact replacement are blocked while the claim is pending. Later hints do not rewrite that snapshot.

The dispatcher gives the claim to a fresh verifier agent. It uses a new execute call and does not resume the producer's CLI session. The verifier reads the goal and frozen evidence, inspects workspace files, and may run relevant tests. Its response must contain a decision, specific feedback and evidence. The same model and configured worker can serve both roles sequentially; they are different agent runs and sessions.

Reviews use the existing `reason` worker capability. A configuration with one Luna worker can produce a result and review it afterward. There is no extra required worker type or YAML configuration. Reviews count toward worker, project and dispatcher concurrency limits. The ordinary usage recorder records their `review_execute` invocations, including failed attempts.

Approval atomically records the review, completes the goal, applies the stored generated goals and unlocks dependencies once. A replay of the same decision is idempotent. Rejection keeps the goal active and adds an exploration intent with actionable feedback. Correct that work before submitting another claim.

## Identity, leases and pauses

Completion requests include `producer_agent_id` and `producer_session_id` when verification is enabled. The dispatcher supplies them, not model output. Each reviewer has its own `agent_id`, `session_id` and configured `worker` label. IDs represent an agent run and session. Drivers with a session ID available supply it; otherwise the dispatcher uses a unique logical run/session ID. The decision also records `reviewer_cli_session_id` when the driver extracts an actual CLI session or thread ID. A matching producer CLI session is rejected. These are audit identities, not authentication credentials. The fresh CLI execute call is what prevents producer context reuse.

The server rejects reviews whose agent ID or session ID matches the producer. Claiming a review returns an opaque lease token. Heartbeats, release and decisions must match the current reviewer identity and token. Public review reads omit the lease token. Expired or replaced tokens cannot decide a claim. Pausing a goal or workflow invalidates its review lease; resuming requires a fresh claim. Trash also invalidates leases and rejects review writes.

The server permits at most three review execution attempts for a pending claim before pausing the runtime on the next claim request. Timeouts, malformed responses, process failures and interrupted dispatchers consume this durable budget. An expired attempt can be reclaimed after 120 seconds. Heartbeats run at most 20 seconds apart. Explicit resume resets the execution budget. Three rejected completion claims also pause automatic correction. Explicit resume resets that rejection budget while preserving all review history.

## Reviews page and API

The **Reviews** navigation item lists paginated summaries of pending, approved and rejected claims with project links, producer/reviewer identities, evidence, feedback and frozen context. Full completion and frozen context load only when you open a claim. A goal awaiting review has a separate label from ordinary active work. Pending reviews remain visible while a project is paused; resume the project or workflow to continue.

The API endpoints are `GET /reviews`, `GET /reviews/{id}` and `POST /reviews/{id}/claim`, `/heartbeat`, `/release`, `/decision`. The claim endpoint takes `agent_id`, `session_id`, and `worker`. Heartbeat and release additionally require `lease_token`. Decisions require those fields plus `decision` (`approved` or `rejected`), `feedback`, and `evidence`.

The API does not provide a human button that impersonates an agent review. Without verification, `/complete` continues returning the ordinary completion intent. With verification, it returns the completion claim.

Verification adds a second assessment; it does not guarantee correctness. Review quality depends on the goal, inspectable evidence, tests and reviewer judgment. Deterministic tests exercise the state transitions and runner behavior without invoking live models or research targets.
