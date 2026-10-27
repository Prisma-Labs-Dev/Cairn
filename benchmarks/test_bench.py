"""Offline fixture/checker tests. No Cairn server or model credentials needed."""
from __future__ import annotations

import argparse
import copy
import importlib.util
import json
from pathlib import Path
import socket
import tempfile
import unittest
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

spec = importlib.util.spec_from_file_location("bench", Path(__file__).with_name("bench.py"))
bench = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bench)


class FixtureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def prepare(self, case):
        args = argparse.Namespace(case=case, seed="checker-tests", base_url="http://127.0.0.1:8765",
                                  workspace=self.root / case / "worker", private=self.root / case / "evaluator")
        bench.prepare(args)
        return args

    def submit_gold(self, args):
        expected = json.loads((args.private / "expected.json").read_text())
        bench.write_json(args.workspace / "answer.json", expected)
        group = "candidates" if args.case == "research" else "services"
        urls = {c["url"] for row in expected.get(group, []) for c in row["citations"]}
        (args.workspace / "report.md").write_text("\n".join(f"[Evidence]({url})" for url in urls))
        return expected

    def test_expected_answers_pass_and_stay_out_of_worker_bundle(self):
        for case in bench.CASES:
            with self.subTest(case=case):
                args = self.prepare(case)
                public_names = {p.name for p in args.workspace.rglob("*")}
                self.assertNotIn("expected.json", public_names)
                self.assertNotIn("manifest.json", public_names)
                self.assertNotIn("workflow.json", public_names)
                self.submit_gold(args)
                self.assertTrue(bench.evaluate(args)["passed"])

    def test_wrong_value_fails(self):
        args = self.prepare("needle")
        gold = self.submit_gold(args)
        gold["scaled_quantity"] += 1
        bench.write_json(args.workspace / "answer.json", gold)
        self.assertFalse(bench.evaluate(args)["passed"])

    def test_missing_citation_and_invented_quote_fail(self):
        args = self.prepare("research")
        gold = self.submit_gold(args)
        for mutation in ([], [{"field": "monthly_eur", "url": "http://127.0.0.1:8765/research/aster/pricing.html", "quote": "Invented"}]):
            bad = copy.deepcopy(gold)
            bad["candidates"][0]["citations"] = mutation
            bench.write_json(args.workspace / "answer.json", bad)
            self.assertFalse(bench.evaluate(args)["passed"])

    def test_full_quote_must_be_verbatim_in_the_cited_source(self):
        for case, group in (("research", "candidates"), ("inventory", "services")):
            args = self.prepare(case)
            gold = self.submit_gold(args)
            for mutation in ("append", "prepend", "wrong_source", "extra_fabricated_citation"):
                with self.subTest(case=case, mutation=mutation):
                    bad = copy.deepcopy(gold)
                    selected = bad[group][0]["citations"][0]
                    if mutation == "append":
                        selected["quote"] += " Fabricated statement that never appears in any source."
                    elif mutation == "prepend":
                        selected["quote"] = "Fabricated statement that never appears in any source. " + selected["quote"]
                    elif mutation == "wrong_source":
                        selected["url"] = bad[group][1]["citations"][0]["url"]
                    else:
                        extra = copy.deepcopy(selected)
                        extra["quote"] += " Fabricated statement that never appears in any source."
                        bad[group][0]["citations"].append(extra)
                    bench.write_json(args.workspace / "answer.json", bad)
                    self.assertFalse(bench.evaluate(args)["passed"])

    def test_larger_verbatim_source_window_is_valid(self):
        for case, group in (("research", "candidates"), ("inventory", "services")):
            args = self.prepare(case)
            gold = self.submit_gold(args)
            selected = gold[group][0]["citations"][0]
            if case == "research":
                body = (args.workspace / "input/public/research/aster/pricing.html").read_text()
            else:
                lab = json.loads((args.private / "lab.json").read_text())
                body = lab["listeners"][0]["responses"]["/identity.html"]
            selected["quote"] = bench.source_text(body)
            bench.write_json(args.workspace / "answer.json", gold)
            self.assertTrue(bench.evaluate(args)["passed"])

    def test_short_verbatim_clause_for_each_field_is_valid(self):
        for case, group in (("research", "candidates"), ("inventory", "services")):
            args = self.prepare(case)
            gold = self.submit_gold(args)
            for row in gold[group]:
                for c in row["citations"]:
                    c["quote"] = bench.supporting_clause(row, c["field"], case)
            bench.write_json(args.workspace / "answer.json", gold)
            with self.subTest(case=case):
                self.assertTrue(bench.evaluate(args)["passed"])

    def test_exact_but_nonsupporting_clause_and_wrong_value_fail(self):
        for case, group in (("research", "candidates"), ("inventory", "services")):
            args = self.prepare(case)
            gold = self.submit_gold(args)
            first = gold[group][0]
            for selected in first["citations"]:
                for mutation in ("wrong_value", "other_field"):
                    with self.subTest(case=case, field=selected["field"], mutation=mutation):
                        bad = copy.deepcopy(gold)
                        changed = next(c for c in bad[group][0]["citations"] if c["field"] == selected["field"])
                        if mutation == "wrong_value":
                            changed["quote"] = bench.supporting_clause(first, selected["field"], case) + "9"
                        else:
                            # Exact text from the same cited page, but it proves no requested field value.
                            changed["quote"] = "Effective 2026-10-01" if case == "research" and selected["field"] in {"monthly_eur", "seats"} else (
                                "Data region:" if case == "research" else "Service ID: alpha")
                        bench.write_json(args.workspace / "answer.json", bad)
                        self.assertFalse(bench.evaluate(args)["passed"])

    def test_report_required_and_fixture_tampering_detected(self):
        args = self.prepare("inventory")
        self.submit_gold(args)
        (args.workspace / "report.md").unlink()
        self.assertFalse(bench.evaluate(args)["passed"])
        self.submit_gold(args)
        (args.workspace / "input/public/targets.json").write_text("changed")
        self.assertFalse(bench.evaluate(args)["passed"])

    def test_actual_loopback_lab_reachable_set_matches_withheld_manifest(self):
        # Reserve and release a candidate block; the lab itself fails if a port is occupied.
        for _ in range(20):
            sockets = []
            try:
                probe = socket.socket()
                probe.bind(("127.0.0.1", 0))
                base = probe.getsockname()[1]
                probe.close()
                if base > 65530:
                    continue
                for port in range(base + 1, base + 5):
                    item = socket.socket()
                    item.bind(("127.0.0.1", port))
                    sockets.append(item)
                break
            except OSError:
                continue
            finally:
                for item in sockets:
                    item.close()
        else:
            self.fail("could not reserve test loopback ports")
        _, expected, _, lab = bench.inventory_lab("network-test", f"http://127.0.0.1:{base}")
        servers, guard = bench.start_inventory(lab)
        try:
            for service in expected["services"]:
                for c in service["citations"]:
                    with urlopen(c["url"], timeout=2) as response:
                        self.assertIn(c["quote"], response.read().decode())
                with self.assertRaises(HTTPError) as error:
                    urlopen(service["origin"] + "/expected.json", timeout=2)
                self.assertEqual(error.exception.code, 404)
                error.exception.close()
            for origin in expected["unreachable"]:
                with self.assertRaises(URLError):
                    urlopen(origin + "/", timeout=2)
        finally:
            for server in servers:
                server.shutdown()
                server.server_close()
            guard.close()

    def test_nested_evaluator_path_rejected(self):
        args = argparse.Namespace(workspace=self.root / "worker", private=self.root / "worker/private")
        with self.assertRaisesRegex(ValueError, "disjoint"):
            bench.prepare(args)

    def test_seed_reproduces_inputs_but_changes_answers(self):
        first = bench.fixture("needle", "same", "http://127.0.0.1:8765")
        self.assertEqual(first, bench.fixture("needle", "same", "http://127.0.0.1:8765"))
        self.assertNotEqual(first[1], bench.fixture("needle", "different", "http://127.0.0.1:8765")[1])

    def test_ledger_counts_failed_calls_and_cached_subset_once(self):
        calls = [{"call_id": str(i), "model": "gpt-6-luna", "effort": "low", "phase": phase,
                  "input_tokens": 100, "cached_input_tokens": 20, "output_tokens": 10,
                  "started_at": start, "finished_at": start + 3, "raw_log": f"call-{i}.jsonl", "status": status}
                 for i, (phase, start, status) in enumerate([("bootstrap", 1, "failed"), ("retry", 2, "success"), ("reason", 6, "success")])]
        ledger = self.root / "calls.jsonl"
        ledger.write_text("\n".join(json.dumps(c) for c in calls))
        summary = bench.summarize(argparse.Namespace(ledger=ledger))
        self.assertEqual(summary["total_tokens"], 330)
        self.assertEqual(summary["wall_seconds"], 8)
        calls[0]["model"] = "host-default"
        ledger.write_text("\n".join(json.dumps(c) for c in calls))
        with self.assertRaisesRegex(ValueError, "gpt-6-luna"):
            bench.summarize(argparse.Namespace(ledger=ledger))


if __name__ == "__main__":
    unittest.main()
