#!/usr/bin/env python3
"""Prepare frozen, disposable runtimes; run one reviewed bounded pilot at a time."""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import hashlib
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from urllib.request import urlopen

import bench
import supervisor
import transport_patch

HERE = Path(__file__).resolve().parent
ENGINE = HERE.parent
REVISIONS = {"original": "8e7e0ea67552383851dfcabfba0c4e9c8d007878", "fork": "f0abdd35e63d649dcfcf36a7abc7d540ef21a0ef"}
MODES = ("original", "standalone", "workflow")


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def command(*args):
    return [sys.executable, "-c", "from cairn.cli import main; main()", *args]


def prepare(root, real_pi):
    root.mkdir(parents=True, exist_ok=False)
    (root / "checkouts").mkdir()
    (root / "attempts").mkdir()
    manifests = {}
    for name, revision in REVISIONS.items():
        checkout = root / "checkouts" / name
        subprocess.run(["git", "worktree", "add", "--detach", str(checkout), revision], cwd=ENGINE, check=True, capture_output=True)
        diff = transport_patch.apply(checkout)
        (root / (name + "-transport.patch")).write_text(diff)
        prompts = {}
        for filename in ("bootstrap.md", "bootstrap_conclude.md", "reason.md", "explore.md", "explore_conclude.md"):
            relative = "cairn/src/cairn/dispatcher/prompts/default/" + filename
            content = subprocess.check_output(["git", "show", REVISIONS["original"] + ":" + relative], cwd=ENGINE)
            (checkout / relative).write_bytes(content)
            prompts[filename] = sha(checkout / relative)
        prompt_diff = subprocess.check_output(["git", "diff", "--", "cairn/src/cairn/dispatcher/prompts/default"], cwd=checkout, text=True)
        (root / (name + "-common-prompts.patch")).write_text(prompt_diff)
        runtime_diff = subprocess.check_output(["git", "diff", "--binary"], cwd=checkout)
        (root / (name + "-runtime.patch")).write_bytes(runtime_diff)
        manifests[name] = {"runtime_patch_sha256": sha(root / (name + "-runtime.patch")), "checkout": str(checkout), "revision": revision, "transport_patch_sha256": sha(root / (name + "-transport.patch")),
                           "prompt_patch_sha256": sha(root / (name + "-common-prompts.patch")), "common_prompt_sha256": prompts}
    rows = []
    for case in bench.CASES:
        fixture_manifests = []
        for mode in MODES:
            name = case + "-" + mode
            attempt = root / "attempts" / name
            attempt.mkdir()
            worker, private = attempt / "worker-bundle", attempt / "evaluator"
            bench.prepare(SimpleNamespace(case=case, seed="pilot-1", base_url="http://127.0.0.1:" + ("18831" if case == "research" else "18840"), workspace=worker, private=private))
            supervisor.write_settings(worker)
            fixture = json.loads((private / "manifest.json").read_text())
            fixture_manifests.append(fixture["fixture_sha256"])
            allowed = []
            if case == "research":
                allowed = ["http://127.0.0.1:18831"]
            elif case == "inventory":
                allowed = json.loads((worker / "input/public/targets.json").read_text())["allowed_origins"]
            supervisor.initialize(attempt / "supervision", real_pi, allowed_origins=allowed)
            row = {"id": name, "case": case, "mode": mode, "status": "prepared_unrun", "path": str(attempt), "engine": "original" if mode == "original" else "fork", "fixture_sha256": fixture["fixture_sha256"], "worker_bundle_sha256": {str(p.relative_to(worker)): sha(p) for p in worker.rglob("*") if p.is_file()},
                   "evaluator_bundle_sha256": {str(p.relative_to(private)): sha(p) for p in private.rglob("*") if p.is_file()},
                   "supervision_config_sha256": sha(attempt / "supervision/config.json")}
            write(attempt / "attempt.json", row)
            rows.append(row)
        if not all(item == fixture_manifests[0] for item in fixture_manifests):
            raise RuntimeError("fixture mismatch across modes")
    result = {"status": "prepared_unrun", "model_calls_made": 0, "engines": manifests, "attempts": rows,
              "model": "gpt-6-luna", "provider": "openai-codex", "effort": "low", "limits": {"seconds": 900, "provider_requests": 24, "measured_tokens": 50000},
              "capacity": {"workers": 2, "running_projects": 2, "workers_per_project": 2},
              "independent_verification": False, "local_host_isolation": False,
              "instrumentation_sha256": {p.name: sha(p) for p in (HERE / "supervisor.py", HERE / "pi_observer.ts", HERE / "transport_patch.py", HERE / "run_matrix.py")}}
    write(root / "matrix.json", result)
    return result


def free_port(port):
    with socket.socket() as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", port))


def wait_api(process, url):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("API process exited before readiness")
        try:
            supervisor.request(url + "/projects")
            return
        except (OSError, ValueError):
            time.sleep(0.1)
    raise RuntimeError("API readiness deadline exceeded")


def stop(process):
    if process.poll() is None:
        supervisor.signal_group(process.pid, __import__("signal").SIGTERM)
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            supervisor.signal_group(process.pid, __import__("signal").SIGKILL)
            process.wait(timeout=3)


def dispatch_config(path, url, runtime):
    # Old config has no selector schema. Common transport owns exact selection.
    text = """server: SERVER
runtime:
  execution: local
  worker_healthcheck: disabled
  max_workers: 2
  max_running_projects: 2
  max_project_workers: 2
  interval: 1
  healthcheck_timeout: 15
  prompt_group: default
tasks:
  bootstrap: {timeout: 120, conclude_timeout: 60}
  reason: {timeout: 120, max_intents: 2}
  explore: {timeout: 180, conclude_timeout: 60}
local:
  workspace_root: WORKSPACE
  completed_action: keep
workers:
  - name: luna
    type: pi
    task_types: [bootstrap, reason, explore]
    max_running: 2
    priority: 0
"""
    path.write_text(text.replace("SERVER", url).replace("WORKSPACE", json.dumps(str(runtime))))


def verify_frozen(root, matrix, row):
    engine = matrix["engines"][row["engine"]]
    checkout = Path(engine["checkout"])
    actual = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=checkout, text=True).strip()
    if actual != engine["revision"]:
        raise ValueError("disposable engine revision changed after freeze")
    patch_bytes = subprocess.check_output(["git", "diff", "--binary"], cwd=checkout)
    if hashlib.sha256(patch_bytes).hexdigest() != engine["runtime_patch_sha256"]:
        raise ValueError("disposable source patch changed after freeze")
    for filename, digest in engine["common_prompt_sha256"].items():
        if sha(checkout / "cairn/src/cairn/dispatcher/prompts/default" / filename) != digest:
            raise ValueError("common prompt changed after freeze")
    worker = Path(row["path"]) / "worker-bundle"
    actual_files = {str(p.relative_to(worker)): sha(p) for p in worker.rglob("*") if p.is_file()}
    if actual_files != row["worker_bundle_sha256"]:
        raise ValueError("worker bundle changed after freeze")
    private = Path(row["path"]) / "evaluator"
    if {str(p.relative_to(private)): sha(p) for p in private.rglob("*") if p.is_file()} != row["evaluator_bundle_sha256"]:
        raise ValueError("evaluator bundle changed after freeze")
    if sha(Path(row["path"]) / "supervision/config.json") != row["supervision_config_sha256"]:
        raise ValueError("supervisor budget or allowlist changed after freeze")
    for name, digest in matrix["instrumentation_sha256"].items():
        if sha(HERE / name) != digest:
            raise ValueError("instrumentation changed after freeze: " + name)
    if sha(Path(row["path"]) / "supervision/pi_observer.ts") != matrix["instrumentation_sha256"]["pi_observer.ts"]:
        raise ValueError("attempt observer differs from frozen common observer")


def run(root, attempt_id):
    matrix = json.loads((root / "matrix.json").read_text())
    row = next(row for row in matrix["attempts"] if row["id"] == attempt_id)
    attempt = Path(row["path"])
    current = json.loads((attempt / "attempt.json").read_text())
    if current["status"] != "prepared_unrun":
        raise ValueError("attempt already started; preserve it and never rerun under the same ID")
    verify_frozen(root, matrix, row)
    write(attempt / "frozen-manifest.json", matrix)
    current.update(status="running", started_at=time.time())
    write(attempt / "attempt.json", current)
    checkout = Path(matrix["engines"][row["engine"]]["checkout"])
    env = {**os.environ, "PYTHONPATH": str(checkout / "cairn/src"),
           "CAIRN_USAGE_OUTBOX": str(attempt / "usage-outbox.sqlite"), "CAIRN_SESSION_OUTBOX": str(attempt / "session-outbox")}
    url, port = "http://127.0.0.1:18821", 18821
    runtime = attempt / "runtime"
    runtime.mkdir()
    target = None
    try:
        free_port(port)
        with ExitStack() as stack:
            if row["case"] == "research":
                public = attempt / "worker-bundle/input/public"
                responses = {"/" + str(p.relative_to(public)): p.read_bytes() for p in public.rglob("*") if p.is_file()}
                fixture = ThreadingHTTPServer(("127.0.0.1", 18831), bench.response_handler(responses))
                stack.callback(fixture.server_close)
                thread = threading.Thread(target=fixture.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
                thread.start()
                stack.callback(fixture.shutdown)
            elif row["case"] == "inventory":
                servers, guard = bench.start_inventory(json.loads((attempt / "evaluator/lab.json").read_text()))
                stack.callback(guard.close)
                for fixture in servers:
                    stack.callback(fixture.server_close)
                    stack.callback(fixture.shutdown)
            log = stack.enter_context(open(attempt / "api.log", "wb"))
            api = subprocess.Popen(command("serve", "--host", "127.0.0.1", "--port", str(port), "--db-path", str(attempt / "cairn.sqlite"), "--no-access-log"), env=env, cwd=checkout, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            stack.callback(stop, api)
            wait_api(api, url)
            plural = "workflows" if row["mode"] == "workflow" else "projects"
            payload = json.loads((attempt / "evaluator" / ("workflow.json" if row["mode"] == "workflow" else "standalone.json")).read_text())
            detail = supervisor.request(url + "/" + plural, "POST", payload)
            entity = detail["workflow" if plural == "workflows" else "project"]
            target = url + "/" + plural + "/" + entity["id"]
            write(attempt / "created.json", detail)
            workdir = runtime / entity["id"]
            shutil.copytree(attempt / "worker-bundle", workdir)
            config = attempt / "dispatch.yaml"
            dispatch_config(config, url, runtime)
            # The same venv interpreter imports only the chosen frozen engine.
            dispatch = ["/usr/bin/env", "PYTHONPATH=" + env["PYTHONPATH"], "CAIRN_USAGE_OUTBOX=" + env["CAIRN_USAGE_OUTBOX"], "CAIRN_SESSION_OUTBOX=" + env["CAIRN_SESSION_OUTBOX"], *command("dispatch", "--config", str(config))]
            result = supervisor.supervise(attempt / "supervision", target, dispatch, checkout)
            stopped = supervisor.request(target)
            write(attempt / "stopped.json", stopped)
            captured = attempt / "captured"
            shutil.copytree(workdir, captured)
            collected = {}
            if plural == "workflows":
                for stage in stopped["stages"]:
                    if stage["project_id"]:
                        write(attempt / ("project-" + stage["project_id"] + ".json"), supervisor.request(url + "/projects/" + stage["project_id"]))
                    if stage["id"] != "synthesize":
                        continue
                    for artifact in stage["collected_artifacts"]:
                        with urlopen(url + artifact["download_url"], timeout=3) as response:
                            content = response.read()
                        if hashlib.sha256(content).hexdigest() != artifact["sha256"]:
                            raise RuntimeError("collected artifact byte hash mismatch")
                        (captured / artifact["path"]).write_bytes(content)
                        collected[artifact["path"]] = artifact
                # Final checker uses collected bytes exclusively for workflow outputs.
                for name in ("answer.json", "report.md"):
                    if name not in collected:
                        (captured / name).unlink(missing_ok=True)
            hashes = {str(p.relative_to(captured)): sha(p) for p in captured.rglob("*") if p.is_file()}
            write(attempt / "captured-sha256.json", hashes)
            try:
                evaluation = bench.evaluate(SimpleNamespace(workspace=captured, private=attempt / "evaluator"))
            except (OSError, ValueError, KeyError, TypeError) as exc:
                evaluation = {"passed": False, "errors": [str(exc)], "evaluation_error": type(exc).__name__}
            write(attempt / "evaluation.json", evaluation)
            current.update(status="stopped", finished_at=time.time(), target_url=target, result=result, evaluation=evaluation,
                           report_review="pending human check of claims, sources, uncertainty and possible fabrication", collected_artifacts=collected)
    except BaseException as exc:
        current.update(status="failed", finished_at=time.time(), error=repr(exc), target_url=target)
        raise
    finally:
        write(attempt / "attempt.json", current)
    return current


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("--root", type=Path, required=True)
    prep.add_argument("--real-pi", required=True)
    pilot = sub.add_parser("run")
    pilot.add_argument("--root", type=Path, required=True)
    pilot.add_argument("--attempt", required=True)
    pilot.add_argument("--reviewed", action="store_true", help="operator has reviewed exact patches, freeze and live transport canary")
    args = parser.parse_args()
    if args.action == "prepare":
        result = prepare(args.root.resolve(), args.real_pi)
    else:
        if not args.reviewed:
            parser.error("review exact fairness patches and live canary before model launch")
        result = run(args.root.resolve(), args.attempt)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
