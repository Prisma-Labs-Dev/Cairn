#!/usr/bin/env python3
"""Common external Pi transport and whole-attempt budget supervisor.

State lives outside the worker workspace. This is accounting, not host isolation.
Only message_start/message_end assistant events count provider turns; agent_end is
cumulative and is deliberately ignored. A CLI invocation can contain many turns.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import signal
import shutil
import sqlite3
import subprocess
import sys
import time
import tempfile
from urllib.request import Request, urlopen
import uuid

MODEL = "gpt-6-luna"
PROVIDER = "openai-codex"
EFFORT = "low"
SETTINGS = {"transport": "sse", "retry": {"enabled": False, "maxRetries": 0, "provider": {"maxRetries": 0}}}


def write_settings(workdir):
    directory = workdir / ".pi"
    directory.mkdir(exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=directory, delete=False) as temporary:
        temporary.write(json.dumps(SETTINGS) + "\n")
        name = temporary.name
    os.replace(name, directory / "settings.json")


def verify_settings(workdir):
    if json.loads((workdir / ".pi/settings.json").read_text()) != SETTINGS:
        raise ValueError("benchmark requires immutable SSE and zero-retry settings")


def initialize(root: Path, real_pi: str, *, seconds=900, calls=24, tokens=50000, allowed_origins=()):
    root.mkdir(parents=True, exist_ok=False)
    (root / "raw").mkdir()
    shutil.copyfile(Path(__file__).with_name("pi_observer.ts"), root / "pi_observer.ts")
    config = {"real_pi": str(Path(real_pi).resolve()), "seconds": seconds,
              "calls": calls, "tokens": tokens, "model": MODEL,
              "provider": PROVIDER, "effort": EFFORT, "allowed_fixture_origins": list(allowed_origins)}
    (root / "config.json").write_text(json.dumps(config, indent=2) + "\n")
    with database(root) as db:
        db.executescript("""
        CREATE TABLE cli(id TEXT PRIMARY KEY,pid INTEGER,pgid INTEGER,started REAL,finished REAL,status TEXT,raw TEXT);
        CREATE TABLE turns(cli TEXT,n INTEGER,started REAL,finished REAL,model TEXT,input INTEGER,cached INTEGER,output INTEGER,stop_reason TEXT,PRIMARY KEY(cli,n));
        CREATE TABLE requests(id TEXT PRIMARY KEY,cli TEXT,started REAL);
        CREATE TABLE control(key TEXT PRIMARY KEY,value TEXT);
        """)
    return config


@contextmanager
def database(root):
    db = sqlite3.connect(root / "ledger.sqlite", timeout=10)
    db.row_factory = sqlite3.Row
    try:
        yield db
        db.commit()
    finally:
        db.close()


def totals(root):
    with database(root) as db:
        cli = [dict(r) for r in db.execute("SELECT * FROM cli ORDER BY started,id")]
        turns = [dict(r) for r in db.execute("SELECT * FROM turns ORDER BY started,cli,n")]
        requests = [dict(r) for r in db.execute("SELECT * FROM requests ORDER BY started,id")]
    known = [r for r in turns if r["input"] is not None and r["output"] is not None]
    missing = [r for r in turns if r not in known]
    return {"cli_invocations": len(cli), "model_invocations": len(turns), "provider_requests": len(requests), "requests": requests,
            "input_tokens": sum(r["input"] for r in known),
            "cached_input_tokens": sum(r["cached"] or 0 for r in known),
            "uncached_input_tokens": sum(r["input"] - (r["cached"] or 0) for r in known),
            "output_tokens": sum(r["output"] for r in known),
            "measured_tokens": sum(r["input"] + r["output"] for r in known),
            "missing_usage_turns": len(missing), "cli": cli, "turns": turns}


def reconcile(root):
    """Match every reserved fetch to raw provider metadata and Pi response usage."""
    data = totals(root)
    problems, requests, completed, messages = [], [], {}, {}
    for cli in data["cli"]:
        metadata = root / "raw" / (cli["id"] + ".metadata.jsonl")
        if metadata.exists():
            for line in metadata.read_text().splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    problems.append("partial_metadata_line")
                    continue
                if event.get("type") == "request_metadata":
                    requests.append(event)
                if event.get("type") == "server_response_metadata" and event.get("event") in ("response.completed", "response.done"):
                    completed[event["id"]] = event
        raw = Path(cli["raw"])
        if raw.exists():
            for line in raw.read_text().splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    problems.append("partial_pi_line")
                    continue
                message = event.get("message", {})
                if event.get("type") == "message_end" and message.get("role") == "assistant":
                    response_id = message.get("responseId")
                    if not response_id:
                        problems.append("pi_response_id_missing")
                    else:
                        messages[response_id] = message
    reserved = {r["id"] for r in data["requests"]}
    if len(requests) != len(reserved) or {r["request_id"] for r in requests} != reserved:
        problems.append("request_reservations_do_not_match_fetch_metadata")
    if len(messages) != len(requests) or set(messages) != set(completed):
        problems.append("request_pi_server_response_sets_do_not_match")
    if any(r.get("model") != MODEL or r.get("reasoning", {}).get("effort") != EFFORT for r in requests):
        problems.append("request_routing_mismatch")
    reasoning = 0
    for response_id, event in completed.items():
        if event.get("model") != MODEL:
            problems.append("server_model_mismatch")
        server = event.get("usage") or {}
        pi = messages.get(response_id, {}).get("usage") or {}
        try:
            if (server["input_tokens"] != pi["input"] + pi["cacheRead"] + pi["cacheWrite"]
                    or server["output_tokens"] != pi["output"]
                    or server["input_tokens_details"]["cached_tokens"] != pi["cacheRead"]):
                problems.append("server_pi_usage_mismatch")
            reasoning += server.get("output_tokens_details", {}).get("reasoning_tokens", 0)
        except (KeyError, TypeError):
            problems.append("server_or_pi_usage_missing")
    return {"complete": bool(requests) and not problems, "problems": sorted(set(problems)),
            "reserved_requests": len(reserved), "observed_fetches": len(requests),
            "pi_response_ids": sorted(messages), "server_completed_response_ids": sorted(completed),
            "reasoning_output_tokens_subset": reasoning}


def limit_reason(root, config, elapsed):
    data = totals(root)
    if elapsed >= config["seconds"]:
        return "time_limit"
    with database(root) as db:
        refused = db.execute("SELECT 1 FROM control WHERE key='cap_refused'").fetchone()
    # Reservation 24 may finish. Refuse 25 before fetch, then drain admitted work.
    if refused and not any(row["finished"] is None for row in data["cli"]):
        return "invocation_limit"
    if data["measured_tokens"] >= config["tokens"]:
        return "token_limit"
    if any(r["model"] not in (None, MODEL) for r in data["turns"]):
        return "model_mismatch"
    return None


def reserve(root, call_id):
    config = json.loads((root / "config.json").read_text())
    with database(root) as db:
        db.execute("BEGIN IMMEDIATE")
        started = db.execute("SELECT value FROM control WHERE key='started'").fetchone()
        if db.execute("SELECT 1 FROM control WHERE key='stopped'").fetchone():
            return None
        if started and time.time() - float(started[0]) >= config["seconds"]:
            return None
        count = db.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
        measured = db.execute("SELECT COALESCE(SUM(input+output),0) FROM turns").fetchone()[0]
        if count >= config["calls"]:
            db.execute("INSERT OR REPLACE INTO control VALUES('cap_refused','1')")
            return None
        if measured >= config["tokens"]:
            return None
        request_id = str(uuid.uuid4())
        db.execute("INSERT INTO requests VALUES(?,?,?)", (request_id, call_id, time.time()))
        return request_id


def request_overshoot(data, config):
    """HTTP budget counts actual requests; rejected assistant turns stay separate."""
    return max(0, data["provider_requests"] - config["calls"])


def forced_arguments(argv, root):
    # Every baseline receives exactly these selectors and session-store placement.
    result, i = [], 0
    while i < len(argv):
        if argv[i] in ("--provider", "--model", "--thinking", "--session-dir"):
            if i + 1 >= len(argv):
                raise ValueError("missing selector value")
            i += 2
        else:
            result.append(argv[i])
            i += 1
    return ["--provider", PROVIDER, "--model", MODEL, "--thinking", EFFORT,
            "--session-dir", str(root / "sessions"), *result]


def valid_count(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def record_event(root, call_id, event, state):
    message = event.get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return
    event_type = event.get("type")
    if event_type not in ("message_start", "message_end"):
        return
    if event_type == "message_start":
        state["n"] += 1
        state["open"] = True
        with database(root) as db:
            db.execute("INSERT INTO turns(cli,n,started,model) VALUES(?,?,?,?)",
                       (call_id, state["n"], time.time(), message.get("model")))
        return
    identity = message.get("responseId") or message.get("id") or hashlib.sha256(json.dumps(message, sort_keys=True).encode()).hexdigest()
    if identity in state["seen"]:
        return
    state["seen"].add(identity)
    if not state["open"]:
        state["n"] += 1
        with database(root) as db:
            db.execute("INSERT INTO turns(cli,n,started) VALUES(?,?,?)", (call_id, state["n"], time.time()))
    state["open"] = False
    usage = message.get("usage", {})
    values = [usage.get(k) for k in ("input", "cacheRead", "cacheWrite", "output")]
    measured = all(valid_count(v) for v in values)
    if message.get("stopReason") in ("error", "aborted") and not any(values):
        measured = False
    fresh, cached, writes, output = values if measured else [None] * 4
    with database(root) as db:
        db.execute("UPDATE turns SET finished=?,model=?,input=?,cached=?,output=?,stop_reason=? WHERE cli=? AND n=?",
                   (time.time(), message.get("responseModel") or message.get("model"),
                    fresh + cached + writes if measured else None, cached, output,
                    message.get("stopReason"), call_id, state["n"]))


def worker(argv):
    root = Path(os.environ["WAYLINE_BENCH_STATE"])
    config = json.loads((root / "config.json").read_text())
    if "--list-models" in argv or "--version" in argv or "--help" in argv:
        os.execv(config["real_pi"], [config["real_pi"], *argv])
    # Fail closed before launching a CLI once supervisor has stopped the attempt.
    with database(root) as db:
        if db.execute("SELECT 1 FROM control WHERE key='stopped'").fetchone():
            return 125
        existing = db.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
        if existing >= config["calls"]:
            db.execute("INSERT OR REPLACE INTO control VALUES('cap_refused','1')")
            return 125
        call_id = str(uuid.uuid4())
        raw = str(root / "raw" / (call_id + ".jsonl"))
        db.execute("INSERT INTO cli VALUES(?,?,?,?,?,?,?)", (call_id, os.getpid(), os.getpgrp(), time.time(), None, "running", raw))
    state = {"n": 0, "open": False, "seen": set()}
    verify_settings(Path.cwd())
    (Path.cwd() / "cairn-sessions").mkdir(exist_ok=True)
    metadata = str(root / "raw" / (call_id + ".metadata.jsonl"))
    child_env = {**os.environ, "WAYLINE_BENCH_CLI_ID": call_id, "WAYLINE_BENCH_PYTHON": sys.executable,
                 "WAYLINE_BENCH_METADATA": metadata, "WAYLINE_BENCH_ALLOWED_ORIGINS": json.dumps(config.get("allowed_fixture_origins", [])), "NODE_OPTIONS": "--import " + str(root / "pi_observer.ts")}

    status = "start_failed"
    try:
        with open(raw, "wb", buffering=0) as log:
            child = subprocess.Popen([config["real_pi"], *forced_arguments(argv, root)], stdout=subprocess.PIPE, env=child_env)
            for line in child.stdout:
                log.write(line)
                sys.stdout.buffer.write(line)
                sys.stdout.buffer.flush()
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if isinstance(event, dict):
                    record_event(root, call_id, event, state)
            code = child.wait()
            with database(root) as db:
                error_turn = db.execute("SELECT 1 FROM turns WHERE cli=? AND stop_reason IN ('error','aborted')", (call_id,)).fetchone()
            if error_turn and code == 0:
                code = 1
            status = "success" if code == 0 else "failed"
    finally:
        with database(root) as db:
            db.execute("UPDATE cli SET finished=?,status=? WHERE id=?", (time.time(), status, call_id))
    return code


def request(url, method="GET", body=None):
    encoded = None if body is None else json.dumps(body).encode()
    req = Request(url, data=encoded, method=method, headers={"content-type": "application/json"})
    with urlopen(req, timeout=3) as response:
        return json.load(response)


def signal_group(pgid, sig):
    if pgid == os.getpgrp():
        raise RuntimeError("refusing to signal supervisor's own process group")
    if not alive(pgid):
        return
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        pass
    except PermissionError:
        # Darwin can report EPERM when the last group member just exited.
        if alive(pgid):
            raise


def alive(pgid):
    # Darwin can return EPERM for killpg(..., 0) after the last member exits.
    # Inspect membership and ignore zombies; they cannot run a worker or child.
    rows = subprocess.check_output(["ps", "-axo", "pgid=,stat="], text=True)
    return any(int(parts[0]) == pgid and not parts[1].startswith("Z")
               for line in rows.splitlines() if len(parts := line.split()) >= 2)


def supervise(root, url, command, cwd=None):
    config = json.loads((root / "config.json").read_text())
    start = time.time()
    monotonic = time.monotonic()
    with database(root) as db:
        db.execute("INSERT OR REPLACE INTO control VALUES('started',?)", (str(start),))
    reason, status_errors = None, []
    env = {**os.environ, "WAYLINE_BENCH_STATE": str(root), "WAYLINE_BENCH_TRANSPORT": str(Path(__file__).resolve())}
    with open(root / "dispatcher.log", "wb") as log:
        process = subprocess.Popen(command, cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            while reason is None:
                reason = limit_reason(root, config, time.monotonic() - monotonic)
                if reason:
                    break
                if process.poll() is not None:
                    reason = "dispatcher_exit"
                    break
                try:
                    detail = request(url)
                    status = detail.get("workflow", detail.get("project", detail)).get("status")
                    if status in ("completed", "stopped"):
                        reason = status
                        break
                except (OSError, ValueError) as exc:
                    status_errors.append(str(exc))
                    if len(status_errors) >= 3:
                        reason = "status_unavailable"
                        break
                time.sleep(0.1)
        finally:
            with database(root) as db:
                db.execute("INSERT OR REPLACE INTO control VALUES('stopped',?)", (reason or "supervisor_interrupted",))
            if reason != "completed":
                try:
                    request(url + "/status", "PUT", {"status": "stopped"})
                except (OSError, ValueError) as exc:
                    status_errors.append("stop endpoint: " + str(exc))
            groups = {r["pgid"] for r in totals(root)["cli"]} | {process.pid}
            for group in groups:
                signal_group(group, signal.SIGTERM)
            until = time.monotonic() + 3
            while any(alive(group) for group in groups) and time.monotonic() < until:
                time.sleep(0.05)
            for group in groups:
                if alive(group):
                    signal_group(group, signal.SIGKILL)
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
    # Snapshot once after all processes have stopped. Incomplete rows remain unknown.
    result = totals(root)
    reconciliation = reconcile(root)
    missing_cli = [r["id"] for r in result["cli"] if not any(t["cli"] == r["id"] for t in result["turns"]) or r["finished"] is None]
    result.update({"stop_reason": reason, "started_at": start, "finished_at": time.time(),
                   "elapsed_seconds": time.monotonic() - monotonic, "budget": config,
                   "token_overshoot": max(0, result["measured_tokens"] - config["tokens"]),
                   "invocation_overshoot": request_overshoot(result, config),
                   "unknown_cli_usage": missing_cli, "status_errors": status_errors,
                   "remaining_process_groups": [group for group in groups if alive(group)],
                   "reconciliation": reconciliation,
                   "usage_complete": not missing_cli and result["missing_usage_turns"] == 0 and reconciliation["complete"]})
    (root / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="action", required=True)
    init = subs.add_parser("init")
    init.add_argument("--root", type=Path, required=True)
    init.add_argument("--real-pi", required=True)
    run = subs.add_parser("run")
    run.add_argument("--root", type=Path, required=True)
    run.add_argument("--target-url", required=True)
    run.add_argument("--cwd", type=Path)
    run.add_argument("command", nargs=argparse.REMAINDER)
    subs.add_parser("reserve")
    call = subs.add_parser("worker")
    call.add_argument("argv", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.action == "init":
        print(json.dumps(initialize(args.root, args.real_pi)))
    elif args.action == "run":
        command = args.command[1:] if args.command[:1] == ["--"] else args.command
        print(json.dumps(supervise(args.root, args.target_url, command, args.cwd), indent=2))
    elif args.action == "reserve":
        reservation = reserve(Path(os.environ["WAYLINE_BENCH_STATE"]), os.environ["WAYLINE_BENCH_CLI_ID"])
        if reservation is None:
            sys.exit(125)
        print(reservation)
    else:
        argv = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
        sys.exit(worker(argv))


if __name__ == "__main__":
    main()
