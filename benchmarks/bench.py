#!/usr/bin/env python3
"""Offline benchmark fixtures and withheld evaluation. Never invokes a model."""
from __future__ import annotations

import argparse
import hashlib
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import random
import re
import socket
import threading
from urllib.parse import unquote, urlsplit

MODEL = "gpt-6-luna"
CASES = ("needle", "research", "inventory")


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def citation(field: str, path: str, quote: str, base: str) -> dict:
    return {"field": field, "url": base + path, "quote": quote}


def fixture(case: str, seed: str, base: str) -> tuple[dict[str, str], dict, str]:
    """Return public bytes, private expected answer, and the common final goal."""
    rng = random.Random(seed + ":" + case)
    pages: dict[str, str] = {}

    def page(path: str, title: str, body: str) -> None:
        pages[path] = f"<!doctype html><html><head><title>{title}</title></head><body><h1>{title}</h1>{body}</body></html>\n"

    if case == "needle":
        rows = [(f"R{i:03}", f"sample {rng.randrange(10000):04}", rng.randrange(1, 90)) for i in range(32)]
        index = rng.randrange(len(rows))
        identifier = rows[index][0]
        rows[index] = (identifier, "  Amber   Atlas  ", rng.randrange(10, 80))
        pages["/records.tsv"] = "id\ttitle\tquantity\n" + "".join(f"{a}\t{b}\t{c}\n" for a, b, c in rows)
        canonical = f"{identifier}|amber atlas|{rows[index][2] * 7}"
        expected = {"id": identifier, "normalized_title": "amber atlas", "scaled_quantity": rows[index][2] * 7,
                    "sha256": hashlib.sha256(canonical.encode()).hexdigest()}
        goal = ("Read input/public/records.tsv. Find the single row whose title becomes 'amber atlas' after stripping, "
                "collapsing whitespace and lowercasing. Write answer.json with exactly id, normalized_title, "
                "scaled_quantity equal to quantity * 7, and sha256 of UTF-8 'id|normalized_title|scaled_quantity' "
                "without a trailing newline. This tiny control needs no follow-up goals.")
        return pages, expected, goal

    if case == "research":
        price = rng.randrange(32, 40)
        details = [("aster", price, 60, "EU", True), ("beacon", 35, 80, "US", True), ("cedar", 45, 120, "EU", None)]
        links = "".join(f'<li><a href="{slug}/index.html">{slug.title()}</a></li>' for slug, *_ in details)
        page("/research/index.html", "Synthetic meeting tools directory", f"<ul>{links}</ul><p>Compare all listed candidates.</p>")
        candidates = []
        for slug, monthly, seats, region, export in details:
            root = f"/research/{slug}/"
            page(root + "index.html", slug.title(), '<a href="pricing.html">Current pricing</a> <a href="trust.html">Trust and export</a> <a href="archive.html">Archived launch announcement</a>')
            pricing = f"Effective 2026-10-01: monthly price EUR {monthly}; included seats {seats}."
            trust = f"Data region: {region}. CSV export: " + ("supported." if export else "not documented; ask support before assuming availability.")
            page(root + "pricing.html", slug.title() + " current pricing", f"<p>{pricing}</p>")
            page(root + "trust.html", slug.title() + " trust", f"<p>{trust}</p>")
            old = f"Archived 2025-02-01 launch offer: monthly price EUR {monthly - 10}; included seats 30. Superseded by current pricing."
            page(root + "archive.html", slug.title() + " archive", f"<p>{old}</p>")
            citations = [citation(field, root + ("pricing.html" if field in ("monthly_eur", "seats") else "trust.html"), pricing if field in ("monthly_eur", "seats") else trust, base)
                         for field in ("monthly_eur", "seats", "data_region", "csv_export")]
            candidates.append({"id": slug, "monthly_eur": monthly, "seats": seats, "data_region": region, "csv_export": export, "citations": citations})
        expected = {"candidates": candidates, "recommended_id": "aster",
                    "conflicts": [{"id": slug, "field": "monthly_eur", "current_url": base + f"/research/{slug}/pricing.html", "outdated_url": base + f"/research/{slug}/archive.html"} for slug, *_ in details],
                    "gaps": [{"id": "cedar", "field": "csv_export"}]}
        goal = (f"Use only the saved synthetic pages at {base}/research/index.html and linked pages. Discover all candidates, "
                "verify each candidate independently, resolve archived versus current pricing claims, and identify missing evidence. "
                "Choose a tool for at least 50 seats, EU data residency, documented CSV export, and monthly price <= EUR 40. "
                "Write answer.json and a concise cited comparison report.md. The answer object has candidates, recommended_id "
                "or null, conflicts, and gaps. Each candidate has id, monthly_eur, seats, data_region, csv_export true/false/null, "
                "and citations. For each of those four factual fields provide a citation object with field, url, and a verbatim "
                "supporting quote. A missing documented feature is null, never assumed false or true. conflicts lists each "
                "candidate with conflicting monthly_eur claims using id, field, current_url, outdated_url. gaps lists id and field. "
                "Use dates and explicit supersession when resolving conflicts. Cite links in report.md. No external web access.")
        return pages, expected, goal

    return inventory_lab(seed, base)[:3]


def inventory_lab(seed: str, base: str) -> tuple[dict[str, str], dict, str, dict]:
    rng = random.Random(seed + ":inventory")
    start_port = urlsplit(base).port or 8765
    if start_port < 1024 or start_port > 65531:
        raise ValueError("inventory base port must leave four unprivileged candidate ports")
    ports = list(range(start_port + 1, start_port + 5))
    rng.shuffle(ports)
    services = [("alpha", "Queue", f"2.{rng.randrange(1, 9)}.0", "ok"),
                ("beta", "Catalog", f"3.{rng.randrange(1, 9)}.1", "degraded"),
                ("gamma", "Metrics", f"1.{rng.randrange(1, 9)}.2", "ok")]
    output = []
    listeners = []
    for (sid, name, version, health), port in zip(services, ports):
        origin = f"http://127.0.0.1:{port}"
        identity = f"Service ID: {sid}. Name: {name}. Version: {version}. Transport: http."
        status = f"Service ID: {sid}. Health: {health}."
        responses = {"/": '<html><body><a href="/identity.html">Identity</a> <a href="/status.html">Status</a></body></html>',
                     "/identity.html": f"<html><body><p>{identity}</p></body></html>",
                     "/status.html": f"<html><body><p>{status}</p></body></html>"}
        listeners.append({"port": port, "responses": responses})
        output.append({"id": sid, "origin": origin, "name": name, "version": version, "health": health, "transport": "http",
                       "citations": [citation(field, "/" + ("status.html" if field == "health" else "identity.html"), status if field == "health" else identity, origin)
                                     for field in ("name", "version", "health", "transport")]})
    targets = [f"http://127.0.0.1:{port}" for port in sorted(ports)]
    pages = {"/targets.json": json.dumps({"allowed_origins": targets, "probe_timeout_seconds": 2}, indent=2) + "\n"}
    expected = {"services": output, "unreachable": [f"http://127.0.0.1:{ports[-1]}"]}
    goal = ("Read input/public/targets.json, the complete allowlist for this controlled local network lab. "
            "Probe HTTP GET / with a two-second timeout on ONLY those listed 127.0.0.1 ports. Record the reachable "
            "services and unreachable origins. Independently inspect each reachable service's linked identity and status "
            "pages. Write answer.json and a concise cited report.md. answer.json has services and unreachable. Each service "
            "has id, origin, name, version, health, transport, citations. Each of name/version/health/transport needs a "
            "citation object with field, url, and verbatim supporting quote. unreachable is a list of origin strings. "
            "Probe only the explicit candidate ports, never other hosts, other ports, UDP, TLS, exploits or external targets. "
            "Do not infer a service from its port number. These are real separate loopback HTTP listeners serving synthetic data.")
    return pages, expected, goal, {"listeners": listeners, "closed_port": ports[-1]}


def workflow(case: str, origin: str, goal: str) -> dict:
    discovery = ("Find the matching row; record its raw values in notes/discovery.json. Complete with goals: []."
                 if case == "needle" else
                 "Discover all distinct candidates/services using the fixture links or the explicit allowed-origin list. "
                 "For the network lab, probe only the allowlisted ports and record unreachable origins as well. Record notes/discovery.json. "
                 "On completion return goals as independent child definitions, one per candidate/active service, to verify "
                 "all fields needed by the final goal. Each child must write a distinct notes/checks/<id>.json with sources. "
                 "Child objects may use id, title, goal, kind, artifacts; do not supply depends_on or expansion. "
                 "Do not write answer.json or report.md in discovery or children.")
    artifacts = [{"path": "answer.json"}]
    if case != "needle":
        artifacts.append({"path": "report.md"})
    return {"title": f"{case} workflow", "origin": origin, "stages": [
        {"id": "discover", "goal": discovery, "expansion": {"max_goals": 16},
         "artifacts": [{"path": "notes/discovery.json"}]},
        {"id": "synthesize", "kind": "synthesis", "depends_on": ["discover"], "goal": goal,
         "artifacts": artifacts}]}


def prepare(args) -> dict:
    workspace, private = args.workspace.resolve(), args.private.resolve()
    if workspace == private or workspace in private.parents or private in workspace.parents:
        raise ValueError("workspace and private evaluator directories must be disjoint")
    if workspace.exists() or private.exists():
        raise ValueError("use fresh directories; existing runs must not be overwritten")
    parts = urlsplit(args.base_url)
    if parts.scheme != "http" or parts.hostname not in {"127.0.0.1", "localhost"} or parts.path or parts.query or parts.fragment or parts.username:
        raise ValueError("fixture URL must be a loopback HTTP origin")
    base = args.base_url.rstrip("/")
    pages, answer, goal = fixture(args.case, args.seed, base)
    if args.case == "inventory":
        write_json(private / "lab.json", inventory_lab(args.seed, base)[3])
    for path, body in pages.items():
        dest = workspace / "input/public" / path.lstrip("/")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(body)
    origin = ("Benign synthetic benchmark. Work only in the supplied isolated worker workspace and the declared "
              "loopback fixture. No external web, paid APIs, external network targets or searching for evaluator data. "
              "All modes may plan independent parallel intents. Final task: " + goal)
    write_json(workspace / "task.json", {"case": args.case, "origin": origin, "goal": goal})
    write_json(private / "expected.json", answer)
    write_json(private / "standalone.json", {"title": args.case, "origin": origin, "goal": goal})
    write_json(private / "workflow.json", workflow(args.case, origin, goal))
    hashes = {path: hashlib.sha256(body.encode()).hexdigest() for path, body in pages.items()}
    write_json(private / "manifest.json", {"case": args.case, "seed": args.seed, "base_url": base, "fixture_sha256": hashes,
               "expected_sha256": hashlib.sha256((private / "expected.json").read_bytes()).hexdigest(), "status": "prepared_unrun"})
    return {"case": args.case, "worker_workspace": str(workspace), "private_evaluator": str(private), "status": "prepared_unrun"}


def normalize(value):
    if isinstance(value, dict):
        return {k: normalize(v) for k, v in value.items() if k != "citations"}
    if isinstance(value, list):
        return sorted((normalize(v) for v in value), key=lambda v: json.dumps(v, sort_keys=True))
    return value


def source_text(body: str) -> str:
    class TextReader(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.parts = []

        def handle_data(self, data):
            if data.strip():
                self.parts.append(data.strip())

    reader = TextReader()
    reader.feed(body)
    return " ".join(reader.parts)


def supporting_clause(row: dict, field: str, case: str) -> str:
    if case == "research":
        export = {True: "supported", False: "not supported", None: "not documented"}[row["csv_export"]]
        return {"monthly_eur": f"monthly price EUR {row['monthly_eur']}",
                "seats": f"included seats {row['seats']}",
                "data_region": f"Data region: {row['data_region']}",
                "csv_export": f"CSV export: {export}"}[field]
    return {"name": f"Name: {row['name']}", "version": f"Version: {row['version']}",
            "health": f"Health: {row['health']}", "transport": f"Transport: {row['transport']}"}[field]


def evaluate(args) -> dict:
    expected = json.loads((args.private / "expected.json").read_text())
    manifest = json.loads((args.private / "manifest.json").read_text())
    actual = json.loads((args.workspace / "answer.json").read_text())
    errors = []
    if json.dumps(normalize(actual), sort_keys=True) != json.dumps(normalize(expected), sort_keys=True):
        errors.append("answer fields differ from withheld expected answer")
    if not isinstance(actual, dict):
        return {"case": manifest["case"], "passed": False, "errors": errors}
    public = args.workspace / "input/public"
    sources = {}
    for path, digest in manifest["fixture_sha256"].items():
        content = (public / path.lstrip("/")).read_bytes()
        sources[manifest["base_url"] + path] = source_text(content.decode())
        if hashlib.sha256(content).hexdigest() != digest:
            errors.append("input fixture changed: " + path)
    if manifest["case"] == "inventory":
        lab = json.loads((args.private / "lab.json").read_text())
        sources.update({f"http://127.0.0.1:{listener['port']}" + path: source_text(body)
                        for listener in lab["listeners"] for path, body in listener["responses"].items()})
    group = "candidates" if manifest["case"] == "research" else "services"
    for gold in expected.get(group, []):
        rows = actual.get(group, [])
        matching = [row for row in rows if isinstance(row, dict) and row.get("id") == gold["id"]] if isinstance(rows, list) else []
        if len(matching) != 1:
            continue
        supplied = matching[0].get("citations", [])
        required_by_field = {c["field"]: c for c in gold["citations"]}

        def valid(c, required):
            if (not isinstance(c, dict) or required is None or c.get("url") != required["url"]
                    or not isinstance(c.get("quote"), str)):
                return False
            # Require the field label and its value together, not an unrelated numeric token
            # or the entire multi-field sentence used by the example expected answer.
            clause = supporting_clause(gold, required["field"], manifest["case"])
            return (re.search(re.escape(clause) + r"(?!\w)", c["quote"]) is not None
                    and c["quote"] in sources.get(c["url"], ""))

        if isinstance(supplied, list):
            for c in supplied:
                required = required_by_field.get(c.get("field")) if isinstance(c, dict) else None
                if not valid(c, required):
                    errors.append(f"invalid source quote: {gold['id']}")
        for required in gold["citations"]:
            found = [c for c in supplied if isinstance(c, dict) and c.get("field") == required["field"]] if isinstance(supplied, list) else []
            # Both a short field-specific clause and a larger exact source window are valid.
            if not any(valid(c, required) for c in found):
                errors.append(f"missing supporting citation: {gold['id']}.{required['field']}")
    if manifest["case"] != "needle":
        report = args.workspace / "report.md"
        text = report.read_text() if report.is_file() else ""
        for row in expected[group]:
            for url in {c["url"] for c in row["citations"]}:
                if not re.search(r"\]\(" + re.escape(url) + r"\)", text):
                    errors.append("report missing Markdown citation: " + url)
    return {"case": manifest["case"], "passed": not errors, "errors": errors,
            "note": "Strict structured answer and source checks; report prose still needs human review."}


def serve(args) -> None:
    root = args.public.resolve()
    allowed = {"/" + str(p.relative_to(root)): p for p in root.rglob("*") if p.is_file() and not p.is_symlink() and root in p.resolve().parents}
    responses = {path: target.read_bytes() for path, target in allowed.items()}
    server = ThreadingHTTPServer(("127.0.0.1", args.port), response_handler(responses))
    print(json.dumps({"url": f"http://127.0.0.1:{server.server_port}", "public_file_count": len(allowed)}), flush=True)
    server.serve_forever()


def response_handler(responses):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = unquote(urlsplit(self.path).path)
            data = responses.get(path)
            if data is None:
                self.send_error(404)
                return
            if isinstance(data, str):
                data = data.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8" if path.endswith(".html") or path == "/" else "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_):
            pass
    return Handler


def start_inventory(lab: dict) -> tuple[list, socket.socket]:
    servers = []
    guard = socket.socket()
    try:
        # Bound but not listening: keep this candidate closed and prevent another process taking it.
        guard.bind(("127.0.0.1", lab["closed_port"]))
        for listener in lab["listeners"]:
            server = ThreadingHTTPServer(("127.0.0.1", listener["port"]), response_handler(listener["responses"]))
            servers.append(server)
        for server in servers:
            threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    except OSError:
        guard.close()
        for server in servers:
            server.server_close()
        raise
    return servers, guard


def serve_inventory(args) -> None:
    lab = json.loads(args.lab_config.read_text())
    servers, guard = start_inventory(lab)
    print(json.dumps({"status": "controlled_lab_ready", "candidate_count": len(servers) + 1}), flush=True)
    try:
        threading.Event().wait()
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
        guard.close()


def summarize(args) -> dict:
    records = [json.loads(line) for line in args.ledger.read_text().splitlines() if line.strip()]
    if not records:
        raise ValueError("empty call ledger is not a benchmark result")
    seen = set()
    for call in records:
        if call["call_id"] in seen:
            raise ValueError("duplicate call_id")
        seen.add(call["call_id"])
        if call["model"] != MODEL or call["effort"] != "low":
            raise ValueError("every call must use gpt-6-luna at low effort, including planning and retries")
        if call["phase"] not in {"bootstrap", "bootstrap_conclude", "reason", "explore", "explore_conclude", "retry", "planning"}:
            raise ValueError("unknown call phase")
        for key in ("input_tokens", "cached_input_tokens", "output_tokens"):
            if type(call[key]) is not int or call[key] < 0:
                raise ValueError("all calls need measured nonnegative integer token usage")
        if call["cached_input_tokens"] > call["input_tokens"]:
            raise ValueError("cached tokens must be a subset of input tokens")
        if call["finished_at"] < call["started_at"]:
            raise ValueError("invalid wall clock interval")
        if not call.get("raw_log") or call.get("status") not in {"success", "failed", "cancelled"}:
            raise ValueError("each call needs status and retained raw_log provenance")
    return {"calls": len(records), "input_tokens": sum(c["input_tokens"] for c in records),
            "cached_input_tokens": sum(c["cached_input_tokens"] for c in records),
            "output_tokens": sum(c["output_tokens"] for c in records),
            "total_tokens": sum(c["input_tokens"] + c["output_tokens"] for c in records),
            "wall_seconds": max(c["finished_at"] for c in records) - min(c["started_at"] for c in records),
            "usage_complete": True, "warning": "Completeness requires reconciliation with all dispatcher invocations and startup/stop timestamps."}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("--case", choices=CASES, required=True)
    prep.add_argument("--seed", default="pilot-1")
    prep.add_argument("--base-url", default="http://127.0.0.1:8765")
    prep.add_argument("--workspace", type=Path, required=True)
    prep.add_argument("--private", type=Path, required=True)
    serving = sub.add_parser("serve")
    serving.add_argument("--public", type=Path, required=True)
    serving.add_argument("--port", type=int, default=8765)
    lab = sub.add_parser("serve-inventory")
    lab.add_argument("--lab-config", type=Path, required=True)
    evaluation = sub.add_parser("evaluate")
    evaluation.add_argument("--workspace", type=Path, required=True)
    evaluation.add_argument("--private", type=Path, required=True)
    summary = sub.add_parser("summarize")
    summary.add_argument("--ledger", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = {"prepare": prepare, "serve": serve, "serve-inventory": serve_inventory, "evaluate": evaluate, "summarize": summarize}[args.command](args)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(2, str(exc) + "\n")
    if result is not None:
        print(json.dumps(result, indent=2))
        if result.get("passed") is False:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
