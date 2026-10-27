from __future__ import annotations

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import subprocess
import sys
import threading

from fastapi.testclient import TestClient
import pytest

from cairn.dispatcher import history
from cairn.dispatcher.history_reader import bounded_record
from cairn.dispatcher.config import LocalConfig
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.local_backend import LocalBackend
from cairn.dispatcher.runtime.process import ProcessResult
from cairn.dispatcher.tasks import bootstrap, explore, reason
from cairn.server import db
from cairn.server.app import app
from cairn.server.models import ProjectDetail

from conftest import FakeClient, FakeContainerManager, FakeDriver, FakeLease, make_config, make_intent


@pytest.fixture
def http_client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "history.db")
    with TestClient(app) as client:
        yield client


def make_history(client):
    result = client.post("/workflows", json={
        "title": "Optional history", "origin": "Current starting context", "stages": [
            {"id": "first", "title": "First", "goal": "Research"},
            {"id": "middle", "title": "Middle", "goal": "Intermediate", "depends_on": ["first"]},
            {"id": "current", "title": "Current", "goal": "Focus on the current goal", "depends_on": ["middle"]},
        ],
    })
    assert result.status_code == 201
    workflow = result.json()
    wid = workflow["workflow"]["id"]
    first = workflow["stages"][0]["project_id"]
    source = ("PRIOR_SENTINEL\n" + "historical evidence " * 10_000).strip()
    completion = ("PRIOR_COMPLETION\n" + "historical conclusion " * 10_000).strip()
    intent = client.post(f"/projects/{first}/intents", json={
        "from": ["origin"], "description": "Investigate earlier evidence", "creator": "test", "worker": "test",
    }).json()
    fact = client.post(f"/projects/{first}/intents/{intent['id']}/conclude", json={
        "worker": "test", "description": source,
    }).json()["fact"]
    hint = client.post(f"/projects/{first}/hints", json={
        "creator": "human", "content": "EARLIER_HUMAN_HINT " * 300,
    }).json()
    response = client.post(f"/projects/{first}/complete", json={
        "from": [fact["id"]], "description": completion, "worker": "test",
    })
    assert response.status_code == 200
    middle = client.get(f"/workflows/{wid}").json()["stages"][1]["project_id"]
    assert client.post(f"/projects/{middle}/complete", json={
        "from": ["origin"], "description": "Intermediate result", "worker": "test",
    }).status_code == 200
    current = client.get(f"/workflows/{wid}").json()["stages"][2]["project_id"]
    return ProjectDetail.model_validate(client.get(f"/projects/{current}").json()), source, completion, hint


@contextmanager
def live_history_server(client):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            result = client.get(self.path)
            self.send_response(result.status_code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(result.content)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def reader_path(reference):
    return re.search(r"python3 ([^` ]+) --help", reference).group(1)


def run_reader(path, *arguments, expected=0):
    result = subprocess.run([sys.executable, path, *arguments], capture_output=True, text=True, timeout=10)
    assert result.returncode == expected, result.stderr + result.stdout
    assert result.stderr == ""
    return json.loads(result.stdout)


def test_worker_reads_selected_transitive_history_only_when_requested(http_client, tmp_path, monkeypatch):
    project, source, completion, hint = make_history(http_client)
    monkeypatch.setattr(history, "HISTORY_HELPER_ROOT", str(tmp_path / "readers"))
    config = make_config()
    config.runtime.execution = "local"
    backend = LocalBackend(LocalConfig(workspace_root=str(tmp_path / "work")))
    with live_history_server(http_client) as (server, requests):
        config.runtime.history_server_url = server
        runtime = backend.ensure_running(project.project.runtime_id)
        reference = history.write_optional_history_reference(config, backend, runtime, project)
        assert requests == []
        assert len(reference) < 1000
        assert source not in reference and completion not in reference
        reader = reader_path(reference)
        first_page = run_reader(reader, "list", "--limit", "1")
        assert first_page["total"] == 2 and first_page["next_offset"] == 1
        assert first_page["items"][0]["stage_id"] == "first"
        second_page = run_reader(reader, "list", "--offset", "1")
        assert second_page["items"][0]["stage_id"] == "middle"
        record = run_reader(reader, "fact", "first", "f001", "--max-chars", "50")
        assert record["description"] == source[:50]
        assert record["text_window"]["next_offset"] == 50
        next_chunk = run_reader(reader, "fact", "first", "f001", "--text-offset", "50", "--max-chars", "50")
        assert next_chunk["description"] == source[50:100]
        assert run_reader(reader, "fact", "first", "f001", "--full")["description"] == source
        assert run_reader(reader, "completion", "first", "--max-chars", "60")["description"] == completion[:60]
        assert run_reader(reader, "hint", "first", hint["id"], "--max-chars", "60")["content"] == hint["content"][:60]
        assert run_reader(reader, "fact", "not-available", "f001", expected=1)["status"] == 404
        assert run_reader(reader, "stage", "current", expected=1)["status"] == 404
        assert "error" in run_reader(reader, "fact", expected=1)


@pytest.mark.parametrize("phase", ["bootstrap", "reason", "explore", "bootstrap_conclude", "explore_conclude"])
def test_every_worker_phase_starts_without_earlier_stage_payloads(http_client, tmp_path, monkeypatch, phase):
    project, source, completion, _hint = make_history(http_client)
    monkeypatch.setattr(history, "HISTORY_HELPER_ROOT", str(tmp_path / "readers"))
    config = make_config()
    config.runtime.execution = "local"
    config.runtime.history_server_url = "http://127.0.0.1:1"
    client = FakeClient(project)
    backend = FakeContainerManager()
    driver = FakeDriver()
    lease = FakeLease()
    intent = make_intent()
    base_phase = phase.removesuffix("_conclude")
    module = {"bootstrap": bootstrap, "reason": reason, "explore": explore}[base_phase]
    monkeypatch.setattr(module, "get_driver", lambda *_a, **_k: driver)
    if base_phase == "reason":
        monkeypatch.setattr(module.HeartbeatLease, "for_reason", lambda *_a, **_k: lease)
    else:
        monkeypatch.setattr(module.HeartbeatLease, "for_intent", lambda *_a, **_k: lease)
    outputs = {
        "bootstrap": '{"accepted":true,"data":{"fact":{"description":"Current finding"},"complete":{"description":"Current goal met"}}}',
        "reason": '{"accepted":true,"data":{"intents":[{"from":["origin"],"description":"Current direction"}]}}',
        "explore": '{"accepted":true,"data":{"description":"Current finding"}}',
        "bootstrap_conclude": '{"accepted":true,"data":{"fact":{"description":"Current finding"}}}',
        "explore_conclude": '{"accepted":true,"data":{"description":"Current finding"}}',
    }
    process_name = "_run_process" if base_phase == "explore" else "run_worker_process"
    results = iter(([ProcessResult(0, "Keep working.", "")] if phase.endswith("_conclude") else [])
                   + [ProcessResult(0, outputs[phase], "")])
    monkeypatch.setattr(module, process_name, lambda *_a, **_k: next(results))
    graph = http_client.get(f"/projects/{project.project.id}/export?format=yaml").text
    if base_phase == "bootstrap":
        outcome = bootstrap.run_bootstrap_task(config, client, backend, project, intent, config.workers[0], TaskCancellation())
    elif base_phase == "reason":
        outcome = reason.run_reason_task(config, client, backend, project, graph, config.workers[0], TaskCancellation())
    else:
        outcome = explore.run_explore_task(config, client, backend, project, graph, intent, config.workers[0], TaskCancellation())
    assert outcome == "success"
    prompts = driver.execute_prompts + driver.conclude_prompts
    assert len(prompts) == (2 if phase.endswith("_conclude") else 1)
    for prompt in prompts:
        assert "Prior-stage history is optional" in prompt
        assert "Focus on the current goal" in (prompt + graph)
        assert len(prompt) < 9000
    for text in [*prompts, graph, *[content for _runtime, _path, content in backend.writes]]:
        assert "PRIOR_SENTINEL" not in text
        assert "PRIOR_COMPLETION" not in text
        assert "EARLIER_HUMAN_HINT" not in text
        assert source not in text and completion not in text


def test_worker_history_endpoint_resolution_is_explicit():
    config = make_config()
    config.server = "http://cairn-server:8000"
    assert history.worker_history_server_url(config) == "http://127.0.0.1:8000"
    config.container.network_mode = "bridge"
    assert history.worker_history_server_url(config) == config.server
    config.runtime.execution = "local"
    assert history.worker_history_server_url(config) == config.server
    config.server = "https://custom.example:8443/cairn"
    assert history.worker_history_server_url(config) == config.server
    config.runtime.history_server_url = "https://worker-visible.example/cairn"
    assert history.worker_history_server_url(config) == config.runtime.history_server_url


def test_worker_history_override_rejects_non_base_urls():
    config = make_config().model_dump()
    for url in ("file:///tmp/server", "http://user:secret@example.com", "http://example.com?a=b", "http://example.com#fragment"):
        config["runtime"]["history_server_url"] = url
        with pytest.raises(ValueError):
            type(make_config()).model_validate(config)


def test_selected_record_bounds_long_metadata_as_well_as_text():
    record = bounded_record({"id": "h001", "content": "x" * 20_000, "creator": "y" * 20_000}, 0, 200)
    assert len(record["content"]) == 200
    assert len(record["creator"]) == 256
    assert record["metadata_truncated"] == ["creator"]
    assert record["text_window"]["next_offset"] == 200
