# UI review of retained Luna runs, 2026-10-28

This review used SQLite backups and copied Pi viewer files from the retained
`~/.cache/cairn/pi-luna-matrix-20261027/attempts/` runs. No model calls were made.
Original benchmark databases, transcripts and worker results were not changed.
These are actual Luna runs on synthetic benchmark tasks, not seeded UI fixtures.

## Verified in the browser

- Completed needle workflow: stage completion, earlier findings, saved sessions and
  collected answer download. The downloaded answer was 144 bytes with SHA-256
  `1aeac8743771a1c226215e1f82b8256fa44d18385c3ea255e00a962eb8b75880`, matching its stored artifact.
- Needle graph at 390 pixels: fact tap selected `f001`, edge tap selected `i001`,
  and linked sessions showed the associated bootstrap invocation. Desktop layout
  returned to a horizontal graph and detail panel.
- Needle invocation tail: actual captured events 190 through 239 now show one complete
  assistant message and `agent_end`. Raw mode retains all 50 events. An independent
  local harness replay of these exact retained events confirmed the grouping.
- Capped inventory workflow: `i001` showed bootstrap only; `i002` showed explore only;
  `f001` showed bootstrap as producer and reason as consumer; `origin` showed all three.
  Process failure and transcript delivery remained separate states.
- Capped inventory usage: two ledger records, three known Pi invocations and one
  missing usage record. Opening the gap selected the exact failed explore invocation
  `92f5c06b-1acb-4e26-87d4-42505c02429d`. Its missing tokens were not added to the
  39,022-token native subtotal. The stale reporter count remained a last reported value.
- Completed research standalone: graph completion identified the worker report with
  independent verification off. The saved 24.5 KB transcript remained available even
  though activity capture stopped at its limit of 760 events. The read-only Pi viewer
  showed the actual answer and report writes plus the final completion message.

## Changes and checks

Activity now groups message fragments and tool updates, while keeping raw events
available. Sequence windows, activity truncation, terminal error messages and the
saved conversation link remain explicit. Phone invocation selection collapses after
selection; graph links and session IDs remain in a disclosure. Phone graphs stack
above details, and header controls wrap. Phone layout does not replace the saved
desktop panel width.

The focused UI behavior suite covers live partial text and tool output, failures,
repeated end markers, paging back to latest, stale async responses, missing-usage
navigation and counts, and desktop width retention. Run it with:

```sh
uv run --project cairn --group dev pytest cairn/tests/test_agent_session_ui.py
```

Standalone output files are not collected server artifacts. Their presence in a saved
conversation is evidence of the recorded tool result, not a native artifact download
or independent outcome verification. Pi dollar estimates are not measured billing.
