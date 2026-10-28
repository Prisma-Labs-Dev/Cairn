from __future__ import annotations

import json

from fastapi.testclient import TestClient
import pytest
import yaml

from wayline.server import db
from wayline.server.app import app
from wayline.server.services import next_hint_id, utcnow
from wayline.server.workflow_context import stage_instructions
from wayline.server.models import WorkflowStageDefinition


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "context.db")
    with TestClient(app) as client:
        yield client


def stage(stage_id, *dependencies):
    return {"id": stage_id, "title": stage_id.title(), "goal": f"Finish {stage_id}", "depends_on": list(dependencies)}


def create(client, stages):
    response = client.post("/workflows", json={"title": "Context", "origin": "Starting instructions", "stages": stages})
    assert response.status_code == 201, response.text
    return response.json()


def complete(client, project_id, description="Goal achieved", fact_id="origin"):
    response = client.post(f"/projects/{project_id}/complete", json={"worker": "test", "from": [fact_id], "description": description})
    assert response.status_code == 200, response.text


def add_finding(client, project_id, description):
    response = client.post(f"/projects/{project_id}/intents", json={
        "from": ["origin"], "description": "Investigate", "creator": "test", "worker": "test",
    })
    assert response.status_code == 201
    intent_id = response.json()["id"]
    response = client.post(f"/projects/{project_id}/intents/{intent_id}/conclude", json={"worker": "test", "description": description})
    assert response.status_code == 200
    return response.json()["fact"]["id"]


def detail(client, workflow):
    return client.get(f"/workflows/{workflow['workflow']['id']}").json()


def test_many_giant_prerequisites_do_not_expand_current_stage_context(client):
    parents = [stage(f"parent{index}") for index in range(16)]
    workflow = create(client, parents + [stage("current", *(item["id"] for item in parents))])
    giant = "HISTORICAL_SENTINEL_" + "x" * 100_000
    for source in workflow["stages"][:-1]:
        fact_id = add_finding(client, source["project_id"], giant)
        complete(client, source["project_id"], giant, fact_id)
    current = detail(client, workflow)["stages"][-1]
    project = client.get(f"/projects/{current['project_id']}")
    assert giant[:30] not in project.text
    assert [fact["id"] for fact in project.json()["facts"]] == ["origin", "goal"]
    assert len(project.json()["hints"]) == 2
    assert len(project.content) < 3_000
    exported = client.get(f"/projects/{current['project_id']}/export")
    data = yaml.safe_load(exported.text)
    assert giant[:30] not in exported.text
    assert "handoffs" not in data["workflow"] and "depends_on" not in data["workflow"]
    assert len(exported.content) < 3_000
    assert data["workflow"]["history_index_url"] == current["history_index_url"]
    for ref in current["handoffs"]:
        assert set(ref) == {"stage_id", "project_id", "fact_count", "intent_count", "artifact_count", "context_url"}
        assert len(json.dumps(ref)) < 300
    index = client.get(current["history_index_url"], params={"limit": 3}).json()
    assert len(index["items"]) == 3 and index["total"] == 16 and index["next_offset"] == 3
    source = index["items"][0]
    assert client.get(f"{source['context_url']}/facts/f001").json()["description"] == giant
    assert client.get(f"{source['context_url']}/completion").json()["description"] == giant


def test_history_is_frozen_to_same_workflow_completed_work_at_activation(client):
    workflow = create(client, [stage("a"), stage("b"), stage("late"), stage("current", "a"), stage("future", "current")])
    a, b, late = workflow["stages"][:3]
    complete(client, b["project_id"])
    complete(client, a["project_id"])
    current = detail(client, workflow)["stages"][3]
    index_url = current["history_index_url"]
    assert {item["stage_id"] for item in client.get(index_url).json()["items"]} == {"a", "b"}
    complete(client, late["project_id"])
    other = create(client, [stage("foreign")])
    complete(client, other["stages"][0]["project_id"])
    for forbidden in ("late", "current", "future", "foreign"):
        for suffix in ("", "/facts", "/facts/origin", "/intents", "/completion", "/artifacts", "/hints"):
            assert client.get(f"{index_url}/{forbidden}{suffix}").status_code == 404
    assert {item["stage_id"] for item in client.get(index_url).json()["items"]} == {"a", "b"}
    assert client.get(f"/projects/{a['project_id']}/history").json()["items"] == []


def test_context_lists_are_bounded_and_full_records_are_explicit(client):
    workflow = create(client, [stage("source"), stage("current", "source")])
    source = workflow["stages"][0]
    text = "Long source " + "z" * 5_000
    fact_id = add_finding(client, source["project_id"], text)
    hint = client.post(f"/projects/{source['project_id']}/hints", json={"content": text, "creator": "human"}).json()
    complete(client, source["project_id"], text, fact_id)
    current = detail(client, workflow)["stages"][1]
    base = current["handoffs"][0]["context_url"]
    metadata = client.get(base).json()
    assert client.get(metadata["goal_url"]).json()["description"] == "Finish source"
    assert client.get(metadata["origin_url"]).json()["description"] == "Starting instructions"
    facts = client.get(metadata["facts_url"], params={"offset": 2, "limit": 1}).json()
    assert facts["total"] == 3 and facts["next_offset"] is None
    assert facts["items"] == [{"id": fact_id, "excerpt": text[:512], "truncated": True}]
    assert client.get(f"{metadata['facts_url']}/{fact_id}").json()["description"] == text
    intents = client.get(metadata["intents_url"], params={"offset": 1, "limit": 1}).json()
    assert intents["items"][0]["excerpt"] == text[:512]
    selected = client.get(f"{metadata['intents_url']}/{intents['items'][0]['id']}").json()
    assert selected["description"] == text and selected["from"] == [fact_id]
    hints = client.get(metadata["hints_url"], params={"offset": metadata["hint_count"] - 1, "limit": 1}).json()
    assert hints["items"][0]["excerpt"] == text[:512]
    assert client.get(f"{metadata['hints_url']}/{hint['id']}").json()["content"] == text
    for url in (current["history_index_url"], metadata["facts_url"], metadata["intents_url"], metadata["hints_url"]):
        assert client.get(url, params={"limit": 101}).status_code == 422
        assert client.get(url, params={"offset": -1}).status_code == 422
    assert client.get(f"{base}/facts/missing").status_code == 404
    assert client.get(f"{base}/intents/missing").status_code == 404
    assert client.get(f"{base}/hints/missing").status_code == 404


def test_legacy_copies_compact_on_restart_without_destroying_sources_or_local_ids(client, monkeypatch):
    workflow = create(client, [stage("source"), stage("current", "source")])
    source = workflow["stages"][0]
    giant = "LEGACY_SENTINEL_" + "x" * 50_000
    fact_id = add_finding(client, source["project_id"], giant)
    complete(client, source["project_id"], giant, fact_id)
    current = detail(client, workflow)["stages"][1]
    project_id = current["project_id"]
    legacy = {"stage_id": "source", "project_id": source["project_id"], "completion_description": giant,
              "from": [fact_id], "facts": [{"id": fact_id, "description": giant}], "artifacts": []}
    exact = f"Stage source completed: {giant}\nEvidence: {source['project_id']}/{fact_id}"
    instructions = stage_instructions(workflow["workflow"]["id"], WorkflowStageDefinition.model_validate(stage("current", "source")))
    with db.get_conn() as conn:
        conn.execute("UPDATE workflow_stages SET context_version=0,handoffs_json=?,history_stage_ids_json='[]' WHERE project_id=?",
                     (json.dumps([legacy]), project_id))
        conn.execute("INSERT INTO facts (id,project_id,description) VALUES ('f001',?,?)", (project_id, exact))
        conn.execute("INSERT INTO facts (id,project_id,description) VALUES ('f099',?,'User finding preserved')", (project_id,))
        conn.execute("INSERT INTO intents (id,project_id,description,creator,created_at) VALUES ('i099',?,'User plan','human',?)", (project_id, utcnow()))
        conn.execute("INSERT INTO intent_sources (intent_id,project_id,fact_id) VALUES ('i099',?,'f001')", (project_id,))
        conn.execute("DELETE FROM hints WHERE project_id=? AND creator='workflow'", (project_id,))
        for content, creator in [
            (instructions + " Prerequisite findings below are inherited claims, not independent verification.", "workflow"),
            ("Prerequisite handoff:\n" + json.dumps(legacy), "workflow"),
            ('Prerequisite handoff:\n{"manual":"human note"}', "human"),
            ('Prerequisite handoff:\n{"manual":"custom workflow note"}', "workflow"),
        ]:
            conn.execute("INSERT INTO hints(id,project_id,content,creator,created_at) VALUES (?,?,?,?,?)",
                         (next_hint_id(conn, project_id), project_id, content, creator, utcnow()))
    path = db._db_path
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(path)
    updated = client.get(f"/projects/{project_id}")
    exported = client.get(f"/projects/{project_id}/export")
    assert "LEGACY_SENTINEL_" not in updated.text and "LEGACY_SENTINEL_" not in exported.text
    facts = {fact["id"]: fact["description"] for fact in updated.json()["facts"]}
    assert "Optional source context" in facts["f001"]
    assert facts["f099"] == "User finding preserved"
    assert updated.json()["intents"][0]["from"] == ["f001"]
    hints = [hint["content"] for hint in updated.json()["hints"]]
    assert 'Prerequisite handoff:\n{"manual":"human note"}' in hints
    assert 'Prerequisite handoff:\n{"manual":"custom workflow note"}' in hints
    base = f"/projects/{project_id}/history/source"
    assert client.get(f"{base}/facts/{fact_id}").json()["description"] == giant
    assert client.get(f"{base}/completion").json()["description"] == giant
    assert client.get(f"/projects/{source['project_id']}").json()["facts"][-1]["description"] == giant
    before = updated.json()
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(path)
    assert client.get(f"/projects/{project_id}").json() == before
    assert client.get(f"/projects/{project_id}/export").text == exported.text
