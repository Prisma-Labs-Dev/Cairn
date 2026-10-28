from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import sys

from fastapi.testclient import TestClient
import pytest
import yaml

from wayline.server import db
from wayline.server.app import app
from wayline.dispatcher.contracts import validate_bootstrap_execute_payload, validate_reason_payload
from wayline.dispatcher.config import LocalConfig
from wayline.dispatcher.runtime.local_backend import LocalBackend
from wayline.dispatcher.workers.adapters.mock import MockDriver
from wayline.dispatcher.workers.base import DriverResult
from test_mock_end_to_end import InProcessClient, _config, _dispatch_and_wait, _loop, _phase


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "expansion.db")
    with TestClient(app) as client:
        yield client


def create(client, stages=None):
    response = client.post("/workflows", json={"stages": stages or [
        {"id": "discover", "goal": "Discover items", "expansion": {"max_goals": 2}},
        {"id": "join", "goal": "Combine results", "depends_on": ["discover"]},
    ]})
    assert response.status_code == 201, response.text
    return response.json()


def detail(client, workflow):
    return client.get(f"/workflows/{workflow['workflow']['id']}").json()


def finish(client, project_id, **extra):
    return client.post(f"/projects/{project_id}/complete", json={"from": ["origin"], "description": "Confirmed complete", "worker": "test", **extra})


def test_discover_children_parallel_join_and_exactly_once_retry(client):
    workflow = create(client)
    parent_id = workflow["stages"][0]["project_id"]
    project = client.get(f"/projects/{parent_id}").json()
    assert project["expansion"] == {"max_goals": 2, "mode": "all"}
    assert yaml.safe_load(client.get(f"/projects/{parent_id}/export").text)["workflow"]["expansion"] == {"max_goals": 2, "mode": "all"}
    goals = [{"goal": "Investigate alpha"}, {"id": "beta", "goal": "Investigate beta", "kind": "research"}]
    response = finish(client, parent_id, goals=goals)
    assert response.status_code == 200
    after = detail(client, workflow)
    stages = {stage["id"]: stage for stage in after["stages"]}
    assert stages["discover"]["status"] == "completed"
    assert stages["join"]["status"] == "blocked"
    assert stages["join"]["depends_on"] == ["discover", "discover_goal_1", "beta"]
    for stage_id in ("discover_goal_1", "beta"):
        child = stages[stage_id]
        assert child["status"] == "active"
        assert child["depends_on"] == ["discover"] and child["generated_by_stage_id"] == "discover"
        assert child["expansion"] is None
        child_project = client.get(f"/projects/{child['project_id']}").json()
        assert [fact["id"] for fact in child_project["facts"]] == ["origin", "goal"]
        assert "Investigate beta" not in " ".join(hint["content"] for hint in child_project["hints"])
        assert {item["stage_id"] for item in client.get(child["history_index_url"]).json()["items"]} == {"discover"}
    assert finish(client, parent_id, goals=goals).json() == response.json()
    assert len(detail(client, workflow)["stages"]) == 4
    assert finish(client, parent_id, goals=[]).status_code == 409
    assert finish(client, stages["discover_goal_1"]["project_id"]).status_code == 200
    assert {stage["id"]: stage for stage in detail(client, workflow)["stages"]}["join"]["status"] == "blocked"
    assert finish(client, stages["beta"]["project_id"]).status_code == 200
    final_join = {stage["id"]: stage for stage in detail(client, workflow)["stages"]}["join"]
    assert final_join["status"] == "active"
    assert finish(client, final_join["project_id"]).status_code == 200
    assert detail(client, workflow)["workflow"]["status"] == "completed"


def test_expansion_requires_explicit_empty_or_valid_goals_and_rolls_back(client):
    workflow = create(client)
    parent_id = workflow["stages"][0]["project_id"]
    before = detail(client, workflow)
    for extra in ({}, {"goals": None}, {"goals": [{"goal": "A"}] * 3},
                  {"goals": [{"id": "join", "goal": "Collision"}]},
                  {"goals": [{"id": "same", "goal": "A"}, {"id": "same", "goal": "B"}]}):
        assert finish(client, parent_id, **extra).status_code == 409
        assert detail(client, workflow) == before
        assert client.get(f"/projects/{parent_id}").json()["intents"] == []
    assert finish(client, parent_id, goals=[]).status_code == 200
    after = detail(client, workflow)
    assert len(after["stages"]) == 2 and after["stages"][1]["status"] == "active"


@pytest.mark.parametrize("goals", [
    "bad", [{"artifacts": [{"path": "output.bin"}]}], [{"goal": " "}],
    [{"goal": "A", "depends_on": []}], [{"goal": "A", "expansion": {"max_goals": 2}}],
    [{"goal": "A", "bootstrap_enabled": "yes"}], [{"goal": "x" * 8001}],
    [{"goal": "A", "artifacts": [{"path": "a"}, {"path": "A"}]}],
])
def test_malformed_goal_definitions_are_rejected_without_mutation(client, goals):
    workflow = create(client)
    before = detail(client, workflow)
    assert finish(client, workflow["stages"][0]["project_id"], goals=goals).status_code == 422
    assert detail(client, workflow) == before


@pytest.mark.parametrize("config", [{"max_goals": 0}, {"max_goals": 65}, {"max_goals": True}, {"unexpected": 1}])
def test_invalid_expansion_limits_are_rejected(client, config):
    response = client.post("/workflows", json={"stages": [{"goal": "Discover", "expansion": config}]})
    assert response.status_code == 422
    assert client.get("/projects").json() == []


def test_nonexpandable_project_rejects_nonempty_proposals(client):
    response = client.post("/projects", json={"goal": "Finish"})
    project_id = response.json()["project"]["id"]
    assert finish(client, project_id, goals=[{"goal": "Unexpected"}]).status_code == 409
    assert client.get(f"/projects/{project_id}").json()["project"]["status"] == "active"
    assert finish(client, project_id, goals=[]).status_code == 200


def test_generated_paths_are_unique_and_total_stage_limit_is_enforced(client):
    workflow = create(client, [
        {"id": "discover", "goal": "Discover", "expansion": {}},
        {"id": "join", "goal": "Join", "depends_on": ["discover"], "artifacts": [{"path": "outputs/Report.bin"}]},
    ])
    response = finish(client, workflow["stages"][0]["project_id"], goals=[{"goal": "Child", "artifacts": [{"path": "outputs/report.bin"}]}])
    assert response.status_code == 409
    assert detail(client, workflow) == workflow
    stages = [{"id": "discover", "goal": "Discover", "expansion": {}}] + [
        {"id": f"later_{index}", "goal": "Later", "depends_on": ["discover"]} for index in range(127)
    ]
    large = create(client, stages)
    assert finish(client, large["stages"][0]["project_id"], goals=[{"goal": "One more"}]).status_code == 409
    assert len(detail(client, large)["stages"]) == 128


def test_parallel_expanding_parents_update_same_join_atomically(client):
    workflow = create(client, [
        {"id": "a", "goal": "Discover A", "expansion": {}},
        {"id": "b", "goal": "Discover B", "expansion": {}},
        {"id": "join", "goal": "Combine", "depends_on": ["a", "b"]},
    ])
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda stage: finish(client, stage["project_id"], goals=[{"goal": "Investigate one item"}]), workflow["stages"][:2]))
    assert [response.status_code for response in results] == [200, 200]
    after = {stage["id"]: stage for stage in detail(client, workflow)["stages"]}
    assert set(after["join"]["depends_on"]) == {"a", "b", "a_goal_1", "b_goal_1"}
    assert after["join"]["status"] == "blocked"
    assert after["a_goal_1"]["status"] == after["b_goal_1"]["status"] == "active"


def test_expansion_pause_restart_and_long_generated_ids(client, monkeypatch):
    parent_name = "p" * 64
    workflow = create(client, [{"id": parent_name, "goal": "Discover", "expansion": {}}])
    project_id = workflow["stages"][0]["project_id"]
    workflow_id = workflow["workflow"]["id"]
    client.put(f"/workflows/{workflow_id}/status", json={"status": "stopped"})
    assert finish(client, project_id, goals=[]).status_code == 403
    client.put(f"/workflows/{workflow_id}/status", json={"status": "active"})
    assert finish(client, project_id, goals=[{"goal": "A"}, {"goal": "B"}]).status_code == 200
    after = detail(client, workflow)
    children = after["stages"][1:]
    assert all(len(child["id"]) <= 64 for child in children)
    assert len({child["id"] for child in children}) == 2
    database = db._db_path
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(database)
    assert detail(client, workflow) == after


def test_recovery_is_bounded_persisted_and_explicit_resume_resets(client, monkeypatch):
    workflow = create(client)
    project_id = workflow["stages"][0]["project_id"]
    workflow_id = workflow["workflow"]["id"]
    url = f"/projects/{project_id}/expansion-recovery"
    initial_hints = client.get(f"/projects/{project_id}").json()["hints"]
    for attempt in range(1, 4):
        response = client.post(url, json={"worker": "test", "description": "Missing goals array"})
        assert response.status_code == 200 and response.json()["attempts"] == attempt
        correction = response.json()["intent_id"]
        assert client.post(url, json={"worker": "test", "description": "Repeated error"}).json()["status"] == "existing"
        assert finish(client, project_id, goals=[]).status_code == 409
        assert client.get(f"/projects/{project_id}").json()["hints"] == initial_hints
        client.post(f"/projects/{project_id}/intents/{correction}/heartbeat", json={"worker": "test"})
        assert client.post(f"/projects/{project_id}/intents/{correction}/conclude", json={"worker": "test", "description": "Could not correct output"}).status_code == 200
        database = db._db_path
        monkeypatch.setattr(db, "_db_path", None)
        db.configure(database)
    assert client.post(url, json={"worker": "test", "description": "Still malformed"}).json()["status"] == "paused"
    paused = detail(client, workflow)
    assert paused["workflow"]["status"] == "stopped"
    assert paused["stages"][0]["expansion_recovery_attempts"] == 3
    assert "paused after 3" in paused["stages"][0]["expansion_error"]
    assert client.post(url, json={"worker": "test", "description": "Again"}).status_code == 403
    resumed = client.put(f"/workflows/{workflow_id}/status", json={"status": "active"}).json()
    assert resumed["stages"][0]["expansion_recovery_attempts"] == 0 and resumed["stages"][0]["expansion_error"] is None
    assert finish(client, project_id, goals=[]).status_code == 200


def test_worker_contracts_require_goals_only_when_expansion_enabled():
    reason = {"accepted": True, "data": {"complete": {"from": ["origin"], "description": "Done"}}}
    bootstrap = {"accepted": True, "data": {"fact": {"description": "Evidence"}, "complete": {"description": "Done"}}}
    with pytest.raises(ValueError, match="must include goals"):
        validate_reason_payload(reason, True, 2, 2)
    with pytest.raises(ValueError, match="must include goals"):
        validate_bootstrap_execute_payload(bootstrap, 2)
    reason["data"]["complete"]["goals"] = [{"goal": "Investigate A"}]
    kind, data = validate_reason_payload(reason, True, 2, 2)
    assert kind == "complete" and data["goals"][0]["title"] == "Investigate A"
    with pytest.raises(ValueError, match="does not permit"):
        validate_reason_payload(reason, True, 2)


@pytest.mark.parametrize("use_bootstrap", [True, False])
def test_dispatcher_carries_generated_goals_from_both_completion_paths(client, tmp_path, monkeypatch, use_bootstrap):
    from wayline.dispatcher.tasks import bootstrap, reason

    class Driver(MockDriver):
        def build_execute(self, worker, prompt, session):
            script = """
import json,sys
context=json.loads(sys.argv[1])
complete={'description':'Confirmed complete'}
if context.get('expansion') is not None:
    complete['goals']=[{'goal':'Investigate alpha'},{'goal':'Investigate beta'}]
if context['phase']=='bootstrap':
    data={'fact':{'description':'Confirmed result'},'complete':complete}
else:
    complete['from']=[context['fact_ids'][-1]]
    data={'complete':complete}
print(json.dumps({'accepted':True,'data':data}))
"""
            return DriverResult(argv=[sys.executable, "-c", script, prompt], session=session)

    for module in (bootstrap, reason):
        monkeypatch.setattr(module, "get_driver", lambda *_: Driver())
    workflow = create(client, [
        {"id": "discover", "goal": "Discover", "expansion": {"max_goals": 2}, "bootstrap_enabled": use_bootstrap},
        {"id": "join", "goal": "Join", "depends_on": ["discover"]},
    ])
    config = _config(bootstrap=_phase("complete"), reason=_phase("complete", zero_outcomes=["intent"]), explore=_phase("fact"))
    config.runtime.execution = "local"
    loop = _loop(config, InProcessClient(client), LocalBackend(LocalConfig(workspace_root=str(tmp_path / "runs"))))
    try:
        _dispatch_and_wait(loop)
        stages = detail(client, workflow)["stages"]
        assert [stage["status"] for stage in stages] == ["completed", "blocked", "active", "active"]
        _dispatch_and_wait(loop)
        assert detail(client, workflow)["stages"][1]["status"] == "blocked"
        _dispatch_and_wait(loop)
        assert detail(client, workflow)["stages"][1]["status"] == "active"
        _dispatch_and_wait(loop)
        assert detail(client, workflow)["workflow"]["status"] == "completed"
    finally:
        loop.close()


def test_bad_worker_expansion_outputs_pause_after_bounded_corrections(client, tmp_path, monkeypatch):
    from wayline.dispatcher.tasks import bootstrap, reason, explore
    calls = []

    class Driver(MockDriver):
        def build_execute(self, worker, prompt, session):
            calls.append(1)
            script = """
import json,sys
context=json.loads(sys.argv[1])
if context['phase']=='bootstrap':
    data={'fact':{'description':'Observed'},'complete':{'description':'Done but no goals'}}
elif context['phase']=='reason':
    data={'complete':{'from':[context['fact_ids'][-1]],'description':'Still missing goals'}}
else:
    data={'description':'Correction attempted, no valid goal list was established'}
print(json.dumps({'accepted':True,'data':data}))
"""
            return DriverResult(argv=[sys.executable, "-c", script, prompt], session=session)

    for module in (bootstrap, reason, explore):
        monkeypatch.setattr(module, "get_driver", lambda *_: Driver())
    workflow = create(client)
    config = _config(bootstrap=_phase("complete"), reason=_phase("complete", zero_outcomes=["intent"]), explore=_phase("fact"))
    config.runtime.execution = "local"
    loop = _loop(config, InProcessClient(client), LocalBackend(LocalConfig(workspace_root=str(tmp_path / "runs"))))
    try:
        for _ in range(8):
            if detail(client, workflow)["workflow"]["status"] == "stopped":
                break
            _dispatch_and_wait(loop)
        assert detail(client, workflow)["workflow"]["status"] == "stopped"
        assert len(calls) == 7  # one bad bootstrap, three corrections, three bad reason responses
        loop._dispatch_available(loop.client.list_projects())
        assert loop.futures == {}  # no model work continues after the bounded pause
    finally:
        loop.close()
