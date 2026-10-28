# Pi Luna comparison pilots, 2026-10-27 UTC

Generated from actual per-attempt receipts. Exact model `gpt-6-luna`, low effort, Pi 0.78.0 with the existing OpenAI subscription OAuth account.

Report generated 2026-10-27T23:04:20.912066+00:00. Receipt timestamps are Unix UTC. Original engine `8e7e0ea67552383851dfcabfba0c4e9c8d007878`; fork engine `f0abdd35e63d649dcfcf36a7abc7d540ef21a0ef`. Both used the common transport and original phase templates, two global workers, two running projects, two workers per project, and the same 15-minute / 24-request / 50,000-token budgets.

| Case / mode | Outcome | Admitted requests / CLI | Input / cached subset / output | Seconds | Usage complete |
| --- | --- | ---: | ---: | ---: | --- |
| needle / original | completed; checker pass | 5 / 1 | 12021 / 8704 / 306 | 7.93 | true |
| needle / standalone | failed | 0 / 0 | 0 / 0 / 0 | n/a | n/a |
| needle / standalone replacement | completed; checker pass | 4 / 1 | 10449 / 5120 / 350 | 8.74 | true |
| needle / workflow | completed; checker pass | 8 / 2 | 23813 / 13184 / 604 | 17.93 | true |
| research / original | completed; checker pass | 8 / 1 | 29628 / 23552 / 1916 | 22.13 | true |
| research / standalone | completed; checker pass | 6 / 1 | 24831 / 18048 / 1955 | 19.43 | true |
| research / workflow | token_limit; checker fail | 3 / 1 | ≥51638 / ≥26880 / ≥170 | 6.17 | false |
| inventory / original | completed; checker fail | 8 / 1 | 24238 / 14720 / 1669 | 22.84 | true |
| inventory / standalone | completed; checker pass | 6 / 1 | 19690 / 11648 / 1808 | 21.33 | true |
| inventory / workflow | token_limit; checker fail | 15 / 3 | ≥52845 / ≥37376 / ≥1399 | 26.32 | false |

Actual model attempts: 9. Strict checker passes: 6. All usage reconciled: false.

Known pilot usage totals 63 admitted provider requests across 12 CLI calls and 64 observed assistant turns. Input 249153 includes 159232 cached tokens, with 10177 output tokens. These are lower bounds because two capped attempts have incomplete accounting.

## Observed failures

The original inventory answer had correct service fields and verbatim JSON citations, but its report omitted six required direct Markdown source links. It remains a strict citation-presentation failure.

The research workflow stopped during discovery after an unsolicited passive host socket diagnostic returned 203,272 bytes. Pi supplied a 51,265-character truncated tail to later requests. Its measured input rose sharply and the attempt reached 51,808 known tokens, including a 1,808-token overshoot. The private host dump remains outside the repository. This single off-task detour does not establish a systematic workflow cost.

The inventory workflow proposed invalid child definitions, using kind verification and artifact path strings. The server rejected them, then reason and explore recovery ran concurrently. The attempt stopped at 54,244 known tokens, including a 4,244-token overshoot. One admitted provider response and an unfinished CLI remain unreconciled. Both capped workflows produced no final collected answer or report.

The first standalone needle launch failed before API or worker startup because the port preflight treated TIME_WAIT as occupied. Its zero-call failure receipt is retained. The explicit fresh replacement used the same fixtures, prompts and budgets after an infrastructure-only SO_REUSEADDR fix.

Independent agent checks found no evidence of fabricated values or citations in the observed results. Independent verification remains off by default. No human report review was performed.

## Method limits

One attempt per comparison cell; the zero-call infrastructure failure and its explicit fresh replacement remain separate.

Case and mode order was needle, research, inventory; original, standalone, workflow within each case. Shared account and provider caches were not reset.

Common phase templates were byte-identical and taken from the original revision. Fork-specific rendered hints, goal expansion and history adapters are the intended treatment, so full rendered prompts are not claimed identical.

Billing amounts and subscription credit consumption were not measured. Pi catalog dollar estimates, including $0, are not billing evidence.

Measured input includes cached input. Cached tokens are a subset and must not be added again. Missing usage remains unknown; totals are known lower bounds when coverage is incomplete.

Frozen matrix manifests describe state at freeze. They are not current run-status reports.

Independent agent source reviews are recorded in review.json; the root coordinator independently checked all completed outputs and both capped traces.

The local same-user environment does not protect withheld evaluator data. These small synthetic tasks do not establish general research or security quality.

Separate setup receipts are not included in pilot totals. Earlier host OAuth/model setup proof is recorded separately in docs/pi-sessions.md.

## Setup measurements

Separate host and native-driver setup recorded seven completed responses, 5,659 input tokens and 118 output tokens. This includes the 1,468-token smoke through the normal production adapter. The common transport canaries separately recorded four completed responses, 4,902 input tokens and 52 output tokens, including 1,280 cached input tokens. Setup calls and retained failures are excluded from the pilot totals; their receipts remain separate. See docs/pi-sessions.md and the external setup proof bundles.

The per-attempt `evaluation.json` checks structured values and source citations. Report prose requires separate review. A wrong answer alone is not evidence of fabrication.

## Historical local receipts

The tables and conclusions above are the portable receipt summary committed with the project. The following paths identify original operator-local evidence, not files distributed in this repository. They deliberately retain the Cairn name used at measurement time. A fresh clone cannot open these receipts or reproduce the historical model/account state. Raw sessions, authentication material and host diagnostics are not committed.

- `needle-original`: [attempt receipt](/Users/vabole/.cache/cairn/pi-luna-matrix-20261027/attempts/needle-original/attempt.json), [captured artifacts](/Users/vabole/.cache/cairn/pi-luna-matrix-20261027/attempts/needle-original/captured), [actual frozen manifest](/Users/vabole/.cache/cairn/pi-luna-matrix-20261027/attempts/needle-original/frozen-manifest.json).
- `needle-standalone`: [attempt receipt](/Users/vabole/.cache/cairn/pi-luna-matrix-20261027/attempts/needle-standalone/attempt.json), no artifacts created, [actual frozen manifest](/Users/vabole/.cache/cairn/pi-luna-matrix-20261027/attempts/needle-standalone/frozen-manifest.json).
- `needle-standalone-infra-replacement-1`: [attempt receipt](/Users/vabole/.cache/cairn/pi-luna-matrix-20261027/attempts/needle-standalone-infra-replacement-1/attempt.json), [captured artifacts](/Users/vabole/.cache/cairn/pi-luna-matrix-20261027/attempts/needle-standalone-infra-replacement-1/captured), [actual frozen manifest](/Users/vabole/.cache/cairn/pi-luna-matrix-20261027/attempts/needle-standalone-infra-replacement-1/frozen-manifest.json).
- `needle-workflow`: [attempt receipt](/Users/vabole/.cache/cairn/pi-luna-matrix-20261027/attempts/needle-workflow/attempt.json), [captured artifacts](/Users/vabole/.cache/cairn/pi-luna-matrix-20261027/attempts/needle-workflow/captured), [actual frozen manifest](/Users/vabole/.cache/cairn/pi-luna-matrix-20261027/attempts/needle-workflow/frozen-manifest.json).
- `research-original`: [attempt receipt](/Users/vabole/.cache/cairn/pi-luna-matrix-20261027/attempts/research-original/attempt.json), [captured artifacts](/Users/vabole/.cache/cairn/pi-luna-matrix-20261027/attempts/research-original/captured), [actual frozen manifest](/Users/vabole/.cache/cairn/pi-luna-matrix-20261027/attempts/research-original/frozen-manifest.json).
- `research-standalone`: [attempt receipt](/Users/vabole/.cache/cairn/pi-luna-matrix-20261027/attempts/research-standalone/attempt.json), [captured artifacts](/Users/vabole/.cache/cairn/pi-luna-matrix-20261027/attempts/research-standalone/captured), [actual frozen manifest](/Users/vabole/.cache/cairn/pi-luna-matrix-20261027/attempts/research-standalone/frozen-manifest.json).
- `research-workflow`: [attempt receipt](/Users/vabole/.cache/cairn/pi-luna-matrix-20261027/attempts/research-workflow/attempt.json), [captured artifacts](/Users/vabole/.cache/cairn/pi-luna-matrix-20261027/attempts/research-workflow/captured), [actual frozen manifest](/Users/vabole/.cache/cairn/pi-luna-matrix-20261027/attempts/research-workflow/frozen-manifest.json).
- `inventory-original`: [attempt receipt](/Users/vabole/.cache/cairn/pi-luna-matrix-20261027/attempts/inventory-original/attempt.json), [captured artifacts](/Users/vabole/.cache/cairn/pi-luna-matrix-20261027/attempts/inventory-original/captured), [actual frozen manifest](/Users/vabole/.cache/cairn/pi-luna-matrix-20261027/attempts/inventory-original/frozen-manifest.json).
- `inventory-standalone`: [attempt receipt](/Users/vabole/.cache/cairn/pi-luna-matrix-20261027/attempts/inventory-standalone/attempt.json), [captured artifacts](/Users/vabole/.cache/cairn/pi-luna-matrix-20261027/attempts/inventory-standalone/captured), [actual frozen manifest](/Users/vabole/.cache/cairn/pi-luna-matrix-20261027/attempts/inventory-standalone/frozen-manifest.json).
- `inventory-workflow`: [attempt receipt](/Users/vabole/.cache/cairn/pi-luna-matrix-20261027/attempts/inventory-workflow/attempt.json), [captured artifacts](/Users/vabole/.cache/cairn/pi-luna-matrix-20261027/attempts/inventory-workflow/captured), [actual frozen manifest](/Users/vabole/.cache/cairn/pi-luna-matrix-20261027/attempts/inventory-workflow/frozen-manifest.json).
