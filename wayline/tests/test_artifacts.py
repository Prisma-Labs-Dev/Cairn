from __future__ import annotations

import hashlib
import asyncio
from pathlib import Path
import sqlite3
import subprocess

from fastapi.testclient import TestClient
import pytest

from wayline.dispatcher.artifacts import capture_required_artifacts
from wayline.dispatcher.config import LocalConfig
from wayline.dispatcher.protocol.client import ApiResult
from wayline.dispatcher.runtime.artifact_files import CONTAINER_READ_SCRIPT, read_relative_file
from wayline.dispatcher.runtime.local_backend import LocalBackend
from wayline.server import db
from wayline.server.app import app
from wayline.server.artifact_models import MAX_ARTIFACT_BYTES
from wayline.server.artifact_service import list_artifacts, require_stage_artifacts, store_artifact
from wayline.server.models import ProjectDetail


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "wayline.db")
    with TestClient(app) as test_client:
        yield test_client


def create_output_workflow(client):
    response = client.post("/workflows", json={
        "title": "Find and use a result", "origin": "Use the fixture input.",
        "stages": [
            {"id": "find", "title": "Find", "goal": "Find the useful input",
             "artifacts": [{"path": "artifacts/find/input.bin", "label": "Useful input"}]},
            {"id": "use", "title": "Use", "depends_on": ["find"],
             "artifacts": [{"path": "artifacts/use/output.bin", "label": None}]},
        ],
    })
    assert response.status_code == 201, response.text
    return response.json()


def complete(client, project_id):
    return client.post(f"/projects/{project_id}/complete", json={
        "worker": "test", "from": ["origin"], "description": "Fixture goal achieved.",
    })


def test_required_outputs_gate_completion_and_download_survives_restart(client, monkeypatch):
    workflow = create_output_workflow(client)
    project_id = workflow["stages"][0]["project_id"]
    assert "Produce the required outputs" in workflow["stages"][1]["goal"]
    response = complete(client, project_id)
    assert response.status_code == 409
    assert response.json()["detail"]["missing"] == ["artifacts/find/input.bin"]
    assert client.get(f"/workflows/{workflow['workflow']['id']}").json()["stages"][1]["project_id"] is None

    payload = b"\x00\xffexample binary\x00"
    response = client.post(f"/projects/{project_id}/artifacts", params={"path": "artifacts/find/input.bin"}, content=payload)
    assert response.status_code == 201, response.text
    artifact = response.json()
    assert artifact["size"] == len(payload)
    assert artifact["sha256"] == hashlib.sha256(payload).hexdigest()
    assert complete(client, project_id).status_code == 200
    detail = client.get(f"/workflows/{workflow['workflow']['id']}").json()
    assert detail["stages"][0]["collected_artifacts"][0]["id"] == artifact["id"]
    handoff = detail["stages"][1]["handoffs"][0]
    assert handoff["artifact_count"] == 1
    assert client.get(f"{handoff['context_url']}/artifacts").json()[0]["id"] == artifact["id"]

    original_db = db._db_path
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(original_db)
    downloaded = client.get(artifact["download_url"])
    assert downloaded.content == payload
    assert downloaded.headers["content-type"] == "application/octet-stream"
    assert downloaded.headers["x-content-type-options"] == "nosniff"
    assert downloaded.headers["content-disposition"].startswith("attachment;")
    assert client.post(f"/projects/{project_id}/artifacts", params={"path": "artifacts/find/input.bin"}, content=b"changed").status_code == 403


@pytest.mark.parametrize("path", ["../escape", "/absolute", "x/../escape", "x//file", "x/./file", "x\\file", "x\x00file", "x/"])
def test_invalid_artifact_paths_rejected_when_defining_and_uploading(client, path):
    response = client.post("/workflows", json={
        "title": "bad output", "origin": "start", "stages": [
            {"id": "stage", "title": "Stage", "artifacts": [{"path": path}]},
        ],
    })
    assert response.status_code == 422
    workflow = create_output_workflow(client)
    project_id = workflow["stages"][0]["project_id"]
    assert client.post(f"/projects/{project_id}/artifacts", params={"path": path}, content=b"bad").status_code == 400


def test_undeclared_duplicate_and_oversized_outputs_are_rejected(client):
    workflow = create_output_workflow(client)
    project_id = workflow["stages"][0]["project_id"]
    assert client.post(f"/projects/{project_id}/artifacts", params={"path": "other.bin"}, content=b"bad").status_code == 400
    assert client.post(f"/projects/{project_id}/artifacts", params={"path": "artifacts/find/input.bin"},
                       content=b"x" * (MAX_ARTIFACT_BYTES + 1)).status_code == 413
    assert client.get(f"/projects/{project_id}/artifacts").json() == []
    assert client.post("/workflows", json={
        "title": "duplicates", "origin": "start", "stages": [
            {"id": "stage", "title": "Stage", "artifacts": [{"path": "a"}, {"path": "a"}]},
        ],
    }).status_code == 422
    assert client.post("/workflows", json={
        "title": "nothing", "origin": "start", "stages": [{"id": "stage", "title": "Stage"}],
    }).status_code == 422


def test_artifact_metadata_operations_never_read_binary_contents(client):
    workflow = create_output_workflow(client)
    project_id = workflow["stages"][0]["project_id"]

    def deny_content_reads(action, table, column, _database, _trigger):
        if action == sqlite3.SQLITE_READ and table == "artifacts" and column == "content":
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    with db.get_conn() as conn:
        conn.set_authorizer(deny_content_reads)
        artifact = store_artifact(conn, project_id, "artifacts/find/input.bin", b"payload")
        assert artifact.size == 7
        assert list_artifacts(conn, project_id)[0].sha256 == hashlib.sha256(b"payload").hexdigest()
        require_stage_artifacts(conn, project_id)
        # Verify the authorizer really would reject SELECT * or an explicit BLOB read.
        with pytest.raises(sqlite3.DatabaseError, match="prohibited"):
            conn.execute("SELECT content FROM artifacts WHERE project_id = ?", (project_id,)).fetchone()
        conn.set_authorizer(None)


class InProcessClient:
    def __init__(self, client):
        self.client = client

    def get_project(self, project_id):
        return ProjectDetail.model_validate(self.client.get(f"/projects/{project_id}").json())

    @staticmethod
    def result(response):
        return ApiResult(response.status_code, response.json(), response.text)

    def upload_artifact(self, project_id, path, content):
        return self.result(self.client.post(f"/projects/{project_id}/artifacts", params={"path": path}, content=content))

    def create_hint(self, project_id, content, creator):
        return self.result(self.client.post(f"/projects/{project_id}/hints", json={"content": content, "creator": creator}))

    def create_intent(self, project_id, from_ids, description, creator):
        return self.result(self.client.post(f"/projects/{project_id}/intents", json={
            "from": from_ids, "description": description, "creator": creator,
        }))

    def request_artifact_recovery(self, project_id, description, worker):
        return self.result(self.client.post(f"/projects/{project_id}/artifact-recovery", json={
            "description": description, "worker": worker,
        }))


def test_missing_output_schedules_recovery_then_local_capture_unlocks_successor(client, tmp_path):
    workflow = create_output_workflow(client)
    project_id = workflow["stages"][0]["project_id"]
    api = InProcessClient(client)
    backend = LocalBackend(LocalConfig(workspace_root=str(tmp_path / "work")))
    project = api.get_project(project_id)
    assert not capture_required_artifacts(api, backend, project, "test")
    recovering = api.get_project(project_id)
    assert len(recovering.intents) == 1
    assert "Produce the required artifact files" in recovering.intents[0].description
    hint_count = len(recovering.hints)
    assert not capture_required_artifacts(api, backend, recovering, "test")
    assert len(api.get_project(project_id).hints) == hint_count
    assert len(api.get_project(project_id).intents) == 1

    # A deterministic explorer fulfills the corrective task; no model endpoint is used.
    runtime = Path(backend.ensure_running(project.project.runtime_id))
    output = runtime / "artifacts/find/input.bin"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"needle")
    intent_id = recovering.intents[0].id
    assert client.post(f"/projects/{project_id}/intents/{intent_id}/heartbeat", json={"worker": "test"}).status_code == 200
    assert client.post(f"/projects/{project_id}/intents/{intent_id}/conclude", json={
        "worker": "test", "description": "Created artifacts/find/input.bin from the fixture.",
    }).status_code == 200
    assert capture_required_artifacts(api, backend, api.get_project(project_id), "test")
    assert complete(client, project_id).status_code == 200
    successor = client.get(f"/workflows/{workflow['workflow']['id']}").json()["stages"][1]
    assert successor["status"] == "active"
    assert api.get_project(successor["project_id"]).project.runtime_id == project.project.runtime_id
    assert output.read_bytes() == b"needle"
    artifact = client.get(f"/projects/{project_id}/artifacts").json()[0]
    assert client.get(artifact["download_url"]).content == b"needle"


def test_artifact_read_rejects_escape_symlinks_and_non_regular_files(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    external = tmp_path / "outside.txt"
    external.write_text("outside")
    (work / "link").symlink_to(external)
    (work / "dirlink").symlink_to(tmp_path, target_is_directory=True)
    (work / "directory").mkdir()
    for path in ("link", "dirlink/outside.txt", "directory"):
        with pytest.raises((OSError, ValueError)):
            read_relative_file(work, path, MAX_ARTIFACT_BYTES)
    runtime_link = tmp_path / "runtime-link"
    runtime_link.symlink_to(work, target_is_directory=True)
    with pytest.raises(OSError):
        read_relative_file(runtime_link, "link", MAX_ARTIFACT_BYTES)
    (work / "large").write_bytes(b"12345")
    with pytest.raises(ValueError, match="exceeds"):
        read_relative_file(work, "large", 4)


def test_container_reader_script_uses_same_bounded_regular_file_rules(tmp_path):
    (tmp_path / "result.bin").write_bytes(b"\x00\xffresult")
    result = subprocess.run(["python3", "-c", CONTAINER_READ_SCRIPT, "result.bin", "100"], cwd=tmp_path, capture_output=True)
    assert result.returncode == 0
    assert result.stdout == b"\x00\xffresult"
    (tmp_path / "link").symlink_to(tmp_path / "result.bin")
    result = subprocess.run(["python3", "-c", CONTAINER_READ_SCRIPT, "link", "100"], cwd=tmp_path, capture_output=True)
    assert result.returncode != 0


@pytest.mark.parametrize("depends_on", [[], ["first"]])
def test_workflow_rejects_artifact_paths_shared_by_parallel_or_sequential_stages(client, depends_on):
    response = client.post("/workflows", json={
        "title": "Overlapping outputs", "origin": "start", "stages": [
            {"id": "first", "title": "First", "artifacts": [{"path": "result.bin"}]},
            {"id": "second", "title": "Second", "depends_on": depends_on,
             "artifacts": [{"path": "result.bin"}]},
        ],
    })
    assert response.status_code == 422
    assert "unique" in response.text


def test_open_recovery_intent_blocks_capture_and_completion_even_if_file_is_present(client, tmp_path):
    workflow = create_output_workflow(client)
    project_id = workflow["stages"][0]["project_id"]
    api = InProcessClient(client)
    backend = LocalBackend(LocalConfig(workspace_root=str(tmp_path / "runs")))
    assert client.post(f"/projects/{project_id}/artifacts", params={"path": "artifacts/find/input.bin"}, content=b"earlier").status_code == 201
    assert not capture_required_artifacts(api, backend, api.get_project(project_id), "test")
    output = Path(backend.ensure_running(workflow["workflow"]["id"])) / "artifacts/find/input.bin"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"partially written")
    assert not capture_required_artifacts(api, backend, api.get_project(project_id), "test")
    artifact = client.get(f"/projects/{project_id}/artifacts").json()[0]
    assert client.get(artifact["download_url"]).content == b"earlier"
    assert client.post(f"/projects/{project_id}/artifacts", params={"path": "artifacts/find/input.bin"}, content=b"partial").status_code == 409
    response = complete(client, project_id)
    assert response.status_code == 409
    assert "still running" in response.text


def test_pending_and_claimed_exploration_block_capture_and_completion_until_concluded(client, tmp_path):
    workflow = create_output_workflow(client)
    project_id = workflow["stages"][0]["project_id"]
    api = InProcessClient(client)
    backend = LocalBackend(LocalConfig(workspace_root=str(tmp_path / "runs")))
    output = Path(backend.ensure_running(workflow["workflow"]["id"])) / "artifacts/find/input.bin"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"first version")
    assert capture_required_artifacts(api, backend, api.get_project(project_id), "test")
    created = client.post(f"/projects/{project_id}/intents", json={
        "from": ["origin"], "description": "Write a refined output", "creator": "test",
    })
    assert created.status_code == 201
    intent_id = created.json()["id"]
    assert not capture_required_artifacts(api, backend, api.get_project(project_id), "test")
    assert complete(client, project_id).status_code == 409
    assert client.post(f"/projects/{project_id}/intents/{intent_id}/heartbeat", json={"worker": "test"}).status_code == 200
    output.write_bytes(b"partial")
    assert not capture_required_artifacts(api, backend, api.get_project(project_id), "test")
    assert complete(client, project_id).status_code == 409
    output.write_bytes(b"final version")
    assert client.post(f"/projects/{project_id}/intents/{intent_id}/conclude", json={
        "worker": "test", "description": "Final output written.",
    }).status_code == 200
    assert capture_required_artifacts(api, backend, api.get_project(project_id), "test")
    assert complete(client, project_id).status_code == 200
    artifact = client.get(f"/projects/{project_id}/artifacts").json()[0]
    assert client.get(artifact["download_url"]).content == b"final version"


def test_artifact_recovery_budget_persists_pauses_and_explicit_resume_resets(client, tmp_path, monkeypatch):
    workflow = create_output_workflow(client)
    workflow_id = workflow["workflow"]["id"]
    project_id = workflow["stages"][0]["project_id"]
    api = InProcessClient(client)
    backend = LocalBackend(LocalConfig(workspace_root=str(tmp_path / "runs")))
    original_hints = len(api.get_project(project_id).hints)
    for attempt in range(1, 4):
        assert not capture_required_artifacts(api, backend, api.get_project(project_id), "test")
        stage = client.get(f"/workflows/{workflow_id}").json()["stages"][0]
        assert stage["artifact_recovery_attempts"] == attempt
        project = api.get_project(project_id)
        assert len(project.hints) == original_hints  # errors belong to the recovery intent
        intent = next(item for item in project.intents if item.to is None)
        assert intent.creator == "artifact-collector"
        assert intent.description.startswith("Artifact recovery:")
        assert "input.bin" in intent.description
        client.post(f"/projects/{project_id}/intents/{intent.id}/heartbeat", json={"worker": "test"})
        assert client.post(f"/projects/{project_id}/intents/{intent.id}/conclude", json={
            "worker": "test", "description": "Could not create the required output.",
        }).status_code == 200
        if attempt == 1:
            database = db._db_path
            monkeypatch.setattr(db, "_db_path", None)
            db.configure(database)
    assert not capture_required_artifacts(api, backend, api.get_project(project_id), "test")
    paused = client.get(f"/workflows/{workflow_id}").json()
    assert paused["workflow"]["status"] == "stopped"
    assert paused["stages"][0]["status"] == "stopped"
    assert "paused after 3 attempts" in paused["stages"][0]["artifact_error"]
    assert len(api.get_project(project_id).hints) == original_hints + 1
    assert not capture_required_artifacts(api, backend, api.get_project(project_id), "test")
    assert len(api.get_project(project_id).hints) == original_hints + 1

    resumed = client.put(f"/workflows/{workflow_id}/status", json={"status": "active"}).json()
    assert resumed["stages"][0]["artifact_recovery_attempts"] == 0
    assert resumed["stages"][0]["artifact_error"] is None
    assert not capture_required_artifacts(api, backend, api.get_project(project_id), "test")
    again = client.put(f"/workflows/{workflow_id}/status", json={"status": "active"}).json()
    assert again["stages"][0]["artifact_recovery_attempts"] == 1


def test_runtime_transport_failure_does_not_schedule_artifact_recovery(client):
    workflow = create_output_workflow(client)
    project_id = workflow["stages"][0]["project_id"]
    api = InProcessClient(client)

    class BrokenBackend:
        def ensure_running(self, _runtime_id):
            return "container"

        def read_file(self, *_args):
            raise RuntimeError("Docker daemon unavailable")

    with pytest.raises(RuntimeError, match="Docker daemon unavailable"):
        capture_required_artifacts(api, BrokenBackend(), api.get_project(project_id), "test")
    assert api.get_project(project_id).intents == []
    assert client.get(f"/workflows/{workflow['workflow']['id']}").json()["stages"][0]["artifact_recovery_attempts"] == 0


def test_upload_database_work_runs_outside_async_event_loop(client, monkeypatch):
    from wayline.server.routers import artifacts

    workflow = create_output_workflow(client)
    project_id = workflow["stages"][0]["project_id"]
    original = artifacts.store_artifact
    observed_async_loop = []

    def checked_store(*args):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            observed_async_loop.append(False)
        else:
            observed_async_loop.append(True)
        return original(*args)

    monkeypatch.setattr(artifacts, "store_artifact", checked_store)
    response = client.post(f"/projects/{project_id}/artifacts", params={"path": "artifacts/find/input.bin"}, content=b"output")
    assert response.status_code == 201
    assert observed_async_loop == [False]
