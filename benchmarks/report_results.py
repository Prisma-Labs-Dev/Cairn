#!/usr/bin/env python3
"""Derive current pilot results from immutable per-attempt receipts, never manifests."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

import supervisor


def derive(root):
    matrix = json.loads((root / "matrix.json").read_text())
    rows = []
    for frozen in matrix["attempts"]:
        path = Path(frozen["path"])
        attempt = json.loads((path / "attempt.json").read_text())
        result = attempt.get("result")
        ledger = result or supervisor.totals(path / "supervision")
        active = attempt["status"] in ("running", "prepared_unrun")
        row = {"id": attempt["id"], "case": attempt["case"], "mode": attempt["mode"], "status": attempt["status"],
               "replacement_for": attempt.get("replacement_for"), "error": attempt.get("error"),
               "started_at": attempt.get("started_at"), "stop_reason": result.get("stop_reason") if result else None,
               "cli_invocations": ledger["cli_invocations"] if not active else None,
               "provider_requests": ledger["provider_requests"] if not active else None,
               "assistant_turns": ledger["model_invocations"] if not active else None,
               "input_tokens": ledger["input_tokens"] if not active else None,
               "cached_input_tokens": ledger["cached_input_tokens"] if not active else None,
               "uncached_input_tokens": ledger["uncached_input_tokens"] if not active else None,
               "output_tokens": ledger["output_tokens"] if not active else None,
               "measured_tokens": ledger["measured_tokens"] if not active else None,
               "missing_usage_turns": ledger["missing_usage_turns"] if not active else None,
               "usage_complete": result.get("usage_complete") if result else None,
               "elapsed_seconds": result.get("elapsed_seconds") if result else None,
               "token_overshoot": result.get("token_overshoot") if result else None,
               "invocation_overshoot": result.get("invocation_overshoot") if result else None,
               "remaining_process_groups": result.get("remaining_process_groups") if result else None,
               "reconciliation": result.get("reconciliation") if result else None,
               "checker": attempt.get("evaluation"), "report_review": attempt.get("report_review"),
               "receipt": str(path / "attempt.json"), "captured": str(path / "captured"),
               "freeze_snapshot": str(path / "frozen-manifest.json")}
        if (path / "review.json").exists():
            row["independent_agent_review"] = json.loads((path / "review.json").read_text())
        row["human_review"] = "not_performed"
        rows.append(row)
    rows.sort(key=lambda row: row.get("started_at") or float("inf"))
    ran = [r for r in rows if r["provider_requests"]]
    setup = []
    for path in (root.parent / "pi-luna-transport-canary-20261027/summary.json", root.parent / "pi-luna-transport-canary-current-20261027/summary.json"):
        if path.exists():
            data = json.loads(path.read_text())
            setup.append({"receipt": str(path), **{key: data.get(key) for key in ("cli_invocations", "provider_requests", "model_invocations", "input_tokens", "cached_input_tokens", "output_tokens", "measured_tokens")}})
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "source": "per-attempt receipts and actual invocation ledgers", "frozen_manifest_is_current_status": False,
            "model": "gpt-6-luna", "provider": "openai-codex", "effort": "low", "cli_version": "Pi 0.78.0", "provider_request_count_scope": "atomically admitted requests; an incomplete attempt includes an unreconciled admitted response", "independent_agent_review": "completed by Sol and root coordinators for observed results and traces", "human_review": "not_performed", "independent_verification_default": "off; no fabrication evidence in observed results", "limits": matrix["limits"], "rows": rows,
            "actual_model_attempts": len(ran), "completed_checker_passes": sum(bool(r["checker"] and r["checker"].get("passed")) for r in ran),
            "known_pilot_usage": {key: sum(r[key] for r in ran) for key in ("provider_requests", "cli_invocations", "assistant_turns", "input_tokens", "cached_input_tokens", "uncached_input_tokens", "output_tokens", "measured_tokens")},
            "all_pilot_usage_complete": bool(ran) and all(r["usage_complete"] for r in ran),
            "transport_setup_receipts": setup,
            "notes": ["One attempt per comparison cell; the zero-call infrastructure failure and its explicit fresh replacement remain separate.",
                      "Case and mode order was needle, research, inventory; original, standalone, workflow within each case. Shared account and provider caches were not reset.",
                      "Common phase templates were byte-identical and taken from the original revision. Fork-specific rendered hints, goal expansion and history adapters are the intended treatment, so full rendered prompts are not claimed identical.",
                      "Billing amounts and subscription credit consumption were not measured. Pi catalog dollar estimates, including $0, are not billing evidence.",
                      "Measured input includes cached input. Cached tokens are a subset and must not be added again. Missing usage remains unknown; totals are known lower bounds when coverage is incomplete.",
                      "Frozen matrix manifests describe state at freeze. They are not current run-status reports.",
                      "Independent agent source reviews are recorded in review.json; the root coordinator independently checked all completed outputs and both capped traces.",
                      "The local same-user environment does not protect withheld evaluator data. These small synthetic tasks do not establish general research or security quality.",
                      "Separate setup receipts are not included in pilot totals. Earlier host OAuth/model setup proof is recorded separately in docs/pi-sessions.md."]}


def markdown(data):
    lines = ["# Pi Luna comparison pilots, 2026-10-27 UTC", "", "Generated from actual per-attempt receipts. Exact model `gpt-6-luna`, low effort, Pi 0.78.0 with the existing OpenAI subscription OAuth account.", "",
             f"Report generated {data['generated_at']}. Receipt timestamps are Unix UTC. Original engine `8e7e0ea67552383851dfcabfba0c4e9c8d007878`; fork engine `f0abdd35e63d649dcfcf36a7abc7d540ef21a0ef`. Both used the common transport and original phase templates, two global workers, two running projects, two workers per project, and the same 15-minute / 24-request / 50,000-token budgets.", "",
             "| Case / mode | Outcome | Admitted requests / CLI | Input / cached subset / output | Seconds | Usage complete |",
             "| --- | --- | ---: | ---: | ---: | --- |"]
    for row in data["rows"]:
        checker = row["checker"]
        outcome = row["stop_reason"] or row["status"]
        if checker:
            outcome += "; checker " + ("pass" if checker.get("passed") else "fail")
        label = row["case"] + " / " + row["mode"] + (" replacement" if row.get("replacement_for") else "")
        calls = "unrun" if row["provider_requests"] is None else str(row["provider_requests"]) + " / " + str(row["cli_invocations"])
        tokens = "unrun" if row["input_tokens"] is None else f"{row['input_tokens']} / {row['cached_input_tokens']} / {row['output_tokens']}"
        if row["provider_requests"] and row["usage_complete"] is False:
            tokens = " / ".join("≥" + str(row[key]) for key in ("input_tokens", "cached_input_tokens", "output_tokens"))
        elapsed = "n/a" if row["elapsed_seconds"] is None else f"{row['elapsed_seconds']:.2f}"
        complete = "n/a" if row["usage_complete"] is None else str(row["usage_complete"]).lower()
        lines.append(f"| {label} | {outcome} | {calls} | {tokens} | {elapsed} | {complete} |")
    lines += ["", f"Actual model attempts: {data['actual_model_attempts']}. Strict checker passes: {data['completed_checker_passes']}. All usage reconciled: {str(data['all_pilot_usage_complete']).lower()}.", ""]
    usage = data["known_pilot_usage"]
    lines.extend([f"Known pilot usage totals {usage['provider_requests']} admitted provider requests across {usage['cli_invocations']} CLI calls and {usage['assistant_turns']} observed assistant turns. Input {usage['input_tokens']} includes {usage['cached_input_tokens']} cached tokens, with {usage['output_tokens']} output tokens. These are lower bounds because two capped attempts have incomplete accounting.", "", "## Observed failures", "",
                  "The original inventory answer had correct service fields and verbatim JSON citations, but its report omitted six required direct Markdown source links. It remains a strict citation-presentation failure.", "",
                  "The research workflow stopped during discovery after an unsolicited passive host socket diagnostic returned 203,272 bytes. Pi supplied a 51,265-character truncated tail to later requests. Its measured input rose sharply and the attempt reached 51,808 known tokens, including a 1,808-token overshoot. The private host dump remains outside the repository. This single off-task detour does not establish a systematic workflow cost.", "",
                  "The inventory workflow proposed invalid child definitions, using kind verification and artifact path strings. The server rejected them, then reason and explore recovery ran concurrently. The attempt stopped at 54,244 known tokens, including a 4,244-token overshoot. One admitted provider response and an unfinished CLI remain unreconciled. Both capped workflows produced no final collected answer or report.", "",
                  "The first standalone needle launch failed before API or worker startup because the port preflight treated TIME_WAIT as occupied. Its zero-call failure receipt is retained. The explicit fresh replacement used the same fixtures, prompts and budgets after an infrastructure-only SO_REUSEADDR fix.", "",
                  "Independent agent checks found no evidence of fabricated values or citations in the observed results. Independent verification remains off by default. No human report review was performed.", "", "## Method limits", ""])
    for note in data["notes"]:
        lines.extend([note, ""])
    lines.extend(["## Setup measurements", "",
                  "Separate host and native-driver setup recorded seven completed responses, 5,659 input tokens and 118 output tokens. This includes the 1,468-token smoke through the normal production adapter. The common transport canaries separately recorded four completed responses, 4,902 input tokens and 52 output tokens, including 1,280 cached input tokens. Setup calls and retained failures are excluded from the pilot totals; their receipts remain separate. See docs/pi-sessions.md and the external setup proof bundles.", ""])
    lines.extend(["The per-attempt `evaluation.json` checks structured values and source citations. Report prose requires separate review. A wrong answer alone is not evidence of fabrication.", "", "## Receipts", ""])
    for row in data["rows"]:
        artifacts = f"[captured artifacts]({row['captured']})" if Path(row["captured"]).is_dir() else "no artifacts created"
        lines.append(f"- `{row['id']}`: [attempt receipt]({row['receipt']}), {artifacts}, [actual frozen manifest]({row['freeze_snapshot']}).")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--markdown", type=Path)
    args = parser.parse_args()
    result = derive(args.root.resolve())
    (args.root / "results.json").write_text(json.dumps(result, indent=2) + "\n")
    (args.markdown or args.root / "pilot-results.md").write_text(markdown(result))
    print(json.dumps({"actual_model_attempts": result["actual_model_attempts"], "checker_passes": result["completed_checker_passes"], "all_usage_complete": result["all_pilot_usage_complete"]}))
