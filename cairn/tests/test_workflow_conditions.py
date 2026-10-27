from concurrent.futures import ThreadPoolExecutor
import hashlib
import json

from fastapi.testclient import TestClient
import pytest
from pydantic import ValidationError

from cairn.server import db
from cairn.server.app import app
from cairn.server.models import CreateWorkflowRequest
from cairn.server.workflow_conditions import ResultField, validate_result
from cairn.dispatcher.contracts import validate_reason_payload, validate_bootstrap_execute_payload


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "conditions.db")
    with TestClient(app) as client:
        yield client


def stage(id, **extra):
    return {"id": id, "goal": "Achieve " + id, **extra}


COUNT = {"count": {"type": "integer", "minimum": 0}}


def create(client, stages):
    response = client.post("/workflows", json={"stages": stages})
    assert response.status_code == 201, response.text
    return response.json()


def complete(client, pid, **extra):
    return client.post(f"/projects/{pid}/complete", json={"from": ["origin"], "description": "Observed the actual result", "worker": "test", **extra})


def detail(client, workflow):
    return client.get(f"/workflows/{workflow['workflow']['id']}").json()


@pytest.mark.parametrize("result", [{"count": True}, {"count": 1.0}, {"count": "1"}, {"count": -1}, {}, {"count": 1, "other": 2}, {"count": None}])
def test_strict_result_failure_does_not_complete_or_expand(client, result):
    workflow = create(client, [stage("find", result=COUNT, expansion={"max_goals": 2}), stage("use", depends_on=["find"])])
    pid = workflow["stages"][0]["project_id"]
    response = complete(client, pid, result=result, goals=[{"id": "child", "goal": "Do child work"}])
    assert response.status_code == 422
    after = detail(client, workflow)
    assert len(after["stages"]) == 2
    assert after["stages"][0]["completion"] is None
    assert after["stages"][1]["project_id"] is None


@pytest.mark.parametrize("count,expected", [(0, "not_applicable"), (2, "active")])
def test_condition_uses_counts_and_negative_findings_are_success(client, count, expected):
    workflow = create(client, [stage("ownership", result=COUNT), stage("exposure", depends_on=[{"stage": "ownership", "when": "ownership.result.count > 0"}])])
    assert complete(client, workflow["stages"][0]["project_id"], result={"count": count}).status_code == 200
    after = detail(client, workflow)
    assert after["stages"][0]["status"] == "completed"
    assert after["stages"][0]["completion"]["result"] == {"count": count}
    assert after["stages"][1]["status"] == expected
    if count == 0:
        assert after["stages"][1]["project_id"] is None
        assert "Condition is false" in after["stages"][1]["not_applicable_reason"]
        assert after["stages"][1]["resolved_at"]
        assert after["workflow"]["status"] == "completed"
        summary = client.get("/workflows").json()[0]
        assert summary["stage_count"] == 2
        assert summary["completed_stage_count"] == 1
        assert summary["not_applicable_stage_count"] == 1


def test_false_gate_propagates_and_other_success_cannot_bypass(client):
    workflow = create(client, [stage("final", depends_on=["optional", "other"]), stage("partial", depends_on=[{"stage": "optional", "allow_not_applicable": True}, "other"]), stage("optional", depends_on=[{"stage": "find", "when": "find.result.count > 0"}]), stage("find", result=COUNT), stage("other")])
    projects = {s["id"]: s["project_id"] for s in workflow["stages"]}
    assert complete(client, projects["find"], result={"count": 0}).status_code == 200
    assert detail(client, workflow)["stages"][0]["status"] == "blocked"
    assert complete(client, projects["other"]).status_code == 200
    after = detail(client, workflow)
    assert [s["status"] for s in after["stages"][:3]] == ["not_applicable", "active", "not_applicable"]
    partial = client.get(f"/projects/{after['stages'][1]['project_id']}").json()
    assert partial["facts"] == [{"id": "origin", "description": ""}, {"id": "goal", "description": "Achieve partial"}]
    assert any('"status": "not_applicable"' in h["content"] for h in partial["hints"])
    assert len(after["stages"][1]["handoffs"]) == 1


@pytest.mark.parametrize("dependency", [
    {"stage": "find", "when": "find.result.missing > 0"},
    {"stage": "find", "when": "find.result.count > true"},
    {"stage": "find", "when": "find.result.count > __import__(\"os\")"},
    {"stage": "find", "when": "find.result.count > 0 or true"},
    {"stage": "find", "when": "other.result.count > 0"},
    {"stage": "find", "when": "find.result.count in []"},
    {"collection": "unknown"},
])
def test_authoring_rejects_invalid_conditions_and_references(dependency):
    with pytest.raises(ValidationError):
        CreateWorkflowRequest.model_validate({"stages": [stage("find", result=COUNT), stage("use", depends_on=[dependency])]})


def test_collection_dependency_edges_are_cycle_checked():
    with pytest.raises(ValidationError, match="cycle"):
        CreateWorkflowRequest.model_validate({"stages": [stage("find", depends_on=["use"], expansion={"mode": "all"}, emits={"collection": "items"}), stage("use", depends_on=[{"collection": "items"}])]})


def test_unsafe_result_schema_is_rejected():
    for schema in ({"type": "boolean", "minimum": 0}, {"type": "integer", "minimum": True}, {"type": "number", "minimum": "0"}, {"enum": [True, 1]}, {"type": "string", "enum": [1]}, {"type": "integer", "minimum": 5, "maximum": 2}):
        with pytest.raises(ValidationError):
            ResultField.model_validate(schema)
    field = ResultField(type="number")
    for value in (float("nan"), float("inf"), True):
        with pytest.raises(ValueError):
            validate_result({"number": field}, {"number": value})


def test_review_pending_or_rejected_result_never_unlocks(client):
    workflow = create(client, [stage("find", result=COUNT, verification_enabled=True), stage("use", depends_on=[{"stage": "find", "when": "find.result.count > 0"}])])
    pid = workflow["stages"][0]["project_id"]
    identity = {"producer_agent_id": "producer", "producer_session_id": "producer-session"}
    assert complete(client, pid, result={"count": True}, **identity).status_code == 422
    assert client.get("/reviews").json()["items"] == []
    claim = complete(client, pid, result={"count": 2}, **identity).json()
    assert detail(client, workflow)["stages"][1]["project_id"] is None
    reviewer = {"agent_id": "reviewer", "session_id": "reviewer-session", "worker": "reviewer"}
    lease = client.post(f"/reviews/{claim['id']}/claim", json=reviewer).json()["lease_token"]
    decision = {**reviewer, "lease_token": lease, "decision": "rejected", "feedback": "Evidence did not support two", "evidence": ["Checked fixture"]}
    assert client.post(f"/reviews/{claim['id']}/decision", json=decision).status_code == 200
    assert detail(client, workflow)["stages"][1]["project_id"] is None


def test_accepted_review_result_is_frozen_and_survives_schema_reload(client):
    workflow = create(client, [stage("find", result=COUNT, verification_enabled=True), stage("use", depends_on=[{"stage": "find", "when": "find.result.count > 0"}])])
    pid = workflow["stages"][0]["project_id"]
    claim = complete(client, pid, result={"count": 2}, producer_agent_id="producer", producer_session_id="producer-session").json()
    reviewer = {"agent_id": "reviewer", "session_id": "reviewer-session", "worker": "reviewer"}
    lease = client.post(f"/reviews/{claim['id']}/claim", json=reviewer).json()["lease_token"]
    assert client.post(f"/reviews/{claim['id']}/decision", json={**reviewer, "lease_token": lease, "decision": "approved", "feedback": "Checked count against source", "evidence": ["fixture lines 1 and 2"]}).status_code == 200
    saved_path = db._db_path
    db._db_path = None
    db.configure(saved_path)
    after = detail(client, workflow)
    assert after["stages"][0]["completion"]["result"] == {"count": 2}
    assert after["stages"][1]["status"] == "active"
    assert complete(client, pid, result={"count": 0}, producer_agent_id="producer", producer_session_id="producer-session").status_code in (403, 409)


def test_empty_collection_resolves_after_emitter_completion(client):
    workflow = create(client, [stage("find", expansion={"mode": "all"}, emits={"collection": "items", "result": COUNT}), stage("use", depends_on=[{"collection": "items"}])])
    assert workflow["collections"][0]["ready"] is False
    assert complete(client, workflow["stages"][0]["project_id"], goals=[]).status_code == 200
    after = detail(client, workflow)
    assert after["stages"][1]["status"] == "active"
    assert after["collections"][0]["sealed"] is True
    assert after["collections"][0]["ready"] is True
    assert after["collections"][0]["members"] == []


def test_collection_members_are_explicit_accepted_results_and_artifacts(client):
    workflow = create(client, [stage("find", expansion={"mode": "all"}, emits={"collection": "items", "result": COUNT}), stage("use", depends_on=[{"collection": "items"}])])
    pid = workflow["stages"][0]["project_id"]
    goals = [{"id": name, "goal": "Collect " + name, "artifacts": [{"path": name + ".json"}]} for name in ("a", "b")]
    assert complete(client, pid, goals=goals).status_code == 200
    after = detail(client, workflow)
    children = after["stages"][2:]
    for child in children:
        assert child["result"] == {"count": {"type": "integer", "enum": None, "minimum": 0, "maximum": None}}
        body = json.dumps({"count": 1}).encode()
        upload = client.post(f"/projects/{child['project_id']}/artifacts", params={"path": child["id"] + ".json"}, content=body)
        assert upload.status_code == 201
        assert upload.json()["sha256"] == hashlib.sha256(body).hexdigest()
        assert client.post(f"/projects/{child['project_id']}/artifacts", params={"path": "leftover.json"}, content=b"unregistered").status_code == 400
    first, second = children
    assert complete(client, first["project_id"], result={"count": 1}).status_code == 200
    manifest = detail(client, workflow)["collections"][0]
    assert manifest["ready"] is False
    assert manifest["members"][1]["artifacts"] == []
    assert detail(client, workflow)["stages"][1]["project_id"] is None
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: complete(client, second["project_id"], result={"count": 1}), range(2)))
    assert sorted(response.status_code for response in responses) in ([200, 403], [200, 200])
    after = detail(client, workflow)
    assert after["stages"][1]["status"] == "active"
    manifest = after["collections"][0]
    assert manifest["ready"] is True
    assert [member["stage_id"] for member in manifest["members"]] == ["a", "b"]
    for member in manifest["members"]:
        assert member["result"] == {"count": 1}
        assert [a["path"] for a in member["artifacts"]] == [member["stage_id"] + ".json"]
        artifact = member["artifacts"][0]
        assert hashlib.sha256(client.get(artifact["download_url"]).content).hexdigest() == artifact["sha256"]


def test_not_applicable_collection_emitter_does_not_deadlock(client):
    workflow = create(client, [stage("count", result=COUNT), stage("find", depends_on=[{"stage": "count", "when": "count.result.count > 0"}], expansion={"mode": "all"}, emits={"collection": "items"}), stage("use", depends_on=[{"collection": "items"}]), stage("partial", depends_on=[{"collection": "items", "allow_not_applicable": True}])])
    assert complete(client, workflow["stages"][0]["project_id"], result={"count": 0}).status_code == 200
    after = detail(client, workflow)
    assert [s["status"] for s in after["stages"]] == ["completed", "not_applicable", "not_applicable", "active"]
    assert after["collections"][0]["sealed"] is True
    assert after["collections"][0]["ready"] is False


def test_result_correction_is_bounded_and_resume_resets_budget(client):
    workflow = create(client, [stage("find", result=COUNT)])
    pid = workflow["stages"][0]["project_id"]
    for attempt in range(3):
        response = client.post(f"/projects/{pid}/result-recovery", json={"worker": "test", "description": "count must be integer"})
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "scheduled"
        assert client.post(f"/projects/{pid}/result-recovery", json={"worker": "test", "description": "same"}).json()["intent_id"] == body["intent_id"]
        assert complete(client, pid, result={"count": 1}).status_code == 409
        assert client.post(f"/projects/{pid}/intents/{body['intent_id']}/heartbeat", json={"worker": "test"}).status_code == 200
        assert client.post(f"/projects/{pid}/intents/{body['intent_id']}/conclude", json={"worker": "test", "description": "Checked proper result"}).status_code == 200
    assert client.post(f"/projects/{pid}/result-recovery", json={"worker": "test", "description": "still invalid"}).json()["status"] == "paused"
    assert detail(client, workflow)["workflow"]["status"] == "stopped"
    assert client.put(f"/workflows/{workflow['workflow']['id']}/status", json={"status": "active"}).status_code == 200
    assert detail(client, workflow)["stages"][0]["result_recovery_attempts"] == 0


def test_dispatcher_preserves_and_validates_typed_results():
    contract = {"count": ResultField(type="integer", minimum=0)}
    data = {"complete": {"from": ["f001"], "description": "Found two", "result": {"count": 2}}}
    kind, checked = validate_reason_payload({"accepted": True, "data": data}, open_intents_empty=True, max_intents=3, result_contract=contract)
    assert kind == "complete" and checked["result"] == {"count": 2}
    bootstrap = {"accepted": True, "data": {"fact": {"description": "Observed two records"}, "complete": {"description": "Found two", "result": {"count": 2}}}}
    assert validate_bootstrap_execute_payload(bootstrap, result_contract=contract)[1]["result"] == {"count": 2}
    data["complete"]["result"]["count"] = True
    with pytest.raises(ValueError):
        validate_reason_payload({"accepted": True, "data": data}, open_intents_empty=True, max_intents=3, result_contract=contract)


@pytest.mark.parametrize("bootstrap_enabled", [True, False])
def test_real_dispatcher_paths_deliver_result_through_protocol_client(client, tmp_path, monkeypatch, bootstrap_enabled):
    import sys
    from cairn.dispatcher.tasks import bootstrap, reason
    from cairn.dispatcher.config import LocalConfig
    from cairn.dispatcher.runtime.local_backend import LocalBackend
    from cairn.dispatcher.protocol.client import CairnClient
    from cairn.dispatcher.workers.adapters.mock import MockDriver
    from cairn.dispatcher.workers.base import DriverResult
    from test_mock_end_to_end import InProcessClient, _config, _dispatch_and_wait, _loop, _phase

    class ProtocolClient(InProcessClient):
        complete = CairnClient.complete

        def _request_json(self, method, path, json):
            assert method == "POST"
            return self._post(path, json)

    class Driver(MockDriver):
        def build_execute(self, worker, prompt, session):
            script = """
import json,sys
context=json.loads(sys.argv[1])
complete={'description':'Two records verified','result':{'count':2}}
if context['phase']=='bootstrap':
    data={'fact':{'description':'Observed source records one and two'},'complete':complete}
else:
    complete['from']=[context['fact_ids'][-1]]
    data={'complete':complete}
print(json.dumps({'accepted':True,'data':data}))
"""
            return DriverResult(argv=[sys.executable, "-c", script, prompt], session=session)

    for module in (bootstrap, reason):
        monkeypatch.setattr(module, "get_driver", lambda *_: Driver())
    workflow = create(client, [stage("find", result=COUNT, bootstrap_enabled=bootstrap_enabled)])
    config = _config(bootstrap=_phase("complete"), reason=_phase("complete", zero_outcomes=["intent"]), explore=_phase("fact"))
    config.runtime.execution = "local"
    loop = _loop(config, ProtocolClient(client), LocalBackend(LocalConfig(workspace_root=str(tmp_path / "runs"))))
    try:
        _dispatch_and_wait(loop)
        after = detail(client, workflow)
        assert after["workflow"]["status"] == "completed"
        assert after["stages"][0]["completion"]["result"] == {"count": 2}
    finally:
        loop.close()


def test_bad_worker_results_pause_after_corrections_without_unlocking(client, tmp_path, monkeypatch):
    import sys
    from cairn.dispatcher.tasks import bootstrap, reason, explore
    from cairn.dispatcher.config import LocalConfig
    from cairn.dispatcher.runtime.local_backend import LocalBackend
    from cairn.dispatcher.protocol.client import CairnClient
    from cairn.dispatcher.workers.adapters.mock import MockDriver
    from cairn.dispatcher.workers.base import DriverResult
    from test_mock_end_to_end import InProcessClient, _config, _dispatch_and_wait, _loop, _phase

    class ProtocolClient(InProcessClient):
        complete = CairnClient.complete
        request_result_recovery = CairnClient.request_result_recovery

        def _request_json(self, method, path, json):
            return self._post(path, json)

    class Driver(MockDriver):
        def build_execute(self, worker, prompt, session):
            script = """
import json,sys
context=json.loads(sys.argv[1])
complete={'description':'Unsupported completion','result':{'count':True}}
if context['phase']=='bootstrap':
    data={'fact':{'description':'No valid count established'},'complete':complete}
elif context['phase']=='reason':
    complete['from']=[context['fact_ids'][-1]]
    data={'complete':complete}
else:
    data={'description':'Attempted correction, count still unknown'}
print(json.dumps({'accepted':True,'data':data}))
"""
            return DriverResult(argv=[sys.executable, "-c", script, prompt], session=session)

    for module in (bootstrap, reason, explore):
        monkeypatch.setattr(module, "get_driver", lambda *_: Driver())
    workflow = create(client, [stage("find", result=COUNT), stage("use", depends_on=[{"stage": "find", "when": "find.result.count > 0"}])])
    config = _config(bootstrap=_phase("complete"), reason=_phase("complete", zero_outcomes=["intent"]), explore=_phase("fact"))
    config.runtime.execution = "local"
    loop = _loop(config, ProtocolClient(client), LocalBackend(LocalConfig(workspace_root=str(tmp_path / "runs"))))
    try:
        for _ in range(8):
            if detail(client, workflow)["workflow"]["status"] == "stopped":
                break
            _dispatch_and_wait(loop)
        after = detail(client, workflow)
        assert after["workflow"]["status"] == "stopped"
        assert after["stages"][0]["result_recovery_attempts"] == 3
        assert after["stages"][0]["completion"] is None
        assert after["stages"][1]["project_id"] is None
        loop._dispatch_available(loop.client.list_projects())
        assert loop.futures == {}
    finally:
        loop.close()


def test_choice_condition_waits_for_success_and_keeps_discovery_result(client):
    workflow = create(client, [stage("choose", result=COUNT, expansion={"mode": "one"}), stage("use", depends_on=[{"stage": "choose", "when": "choose.result.count > 0"}])])
    pid = workflow["stages"][0]["project_id"]
    assert complete(client, pid, result={"count": 2}, goals=[{"id": "a", "goal": "Do method a"}, {"id": "b", "goal": "Do method b"}], selected_goal="a", selection_reason="Method a preferred").status_code == 200
    after = detail(client, workflow)
    assert after["stages"][1]["project_id"] is None
    assert after["stages"][3]["project_id"] is None
    assert complete(client, after["stages"][2]["project_id"]).status_code == 200
    after = detail(client, workflow)
    assert after["stages"][1]["status"] == "active"
    assert after["stages"][3]["option_state"] == "skipped"
    assert after["stages"][0]["completion"]["result"] == {"count": 2}


def test_pause_and_trash_fence_typed_completion(client):
    workflow = create(client, [stage("find", result=COUNT), stage("use", depends_on=[{"stage": "find", "when": "find.result.count > 0"}])])
    pid = workflow["stages"][0]["project_id"]
    wid = workflow["workflow"]["id"]
    assert client.put(f"/workflows/{wid}/status", json={"status": "stopped"}).status_code == 200
    assert complete(client, pid, result={"count": 1}).status_code == 403
    assert detail(client, workflow)["stages"][1]["project_id"] is None
    assert client.delete(f"/workflows/{wid}").status_code == 200
    assert complete(client, pid, result={"count": 1}).status_code == 409
    assert detail(client, workflow)["stages"][1]["project_id"] is None
