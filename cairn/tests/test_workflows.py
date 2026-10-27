from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import sqlite3

from fastapi.testclient import TestClient
import pytest
import yaml

from cairn.server import db
from cairn.server.app import app
from cairn.server.workflow_service import workflow_summaries


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "workflows.db")
    with TestClient(app) as client:
        yield client


def _stage(stage_id, *dependencies, **kwargs):
    return {"id": stage_id, "title": stage_id.title(), "goal": f"Finish {stage_id}",
            "depends_on": list(dependencies), **kwargs}


def _create(client, stages):
    response = client.post("/workflows", json={"title": "Book", "origin": "Investigate X", "stages": stages})
    assert response.status_code == 201, response.text
    return response.json()


def _complete(client, project_id, *, evidence="origin", description="Goal proven"):
    return client.post(f"/projects/{project_id}/complete",
                       json={"from": [evidence], "description": description, "worker": "test"})


def _detail(client, workflow_id):
    response = client.get(f"/workflows/{workflow_id}")
    assert response.status_code == 200
    return response.json()


def test_chain_unlocks_lazily_and_preserves_provenance(client):
    workflow = _create(client, [_stage("investigate", kind="research", layer="Evidence"),
                                _stage("write", "investigate", kind="synthesis"),
                                _stage("review", "write", kind="review")])
    workflow_id = workflow["workflow"]["id"]
    first, second, third = workflow["stages"]
    assert [stage["status"] for stage in workflow["stages"]] == ["active", "blocked", "blocked"]
    assert second["project_id"] is None and third["project_id"] is None
    project_id = first["project_id"]
    assert client.post(f"/projects/{project_id}/intents", json={
        "from": ["origin"], "description": "Read sources", "creator": "test", "worker": "test",
    }).status_code == 201
    evidence = "Sources agree; detailed notes are in artifacts/investigate/sources.md"
    response = client.post(f"/projects/{project_id}/intents/i001/conclude",
                           json={"worker": "test", "description": evidence})
    assert response.status_code == 200
    assert _complete(client, project_id, evidence="f001", description="Evidence collected").status_code == 200
    after = _detail(client, workflow_id)
    first, second, third = after["stages"]
    assert first["completion"]["description"] == "Evidence collected"
    assert first["completion"]["from"] == ["f001"]
    assert second["status"] == "active" and third["status"] == "blocked"
    handoff = second["handoffs"][0]
    assert handoff["project_id"] == project_id and handoff["fact_count"] == 3
    assert "facts" not in handoff and "completion_description" not in handoff
    inherited_project = client.get(f"/projects/{second['project_id']}").json()
    assert inherited_project["project"]["workflow_id"] == workflow_id
    assert [fact["id"] for fact in inherited_project["facts"]] == ["origin", "goal"]
    assert not any(evidence in hint["content"] for hint in inherited_project["hints"])
    source = client.get(f"{handoff['context_url']}/facts/f001").json()
    assert source["description"] == evidence
    assert client.get(f"{handoff['context_url']}/completion").json()["from"] == ["f001"]
    exported = yaml.safe_load(client.get(f"/projects/{second['project_id']}/export").text)
    assert "handoffs" not in exported["workflow"]
    assert exported["workflow"]["history_index_url"] == second["history_index_url"]
    assert _complete(client, project_id).status_code == 403  # no duplicate successor
    assert _complete(client, second["project_id"]).status_code == 200
    third = _detail(client, workflow_id)["stages"][2]
    assert _complete(client, third["project_id"]).status_code == 200
    assert _detail(client, workflow_id)["workflow"]["status"] == "completed"
    assert len(client.get("/projects").json()) == 3
    summary = client.get("/workflows").json()[0]
    assert summary["completed_stage_count"] == 3 and summary["active_stage_count"] == 0


def test_parallel_research_requires_all_dependencies_and_serializes_completion(client):
    workflow = _create(client, [_stage("join", "a", "b"), _stage("a"), _stage("b")])
    workflow_id = workflow["workflow"]["id"]
    a, b = [stage["project_id"] for stage in workflow["stages"] if stage["id"] != "join"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda pid: _complete(client, pid), [a, b]))
    assert [response.status_code for response in results] == [200, 200]
    detail = _detail(client, workflow_id)
    join = detail["stages"][0]
    assert join["status"] == "active"
    assert {handoff["stage_id"] for handoff in join["handoffs"]} == {"a", "b"}
    assert len(client.get("/projects").json()) == 3
    # Restarting database initialization does not recreate active stages.
    path = db._db_path
    db._db_path = None
    db.configure(path)
    assert _detail(client, workflow_id) == detail
    assert len(client.get("/projects").json()) == 3


def test_join_stays_blocked_after_only_one_prerequisite(client):
    workflow = _create(client, [_stage("a"), _stage("b"), _stage("join", "a", "b")])
    assert _complete(client, workflow["stages"][0]["project_id"]).status_code == 200
    detail = _detail(client, workflow["workflow"]["id"])
    assert detail["stages"][2]["status"] == "blocked"
    assert detail["stages"][2]["project_id"] is None
    assert len(client.get("/projects").json()) == 2


def test_workflow_progress_summary_does_not_read_research_payloads(client):
    workflow = _create(client, [_stage("research"), _stage("write", "research"), _stage("review", "write")])
    assert _complete(client, workflow["stages"][0]["project_id"]).status_code == 200

    def reject_research_reads(action, table, column, *_):
        if action == sqlite3.SQLITE_READ and (
            table in {"facts", "hints", "intents", "artifacts"}
            or table == "workflow_stages" and column in {"definition_json", "handoffs_json", "completion_json"}
        ):
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    with db.get_conn() as conn:
        conn.set_authorizer(reject_research_reads)
        summary = workflow_summaries(conn)[0]
    assert summary.id == workflow["workflow"]["id"]
    assert summary.stage_count == 3
    assert summary.completed_stage_count == 1
    assert summary.active_stage_count == 1


def test_pause_releases_claims_rejects_completion_and_resume_keeps_same_projects(client):
    workflow = _create(client, [_stage("a"), _stage("b", "a")])
    workflow_id = workflow["workflow"]["id"]
    project_id = workflow["stages"][0]["project_id"]
    client.post(f"/projects/{project_id}/intents", json={
        "from": ["origin"], "description": "work", "creator": "worker", "worker": "worker",
    })
    client.post(f"/projects/{project_id}/reason/claim", json={"worker": "worker", "trigger": "test"})
    assert client.put(f"/workflows/{workflow_id}/status", json={"status": "stopped"}).status_code == 200
    project = client.get(f"/projects/{project_id}").json()
    assert project["project"]["status"] == "stopped" and project["project"]["reason"] is None
    assert project["intents"][0]["worker"] is None
    assert _complete(client, project_id).status_code == 403
    assert len(client.get("/projects").json()) == 1
    resumed = client.put(f"/workflows/{workflow_id}/status", json={"status": "active"}).json()
    assert resumed["stages"][0]["project_id"] == project_id
    assert resumed["stages"][0]["status"] == "active"
    assert _complete(client, project_id).status_code == 200
    next_project = _detail(client, workflow_id)["stages"][1]["project_id"]
    assert _complete(client, next_project).status_code == 200
    assert client.put(f"/workflows/{workflow_id}/status", json={"status": "active"}).status_code == 409


def test_workflow_project_cannot_bypass_lifecycle(client):
    workflow = _create(client, [_stage("a"), _stage("b", "a")])
    project_id = workflow["stages"][0]["project_id"]
    assert client.delete(f"/projects/{project_id}").status_code == 409
    assert client.put(f"/projects/{project_id}/status", json={"status": "stopped"}).status_code == 409
    assert _complete(client, project_id).status_code == 200
    assert client.post(f"/projects/{project_id}/reopen",
                       json={"description": "reconsider", "creator": "human"}).status_code == 409
    assert client.delete(f"/projects/{project_id}").status_code == 409


def test_pause_racing_completion_cannot_leave_an_active_successor(client):
    workflow = _create(client, [_stage("a"), _stage("b", "a")])
    workflow_id = workflow["workflow"]["id"]
    project_id = workflow["stages"][0]["project_id"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        completed = pool.submit(_complete, client, project_id)
        stopped = pool.submit(client.put, f"/workflows/{workflow_id}/status", json={"status": "stopped"})
        assert completed.result().status_code in (200, 403)
        assert stopped.result().status_code == 200
    detail = _detail(client, workflow_id)
    assert detail["workflow"]["status"] == "stopped"
    assert all(stage["status"] != "active" for stage in detail["stages"])


@pytest.mark.parametrize("paths", [("Report.md", "report.md"), ("caf\u00e9.md", "cafe\u0301.md")])
@pytest.mark.parametrize("same_stage", [True, False])
def test_output_paths_reject_filesystem_aliases_within_and_across_stages(client, paths, same_stage):
    first = _stage("first", artifacts=[{"path": paths[0]}])
    if same_stage:
        first["artifacts"].append({"path": paths[1]})
        stages = [first]
    else:
        stages = [first, _stage("second", "first", artifacts=[{"path": paths[1]}])]
    response = client.post("/workflows", json={"title": "Aliases", "origin": "Start", "stages": stages})
    assert response.status_code == 422
    assert "artifact paths must be unique" in response.text
    assert client.get("/workflows").json() == []
    assert client.get("/projects").json() == []


@pytest.mark.parametrize("stages", [
    [], [_stage("same"), _stage("same")], [_stage("a", "missing")],
    [_stage("a", "a")], [_stage("a", "b"), _stage("b", "a")],
    [_stage("a"), _stage("b", "a", "a")], [_stage("../bad")],
])
def test_invalid_workflow_rejected_without_partial_projects(client, stages):
    response = client.post("/workflows", json={"title": "Bad", "origin": "Start", "stages": stages})
    assert response.status_code == 422
    assert client.get("/workflows").json() == []
    assert client.get("/projects").json() == []
