from __future__ import annotations

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading

from click.testing import CliRunner
from fastapi.testclient import TestClient
import pytest
import requests

from cairn import creation_cli
from cairn.cli import main
from cairn.server import db
from cairn.server.app import app
from cairn.server.models import CreateProjectRequest, CreateWorkflowRequest


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "cli.db")
    with TestClient(app) as client:
        yield client


@contextmanager
def server_for(client):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def send_request(self):
            content = self.rfile.read(int(self.headers.get("content-length", "0")))
            calls.append((self.command, self.path, json.loads(content)))
            response = client.request(self.command, self.path, content=content, headers={"Content-Type": "application/json"})
            self.send_response(response.status_code)
            self.send_header("Content-Type", response.headers.get("content-type", "application/json"))
            self.end_headers()
            self.wfile.write(response.content)

        do_POST = send_request
        do_PATCH = send_request

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def run(*args, input=None, status=0):
    result = CliRunner().invoke(main, list(args), input=input)
    assert result.exit_code == status, result.output + repr(result.exception)
    assert result.stderr == ""
    return json.loads(result.stdout)


def test_goal_only_creation_reaches_api_and_returns_ids(client):
    with server_for(client) as (server, calls):
        result = run("project", "create", "--goal", "Find the requested item", "--server", server)
    assert result["project"]["id"].startswith("proj_")
    assert result["project"]["title"] == "Find the requested item"
    assert {fact["id"]: fact["description"] for fact in result["facts"]} == {
        "origin": "", "goal": "Find the requested item",
    }
    assert calls[0][0:2] == ("POST", "/projects")
    assert client.get(f"/projects/{result['project']['id']}").status_code == 200


@pytest.mark.parametrize("extension,content", [
    ("json", '{"stages":[{"goal":"Find evidence"},{"goal":"Use evidence","depends_on":["stage_1"]}]}'),
    ("yaml", 'stages:\n  - goal: Find evidence\n  - artifacts:\n      - path: outputs/result.bin\n    depends_on: [stage_1]\n'),
])
def test_workflow_file_creation_without_metadata_preserves_input_file(client, tmp_path, extension, content):
    source = tmp_path / f"workflow.{extension}"
    source.write_text(content, encoding="utf-8")
    with server_for(client) as (server, calls):
        result = run("workflow", "create", "--file", str(source), "--server", server)
    assert source.read_text(encoding="utf-8") == content
    assert result["workflow"]["id"].startswith("workflow_")
    assert result["workflow"]["origin"] == ""
    assert [stage["id"] for stage in result["stages"]] == ["stage_1", "stage_2"]
    assert [stage["status"] for stage in result["stages"]] == ["active", "blocked"]
    assert calls[0][0:2] == ("POST", "/workflows")


@pytest.mark.parametrize("resource,payload", [
    ("project", '{"goal":"Исследовать тему"}'),
    ("workflow", 'stages:\n  - goal: Investigate\n'),
])
def test_stdin_json_and_yaml_creation(client, resource, payload):
    with server_for(client) as (server, _calls):
        result = run(resource, "create", "--file", "-", "--server", server, input=payload)
    assert "id" in result[resource]


def never_network(*_args, **_kwargs):
    raise AssertionError("This command must not connect to a server")


def test_schemas_and_dry_run_use_api_models_without_network(monkeypatch):
    monkeypatch.setattr(creation_cli.requests, "request", never_network)
    assert run("project", "schema") == CreateProjectRequest.model_json_schema()
    assert run("workflow", "schema") == CreateWorkflowRequest.model_json_schema()
    project = run("project", "create", "--goal", "Find evidence", "--dry-run")
    assert project == {"dry_run": True, "resource": "project", "request": CreateProjectRequest(goal="Find evidence").model_dump(mode="json")}
    workflow = run("workflow", "create", "--file", "-", "--dry-run", input='{"stages":[{"goal":"Find evidence"}]}')
    assert workflow["dry_run"] is True
    assert workflow["request"]["stages"][0]["id"] == "stage_1"


def test_file_metadata_overrides_are_locally_normalized(monkeypatch):
    monkeypatch.setattr(creation_cli.requests, "request", never_network)
    result = run("project", "create", "--file", "-", "--title", "  Better title  ", "--origin", "  Supplied context  ",
                 "--dry-run", input='{"goal":"Find evidence","title":"Old title"}')
    assert result["request"]["title"] == "Better title"
    assert result["request"]["origin"] == "Supplied context"


@pytest.mark.parametrize("args,payload", [
    (("project", "create", "--file", "-"), '{}'),
    (("project", "create", "--goal", "  "), None),
    (("workflow", "create", "--file", "-"), '{"stages":[]}'),
    (("workflow", "create", "--file", "-"), '{"stages":[{"goal":"A","depends_on":["missing"]}]}'),
    (("project", "metadata", "proj_001"), None),
    (("workflow", "metadata", "workflow_001", "--title", "  "), None),
])
def test_schema_errors_are_json_and_never_reach_network(monkeypatch, args, payload):
    monkeypatch.setattr(creation_cli.requests, "request", never_network)
    result = run(*args, input=payload, status=1)
    assert result["error"]["type"] == "validation"
    assert result["error"]["issues"]
    assert all("input" not in issue and "ctx" not in issue for issue in result["error"]["issues"])


def test_parse_errors_and_input_limit_do_not_echo_source(monkeypatch, tmp_path):
    monkeypatch.setattr(creation_cli.requests, "request", never_network)
    bad_json = tmp_path / "bad.json"
    bad_json.write_text('{"goal":"secret-token",', encoding="utf-8")
    result = run("project", "create", "--file", str(bad_json), status=1)
    assert result["error"]["type"] == "parse"
    assert result["error"]["line"] == 1
    assert "secret-token" not in json.dumps(result)
    yaml_error = run("workflow", "create", "--file", "-", input='stages: [secret-token', status=1)
    assert yaml_error["error"]["type"] == "parse"
    assert "secret-token" not in json.dumps(yaml_error)
    assert run("project", "create", "--file", "-", input="[]", status=1)["error"]["type"] == "input"
    assert run("project", "create", "--file", "-", input="x" * (creation_cli.MAX_INPUT_BYTES + 1), status=1)["error"]["type"] == "input"


@pytest.mark.parametrize("args,code", [
    (("project", "create"), 1),
    (("workflow", "create"), 1),
    (("project", "create", "--file", "-", "--goal", "A"), 1),
    (("project", "create", "--unknown-option"), 2),
    (("workflow", "create", "--goal", "A"), 2),
    (("project", "--unknown-option"), 2),
    (("workflow", "--unknown-option"), 2),
    (("project", "missing-command"), 2),
    (("workflow", "metadata"), 2),
])
def test_creation_usage_errors_are_json(monkeypatch, args, code):
    monkeypatch.setattr(creation_cli.requests, "request", never_network)
    assert run(*args, status=code)["error"]["type"] == "usage"


def test_existing_server_command_usage_stays_unchanged():
    result = CliRunner().invoke(main, ["serve", "--unknown-option"])
    assert result.exit_code == 2
    assert "No such option" in result.stderr
    assert result.stdout == ""


def test_metadata_refinement_reaches_patch_routes_without_null_fields(client):
    with server_for(client) as (server, calls):
        project = run("project", "create", "--goal", "Investigate", "--server", server)
        pid = project["project"]["id"]
        updated = run("project", "metadata", pid, "--title", "Research", "--server", server)
        assert updated["project"]["title"] == "Research"
        assert calls[-1] == ("PATCH", f"/projects/{pid}/metadata", {"title": "Research"})
        enriched = run("project", "metadata", pid, "--origin", "Use the supplied source", "--server", server)
        assert next(fact for fact in enriched["facts"] if fact["id"] == "origin")["description"] == "Use the supplied source"
        workflow = run("workflow", "create", "--file", "-", "--server", server, input='{"stages":[{"goal":"Investigate"}]}')
        wid = workflow["workflow"]["id"]
        changed = run("workflow", "metadata", wid, "--title", "Topic investigation", "--origin", "Future stage context", "--server", server)
        assert changed["workflow"]["title"] == "Topic investigation"
        assert changed["workflow"]["origin"] == "Future stage context"


@pytest.mark.parametrize("status,body,error_type", [
    (400, '{"detail":"secret-token"}', "server"),
    (409, 'not json secret-token', "server"),
    (500, '<html>secret-token</html>', "server"),
    (201, '<html>secret-token</html>', "protocol"),
    (201, '[]', "protocol"),
])
def test_server_failures_are_safe_json(monkeypatch, status, body, error_type):
    response = requests.Response()
    response.status_code = status
    response._content = body.encode()
    monkeypatch.setattr(creation_cli.requests, "request", lambda *_a, **_k: response)
    result = run("project", "create", "--goal", "Find", status=1)
    assert result["error"]["type"] == error_type
    assert result["error"]["status"] == status
    assert "secret-token" not in json.dumps(result)


@pytest.mark.parametrize("exception", [requests.ConnectionError("https://secret-token@example.invalid"), requests.Timeout("secret-token")])
def test_transport_failures_do_not_expose_urls_or_tokens(monkeypatch, exception):
    def unavailable(*_args, **_kwargs):
        raise exception
    monkeypatch.setattr(creation_cli.requests, "request", unavailable)
    result = run("project", "create", "--goal", "Find", status=1)
    assert result["error"]["type"] == "network"
    assert "secret-token" not in json.dumps(result)


def test_server_url_credentials_are_rejected_before_network(monkeypatch):
    monkeypatch.setattr(creation_cli.requests, "request", never_network)
    result = run("project", "create", "--goal", "Find", "--server", "http://user:secret-token@example.invalid", status=1)
    assert result["error"]["type"] == "usage"
    assert "secret-token" not in json.dumps(result)
