from concurrent.futures import Future, ThreadPoolExecutor
import os
import sqlite3
import sys

from fastapi.testclient import TestClient
from pydantic import TypeAdapter
import pytest

from cairn.dispatcher.config import LocalConfig
from cairn.dispatcher.models import RunningTask
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.local_backend import LocalBackend
from cairn.dispatcher.runtime.local_process import LocalProcess
from cairn.dispatcher.scheduler.loop import DispatcherLoop
from cairn.server import db
from cairn.server.app import app
from cairn.server.models import ProjectSummary


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "management.db")
    with TestClient(app) as http:
        yield http


def create(client, **first):
    response = client.post("/workflows", json={
        "title": "Disposable workflow management test fixture",
        "stages": [
            {"id": "first", "goal": "Produce the disposable result", **first},
            {"id": "second", "goal": "Review the disposable result", "depends_on": ["first"]},
        ],
    })
    assert response.status_code == 201, response.text
    return response.json()


def complete(client, project_id, **extra):
    return client.post(f"/projects/{project_id}/complete", json={
        "from": ["origin"], "description": "Disposable fixture goal achieved", "worker": "test", **extra,
    })


def test_trash_hides_workflow_and_releases_open_claims(client):
    workflow = create(client)
    workflow_id = workflow["workflow"]["id"]
    project_id = workflow["stages"][0]["project_id"]
    assert client.post(f"/projects/{project_id}/intents", json={
        "from": ["origin"], "description": "Inspect fixture", "creator": "worker", "worker": "worker",
    }).status_code == 201
    assert client.post(f"/projects/{project_id}/reason/claim", json={"worker": "reasoner", "trigger": "test"}).status_code == 200
    response = client.delete(f"/workflows/{workflow_id}")
    assert response.status_code == 200
    deleted = response.json()
    assert deleted["workflow"]["deleted_at"]
    assert deleted["workflow"]["status"] == "stopped"
    assert client.get("/workflows").json() == []
    assert [item["id"] for item in client.get("/workflow-trash").json()] == [workflow_id]
    project = client.get(f"/projects/{project_id}").json()
    assert project["project"]["status"] == "stopped"
    assert project["project"]["reason"] is None
    assert project["intents"][0]["worker"] is None
    assert project["intents"][0]["last_heartbeat_at"] is None
    assert deleted["stages"][1]["project_id"] is None
    assert client.get(f"/workflows/{workflow_id}").json() == deleted
    assert len(client.get("/projects").json()) == 1  # Scheduler still sees paused projects.


def test_trash_restore_idempotent_and_restore_never_resumes(client, monkeypatch):
    workflow = create(client)
    workflow_id = workflow["workflow"]["id"]
    project_id = workflow["stages"][0]["project_id"]
    monkeypatch.setattr("cairn.server.workflow_management.utcnow", lambda: "2026-10-27T12:00:00Z")
    first = client.delete(f"/workflows/{workflow_id}").json()
    monkeypatch.setattr("cairn.server.workflow_management.utcnow", lambda: "2026-10-27T13:00:00Z")
    assert client.delete(f"/workflows/{workflow_id}").json() == first
    restored = client.post(f"/workflows/{workflow_id}/restore").json()
    assert restored["workflow"]["deleted_at"] is None
    assert restored["workflow"]["status"] == "stopped"
    assert restored["stages"][0]["project_id"] == project_id
    assert restored["stages"][0]["status"] == "stopped"
    assert restored["stages"][1]["project_id"] is None
    assert client.get("/workflow-trash").json() == []
    assert client.post(f"/workflows/{workflow_id}/restore").json() == restored
    assert client.put(f"/workflows/{workflow_id}/status", json={"status": "active"}).status_code == 200
    assert client.post(f"/workflows/{workflow_id}/restore").json()["workflow"]["status"] == "active"


def test_trash_blocks_mutations_but_keeps_history_readable(client):
    workflow = create(client)
    workflow_id = workflow["workflow"]["id"]
    project_id = workflow["stages"][0]["project_id"]
    assert client.delete(f"/workflows/{workflow_id}").status_code == 200
    requests = [
        ("put", f"/workflows/{workflow_id}/status", {"status": "active"}),
        ("patch", f"/workflows/{workflow_id}/metadata", {"title": "Changed"}),
        ("patch", f"/projects/{project_id}/metadata", {"title": "Changed"}),
        ("put", f"/projects/{project_id}/title", {"title": "Changed"}),
        ("post", f"/projects/{project_id}/hints", {"content": "Changed", "creator": "human"}),
        ("post", f"/projects/{project_id}/intents", {"from": ["origin"], "description": "Changed", "creator": "human"}),
    ]
    for method, url, body in requests:
        response = getattr(client, method)(url, json=body)
        assert response.status_code == 409, response.text
        assert "Trash" in response.text
    assert complete(client, project_id).status_code == 409
    assert client.get(f"/projects/{project_id}/export").status_code == 200
    assert client.get(f"/projects/{project_id}/history").status_code == 200


def test_completed_workflow_restore_preserves_artifacts_completion_and_usage(client):
    workflow = create(client, artifacts=[{"path": "result.txt"}])
    workflow_id = workflow["workflow"]["id"]
    project_id = workflow["stages"][0]["project_id"]
    upload = client.post(f"/projects/{project_id}/artifacts", params={"path": "result.txt"}, content=b"Disposable result")
    assert upload.status_code == 201
    download_url = upload.json()["download_url"]
    assert client.post("/usage/attempts", json={
        "attempt_id": "8f53a7bb-82da-44d7-98f4-8b0b8cff0101", "project_id": project_id,
        "worker_name": "test", "worker_type": "mock",
        "phase": "reason_execute", "status": "success", "started_at": "2026-10-27T12:00:00Z",
        "finished_at": "2026-10-27T12:00:01Z", "duration_ms": 1000, "readings": [{"model_source": "unknown"}],
    }).status_code == 200
    assert complete(client, project_id).status_code == 200
    second_id = client.get(f"/workflows/{workflow_id}").json()["stages"][1]["project_id"]
    assert complete(client, second_id).status_code == 200
    before = client.get(f"/workflows/{workflow_id}").json()
    deleted = client.delete(f"/workflows/{workflow_id}").json()
    assert deleted["workflow"]["status"] == "completed"
    assert deleted["stages"] == before["stages"]
    assert client.get(download_url).content == b"Disposable result"
    assert client.get("/usage/summary", params={"project_id": project_id}).json()["totals"]["attempts"] == 1
    restored = client.post(f"/workflows/{workflow_id}/restore").json()
    assert restored == before


def _pending_review(client):
    workflow = create(client, verification_enabled=True)
    project_id = workflow["stages"][0]["project_id"]
    claim = complete(client, project_id, producer_agent_id="producer", producer_session_id="producer-session").json()
    identity = {"agent_id": "reviewer", "session_id": "review-session", "worker": "test"}
    lease = client.post(f"/reviews/{claim['id']}/claim", json=identity)
    assert lease.status_code == 200
    identity["lease_token"] = lease.json()["lease_token"]
    return workflow, project_id, claim, identity


def _approve(client, claim_id, identity):
    return client.post(f"/reviews/{claim_id}/decision", json={
        **identity, "decision": "approved", "feedback": "Checked disposable result", "evidence": ["Fixture checked"],
    })


def test_trash_invalidates_pending_review_and_old_lease_after_restore(client):
    workflow, project_id, claim, identity = _pending_review(client)
    workflow_id = workflow["workflow"]["id"]
    assert client.delete(f"/workflows/{workflow_id}").status_code == 200
    detail = client.get(f"/projects/{project_id}").json()
    assert detail["project"]["pending_review_id"] == claim["id"]
    assert client.get(f"/reviews/{claim['id']}").json()["status"] == "pending"
    assert _approve(client, claim["id"], identity).status_code == 409
    assert client.post(f"/reviews/{claim['id']}/heartbeat", json=identity).status_code == 409
    assert client.post(f"/reviews/{claim['id']}/release", json=identity).status_code == 409
    assert client.post(f"/reviews/{claim['id']}/claim", json={key: value for key, value in identity.items() if key != "lease_token"}).status_code == 409
    assert client.post(f"/workflows/{workflow_id}/restore").status_code == 200
    assert _approve(client, claim["id"], identity).status_code == 403
    assert client.put(f"/workflows/{workflow_id}/status", json={"status": "active"}).status_code == 200
    assert _approve(client, claim["id"], identity).status_code == 409
    fresh = client.post(f"/reviews/{claim['id']}/claim", json={"agent_id": "reviewer-new", "session_id": "review-new", "worker": "test"}).json()
    assert _approve(client, claim["id"], {"agent_id": "reviewer-new", "session_id": "review-new", "worker": "test", "lease_token": fresh["lease_token"]}).status_code == 200


@pytest.mark.parametrize("verified", [False, True])
def test_trash_serializes_with_completion_or_review_approval(client, verified):
    if verified:
        workflow, project_id, claim, identity = _pending_review(client)
        finish = lambda: _approve(client, claim["id"], identity)
    else:
        workflow = create(client)
        project_id = workflow["stages"][0]["project_id"]
        finish = lambda: complete(client, project_id)
    workflow_id = workflow["workflow"]["id"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        finishing = pool.submit(finish)
        deleting = pool.submit(client.delete, f"/workflows/{workflow_id}")
        assert finishing.result().status_code in (200, 409)
        assert deleting.result().status_code == 200
    after = client.get(f"/workflows/{workflow_id}").json()
    assert after["workflow"]["deleted_at"]
    assert all(stage["status"] != "active" for stage in after["stages"])


def test_trash_causes_scheduler_to_cancel_real_process_and_retain_workspace(client, tmp_path):
    workflow = create(client)
    workflow_id = workflow["workflow"]["id"]
    project_id = workflow["stages"][0]["project_id"]
    backend = LocalBackend(LocalConfig(workspace_root=str(tmp_path / "runs"), completed_action="remove"))
    from pathlib import Path
    workspace = Path(backend.ensure_running(workflow_id))
    retained = workspace / "notes.txt"
    retained.write_text("Keep this unfinished research", encoding="utf-8")
    process = LocalProcess([sys.executable, "-c", "import time; time.sleep(30)"], cwd=str(workspace), env=dict(os.environ), term_grace_seconds=1)
    cancellation = TaskCancellation()
    loop = DispatcherLoop.__new__(DispatcherLoop)
    loop.futures = {Future(): RunningTask(project_id, "explore", "local-fixture", cancellation)}
    process.start()
    cancellation.attach_process(process)
    try:
        assert client.delete(f"/workflows/{workflow_id}").status_code == 200
        summaries = TypeAdapter(list[ProjectSummary]).validate_python(client.get("/projects").json())
        loop._cancel_inactive_tasks(summaries)
        result = process.communicate(timeout=5)
        assert result.cancelled and result.cancel_reason == "stopped"
        loop.futures = {}
        loop._workflow_cleanup_done = set()
        loop._cleanup_pending = set()
        loop.container_manager = backend
        loop._cleanup_workflow_runtimes(summaries)
        assert retained.read_text(encoding="utf-8") == "Keep this unfinished research"
    finally:
        process.kill()


def test_workflow_deleted_at_migration_preserves_existing_rows(tmp_path, monkeypatch):
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE workflows(id TEXT PRIMARY KEY,title TEXT NOT NULL,origin TEXT NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL)")
    conn.execute("INSERT INTO workflows VALUES('old','Existing','Known','active','2026-10-01T00:00:00Z')")
    conn.commit()
    conn.close()
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(path)
    with db.get_conn() as migrated:
        row = migrated.execute("SELECT title,deleted_at FROM workflows WHERE id='old'").fetchone()
        assert row["title"] == "Existing" and row["deleted_at"] is None


@pytest.mark.parametrize("method,url", [("delete", "/workflows/missing"), ("post", "/workflows/missing/restore")])
def test_unknown_workflow_management_is_not_found(client, method, url):
    assert getattr(client, method)(url).status_code == 404
