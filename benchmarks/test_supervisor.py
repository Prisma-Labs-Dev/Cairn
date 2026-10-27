from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import shlex
import threading
import socket

HERE = Path(__file__).parent
spec = importlib.util.spec_from_file_location("supervisor", HERE / "supervisor.py")
supervisor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(supervisor)
spec = importlib.util.spec_from_file_location("transport_patch", HERE / "transport_patch.py")
transport = importlib.util.module_from_spec(spec)
spec.loader.exec_module(transport)


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        supervisor.write_settings(self.base)
        self.root = self.base / "state"
        supervisor.initialize(self.root, sys.executable)
        with supervisor.database(self.root) as db:
            db.execute("INSERT INTO cli VALUES('test',1,1,1,NULL,'running','raw')")
        self.state = {"n": 0, "open": False, "seen": set()}

    def tearDown(self):
        self.temp.cleanup()

    def event(self, kind, usage=None, **kwargs):
        message = {"role": "assistant", "model": "gpt-6-luna", "responseId": "r1", **kwargs}
        if usage is not None:
            message["usage"] = usage
        supervisor.record_event(self.root, "test", {"type": kind, "message": message}, self.state)

    def test_actual_turns_cached_inclusive_and_cumulative_duplicates(self):
        self.event("message_start")
        usage = {"input": 100, "cacheRead": 30, "cacheWrite": 5, "output": 12}
        self.event("message_end", usage)
        self.event("message_end", usage)
        self.event("turn_end", usage)
        self.event("agent_end", usage)
        result = supervisor.totals(self.root)
        self.assertEqual(result["model_invocations"], 1)
        self.assertEqual(result["input_tokens"], 135)
        self.assertEqual(result["cached_input_tokens"], 30)
        self.assertEqual(result["measured_tokens"], 147)

    def test_missing_and_error_zero_usage_stay_unknown(self):
        self.event("message_start")
        self.assertEqual(supervisor.totals(self.root)["missing_usage_turns"], 1)
        self.event("message_end", {"input": 0, "cacheRead": 0, "cacheWrite": 0, "output": 0}, stopReason="error")
        self.assertEqual(supervisor.totals(self.root)["missing_usage_turns"], 1)

    def test_every_limit_and_model_mismatch(self):
        config = {"seconds": 900, "calls": 24, "tokens": 50000}
        self.assertEqual(supervisor.limit_reason(self.root, config, 900), "time_limit")
        self.event("message_start")
        self.event("message_end", {"input": 50000, "cacheRead": 0, "cacheWrite": 0, "output": 2})
        self.assertEqual(supervisor.limit_reason(self.root, config, 1), "token_limit")
        with supervisor.database(self.root) as db:
            db.execute("INSERT INTO control VALUES('cap_refused','1')")
            db.execute("UPDATE cli SET finished=2 WHERE id='test'")
        self.assertEqual(supervisor.limit_reason(self.root, {**config, "calls": 1}, 1), "invocation_limit")
        with supervisor.database(self.root) as db:
            db.execute("DELETE FROM control WHERE key='cap_refused'")
        with supervisor.database(self.root) as db:
            db.execute("UPDATE turns SET model='different'")
        self.assertEqual(supervisor.limit_reason(self.root, {**config, "tokens": 999999}, 1), "model_mismatch")

    def test_rejected_assistant_turn_does_not_claim_http_budget_overshoot(self):
        self.assertEqual(supervisor.request_overshoot({"provider_requests": 24, "model_invocations": 25}, {"calls": 24}), 0)
        self.assertEqual(supervisor.request_overshoot({"provider_requests": 25, "model_invocations": 25}, {"calls": 24}), 1)

    def test_api_port_preflight_allows_time_wait_but_rejects_live_listener(self):
        sys.path.insert(0, str(HERE))
        try:
            import run_matrix
        finally:
            sys.path.pop(0)
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        listener.listen()
        with self.assertRaises(OSError):
            run_matrix.free_port(port)
        client = socket.create_connection(("127.0.0.1", port))
        accepted, _ = listener.accept()
        accepted.close()
        client.close()
        listener.close()
        run_matrix.free_port(port)

    def test_all_selectors_forced_and_session_preserved(self):
        args = supervisor.forced_arguments(["--model", "wrong", "--provider", "wrong", "--thinking", "high", "--session", "session.jsonl", "-p", "task"], self.root)
        self.assertEqual(args[:6], ["--provider", "openai-codex", "--model", "gpt-6-luna", "--thinking", "low"])
        self.assertNotIn("wrong", args)
        self.assertIn("session.jsonl", args)

    def test_original_and_fork_have_identical_local_transport(self):
        root = HERE.parent
        sources = [subprocess.check_output(["git", "show", revision + ":cairn/src/cairn/dispatcher/workers/adapters/pi.py"], cwd=root, text=True) for revision in ("8e7e0ea", "f0abdd3")]
        patched = [transport.patched(source) for source in sources]
        for source in patched:
            compile(source, "adapter", "exec")
        local = [source[source.index("    def _local_argv("):source.index("    def extract_session(")] for source in patched]
        self.assertEqual(local[0], local[1])
        execute = [source[source.index("        if self.local:", source.index("    def build_execute(")):source.index("        env = worker.env", source.index("    def build_execute("))] for source in patched]
        self.assertEqual(execute[0], execute[1])

    def test_atomic_pre_request_gate_blocks_all_requests_after_24(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            reservations = list(pool.map(lambda _: supervisor.reserve(self.root, "test"), range(50)))
        self.assertEqual(sum(value is not None for value in reservations), 24)
        self.assertEqual(supervisor.totals(self.root)["provider_requests"], 24)

    def test_node_fetch_observer_gates_real_code_without_network(self):
        script = """
          let nativeCalls=0;
          globalThis.fetch=async()=>{nativeCalls++;return new Response('data: {"type":"response.completed","response":{"id":"fake-response","model":"gpt-6-luna","status":"completed","usage":{"input_tokens":10,"output_tokens":2}}}\\n');};
          const replacement=globalThis.fetch;
          await import(process.env.OBSERVER_URL);
          globalThis.fetch=replacement; // Simulate undici.install after observer import.
          let refused=0;
          for(let i=0;i<26;i++){try{await fetch(i%2 ? new Request('https://chatgpt.com/backend-api/codex/responses',{method:'POST',body:JSON.stringify({model:'gpt-6-luna',reasoning:{effort:'low'}})}) : new URL('https://chatgpt.com/backend-api/codex/responses'), i%2 ? undefined : {method:'POST',body:JSON.stringify({model:'gpt-6-luna',reasoning:{effort:'low'}})});}catch{refused++;}}
          await new Promise(resolve=>setTimeout(resolve,100));
          console.log(JSON.stringify({nativeCalls,refused}));
        """
        env = {**os.environ, "CAIRN_BENCH_STATE": str(self.root), "CAIRN_BENCH_CLI_ID": "test",
               "CAIRN_BENCH_PYTHON": sys.executable, "CAIRN_BENCH_TRANSPORT": str(HERE / "supervisor.py"),
               "CAIRN_BENCH_METADATA": str(self.root / "metadata.jsonl"), "OBSERVER_URL": (self.root / "pi_observer.ts").as_uri()}
        result = subprocess.run(["node", "--input-type=module", "-e", script], env=env, capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout), {"nativeCalls": 24, "refused": 2})
        self.assertEqual(supervisor.totals(self.root)["provider_requests"], 24)
        records = [json.loads(line) for line in (self.root / "metadata.jsonl").read_text().splitlines()]
        self.assertEqual(sum(r["type"] == "server_response_metadata" for r in records), 24)
        self.assertFalse(any("headers" in r for r in records))

    def test_last_admitted_request_finishes_before_call_cap_stop(self):
        fake = self.base / "last-pi"
        fake.write_text("#!" + sys.executable + "\nimport os,subprocess,time,json\n"
                        "gate=subprocess.run([os.environ['CAIRN_BENCH_PYTHON'],os.environ['CAIRN_BENCH_TRANSPORT'],'reserve'],capture_output=True)\n"
                        "assert gate.returncode==0\n"
                        "print(json.dumps({'type':'message_start','message':{'role':'assistant','model':'gpt-6-luna'}}),flush=True)\n"
                        "time.sleep(0.5)\n"
                        "print(json.dumps({'type':'message_end','message':{'role':'assistant','model':'gpt-6-luna','responseId':'final','usage':{'input':10,'cacheRead':0,'cacheWrite':0,'output':2}}}),flush=True)\n")
        fake.chmod(0o755)
        state = self.base / "last-state"
        supervisor.initialize(state, str(fake), calls=1)
        with patch.object(supervisor, "request", return_value={"project": {"status": "active"}}):
            result = supervisor.supervise(state, "http://127.0.0.1:1/projects/fake", [sys.executable, str(HERE / "supervisor.py"), "worker", "--", "--mode", "json", "-p", "fake"], cwd=self.base)
        self.assertEqual(result["provider_requests"], 1)
        self.assertEqual(result["missing_usage_turns"], 0)
        self.assertEqual(result["measured_tokens"], 12)
        self.assertEqual(result["unknown_cli_usage"], [])
        self.assertEqual(result["remaining_process_groups"], [])

    def test_unsupported_provider_routes_refused_and_oauth_refresh_allowed(self):
        script = """
          let nativeCalls=0;
          globalThis.fetch=async()=>{nativeCalls++;return new Response('{}');};
          await import(process.env.OBSERVER_URL);
          let refused=0;
          const inputs=[new URL('https://api.openai.com/v1/responses'),new Request('https://api.openai.com/v1/responses',{method:'POST',body:'{}'}),'https://chatgpt.com/backend-api/codex/responses?bypass=true'];
          for(const input of inputs){try{await fetch(input,{method:'POST',body:JSON.stringify({model:'other-model',reasoning:{effort:'high'}})});}catch{refused++;}}
          await fetch(new Request('https://auth.openai.com/oauth/token',{method:'POST',body:'grant_type=refresh_token'}));
          console.log(JSON.stringify({nativeCalls,refused}));
        """
        env = {**os.environ, "CAIRN_BENCH_STATE": str(self.root), "CAIRN_BENCH_CLI_ID": "test",
               "CAIRN_BENCH_PYTHON": sys.executable, "CAIRN_BENCH_TRANSPORT": str(HERE / "supervisor.py"),
               "CAIRN_BENCH_METADATA": str(self.root / "metadata.jsonl"), "OBSERVER_URL": (self.root / "pi_observer.ts").as_uri()}
        result = subprocess.run(["node", "--input-type=module", "-e", script], env=env, capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout), {"nativeCalls": 1, "refused": 3})
        self.assertEqual(supervisor.totals(self.root)["provider_requests"], 0)

    def test_concurrent_inflight_requests_drain_within_deadline(self):
        fake = self.base / "parallel-pi"
        fake.write_text("#!" + sys.executable + "\nimport os,subprocess,time,json,uuid\n"
                        "gate=subprocess.run([os.environ['CAIRN_BENCH_PYTHON'],os.environ['CAIRN_BENCH_TRANSPORT'],'reserve'],capture_output=True)\n"
                        "assert gate.returncode==0\n"
                        "print(json.dumps({'type':'message_start','message':{'role':'assistant','model':'gpt-6-luna'}}),flush=True)\n"
                        "time.sleep(0.5)\n"
                        "print(json.dumps({'type':'message_end','message':{'role':'assistant','model':'gpt-6-luna','responseId':str(uuid.uuid4()),'usage':{'input':10,'cacheRead':0,'cacheWrite':0,'output':2}}}),flush=True)\n")
        fake.chmod(0o755)
        state = self.base / "parallel-state"
        supervisor.initialize(state, str(fake), calls=2, seconds=3)
        worker = [sys.executable, str(HERE / "supervisor.py"), "worker", "--", "--mode", "json", "-p", "fake"]
        launcher = "import subprocess;cmd=" + repr(worker) + ";children=[subprocess.Popen(cmd) for _ in range(2)];[child.wait() for child in children];subprocess.run(cmd)"
        with patch.object(supervisor, "request", return_value={"project": {"status": "active"}}):
            result = supervisor.supervise(state, "http://127.0.0.1:1/projects/fake", [sys.executable, "-c", launcher], cwd=self.base)
        self.assertEqual(result["provider_requests"], 2)
        self.assertEqual(result["model_invocations"], 2)
        self.assertEqual(result["measured_tokens"], 24)
        self.assertEqual(result["missing_usage_turns"], 0)
        self.assertEqual(result["unknown_cli_usage"], [])
        self.assertEqual(result["remaining_process_groups"], [])
        self.assertLess(result["elapsed_seconds"], 3)

    def test_websocket_refused_after_undici_constructor_reassignment(self):
        script = """
          globalThis.fetch=async()=>{throw new Error('network forbidden in test');};
          await import(process.env.OBSERVER_URL);
          globalThis.WebSocket=class {constructor(){throw new Error('native websocket reached');}};
          new WebSocket('wss://chatgpt.com/backend-api/codex/responses');
        """
        env = {**os.environ, "CAIRN_BENCH_METADATA": str(self.root / "metadata.jsonl"),
               "OBSERVER_URL": (self.root / "pi_observer.ts").as_uri()}
        result = subprocess.run(["node", "--input-type=module", "-e", script], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 125)
        self.assertIn('websocket_transport_refused', (self.root / "metadata.jsonl").read_text())
        self.assertNotIn('native websocket reached', result.stderr)

    def test_parallel_settings_verification_reads_immutable_bundle(self):
        before = (self.base / ".pi/settings.json").read_bytes()
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _: supervisor.verify_settings(self.base), range(100)))
        self.assertEqual((self.base / ".pi/settings.json").read_bytes(), before)

    def test_installed_pi_bash_node_fetch_allows_only_frozen_fixture_get(self):
        tool = Path("/opt/homebrew/lib/node_modules/@earendil-works/pi-coding-agent/dist/core/tools/bash.js")
        if not tool.exists():
            self.skipTest("installed Pi runtime unavailable")
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"controlled-fixture-only")
            def log_message(self, *_):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        origin = "http://127.0.0.1:" + str(server.server_port)
        fetch_script = "fetch(" + json.dumps(origin + "/identity.html") + ").then(r=>r.text()).then(console.log)"
        tool_command = "node -e " + shlex.quote(fetch_script)
        script = "const {createBashTool}=await import(" + json.dumps(tool.as_uri()) + ");const result=await createBashTool(process.cwd()).execute('fixture-test'," + json.dumps({"command": tool_command, "timeout": 2}) + ");console.log(JSON.stringify(result));"
        env = {**os.environ, "NODE_OPTIONS": "--import " + str(self.root / "pi_observer.ts"),
               "CAIRN_BENCH_METADATA": str(self.root / "metadata.jsonl"),
               "CAIRN_BENCH_ALLOWED_ORIGINS": json.dumps([origin])}
        try:
            result = subprocess.run(["node", "--input-type=module", "-e", script], env=env, cwd=self.base, capture_output=True, text=True, check=True, timeout=10)
        finally:
            server.shutdown()
            server.server_close()
        self.assertIn("controlled-fixture-only", result.stdout)
        self.assertEqual(supervisor.totals(self.root)["provider_requests"], 0)

    def test_live_fake_transport_limit_stops_descendants_and_preserves_overshoot(self):
        # A real subprocess and process group, but no model or network call.
        fake = self.base / "fake-pi"
        event = {"type": "message_end", "message": {"role": "assistant", "model": "gpt-6-luna", "responseId": "fake", "usage": {"input": 50000, "cacheRead": 1, "cacheWrite": 0, "output": 3}}}
        fake.write_text("#!" + sys.executable + "\nimport json,time,subprocess\nsubprocess.Popen(['sleep','30'])\nprint(" + repr(json.dumps(event)) + ",flush=True)\ntime.sleep(30)\n")
        fake.chmod(0o755)
        state = self.base / "lifecycle"
        supervisor.initialize(state, str(fake))
        with patch.object(supervisor, "request", return_value={"project": {"status": "active"}}):
            result = supervisor.supervise(state, "http://127.0.0.1:1/projects/fake", [sys.executable, str(HERE / "supervisor.py"), "worker", "--", "--mode", "json", "-p", "fake"], cwd=self.base)
        self.assertEqual(result["stop_reason"], "token_limit")
        self.assertEqual(result["token_overshoot"], 4)
        self.assertEqual(result["remaining_process_groups"], [])
        self.assertTrue(result["unknown_cli_usage"])
        self.assertTrue(Path(result["cli"][0]["raw"]).read_text())


if __name__ == "__main__":
    unittest.main()
