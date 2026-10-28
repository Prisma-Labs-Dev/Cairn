from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient
import pytest

from wayline.server import db
from wayline.server.app import app
from wayline.server.models import (
    CreateProjectRequest, CreateWorkflowRequest, DERIVED_TITLE_MAX_LENGTH,
    UpdateMetadataRequest, WorkflowStageDefinition,
)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "simple.db")
    with TestClient(app) as client:
        yield client


def _project(client, payload=None):
    response = client.post("/projects", json=payload or {"goal": "Investigate the question"})
    assert response.status_code == 201, response.text
    return response.json()


def _workflow(client, payload=None):
    response = client.post("/workflows", json=payload or {"stages": [{"goal": "Investigate"}, {"goal": "Review", "depends_on": ["stage_1"]}]})
    assert response.status_code == 201, response.text
    return response.json()


def _fact(detail, fact_id):
    return next(fact["description"] for fact in detail["facts"] if fact["id"] == fact_id)


def _complete(client, project_id):
    response = client.post(f"/projects/{project_id}/complete", json={"from": ["origin"], "description": "Achieved", "worker": "test"})
    assert response.status_code == 200


@pytest.mark.parametrize("metadata", [{}, {"title": None, "origin": None}, {"title": " \n", "origin": " \t"}])
def test_goal_only_project_normalizes_missing_null_and_blank_metadata(client, metadata):
    detail = _project(client, {"goal": " \nInvestigate  the question\nThen write findings\n ", **metadata})
    assert detail["project"]["title"] == "Investigate the question"
    assert _fact(detail, "origin") == ""
    assert _fact(detail, "goal") == "Investigate  the question\nThen write findings"
    assert detail["hints"] == []
    assert client.get(f"/projects/{detail['project']['id']}").json() == detail


def test_explicit_project_metadata_is_trimmed_and_preserved(client):
    title = "A deliberately explicit title " * 10
    detail = _project(client, {"goal": "Goal", "title": f" {title}\n", "origin": " \nKnown starting context\nSecond line \n"})
    assert detail["project"]["title"] == title.strip()
    assert _fact(detail, "origin") == "Known starting context\nSecond line"


def test_derived_title_is_bounded_without_truncating_goal(client):
    goal = "Investigate " + "a" * 10_000 + "\nAnd preserve this complete goal"
    detail = _project(client, {"goal": goal})
    assert len(detail["project"]["title"]) == DERIVED_TITLE_MAX_LENGTH
    assert detail["project"]["title"].endswith("…")
    assert _fact(detail, "goal") == goal


@pytest.mark.parametrize("payload", [
    {}, {"goal": None}, {"goal": " \n "}, {"goal": 123}, {"goal": False},
    {"goal": "Goal", "title": 1}, {"goal": "Goal", "title": []},
    {"goal": "Goal", "origin": False}, {"goal": "Goal", "origin": {}},
    {"goal": "Goal", "titel": "typo"},
])
def test_invalid_project_requests_do_not_create_partial_data(client, payload):
    assert client.post("/projects", json=payload).status_code == 422
    assert client.get("/projects").json() == []


@pytest.mark.parametrize("metadata", [{}, {"title": None, "origin": None}, {"title": " \n", "origin": " \t"}])
def test_minimal_workflow_assigns_position_ids_and_metadata(client, metadata):
    workflow = _workflow(client, {"stages": [{"goal": " Investigate "}, {"goal": " Review ", "depends_on": ["stage_1"]}], **metadata})
    assert workflow["workflow"]["title"] == "Investigate"
    assert workflow["workflow"]["origin"] == ""
    assert [stage["id"] for stage in workflow["stages"]] == ["stage_1", "stage_2"]
    assert [stage["title"] for stage in workflow["stages"]] == ["Investigate", "Review"]
    assert [stage["status"] for stage in workflow["stages"]] == ["active", "blocked"]
    assert _fact(client.get(f"/projects/{workflow['stages'][0]['project_id']}").json(), "origin") == ""


def test_default_workflow_dependencies_still_mean_independent_roots(client):
    workflow = _workflow(client, {"stages": [{"goal": "First"}, {"goal": "Second"}]})
    assert [stage["depends_on"] for stage in workflow["stages"]] == [[], []]
    assert [stage["status"] for stage in workflow["stages"]] == ["active", "active"]


def test_explicit_ids_titles_and_origins_are_preserved_while_missing_ids_use_positions(client):
    workflow = _workflow(client, {"title": " Workflow label ", "origin": " Known context ", "stages": [
        {"id": "named", "title": " Human stage title ", "goal": "First"},
        {"goal": "Second", "title": None, "depends_on": ["named"]},
    ]})
    assert workflow["workflow"]["title"] == "Workflow label"
    assert workflow["workflow"]["origin"] == "Known context"
    assert [(stage["id"], stage["title"]) for stage in workflow["stages"]] == [("named", "Human stage title"), ("stage_2", "Second")]


@pytest.mark.parametrize("first_artifact,expected", [
    ({"path": "outputs/result.bin", "label": " Deliverable "}, "Deliverable"),
    ({"path": "outputs/result.bin"}, "outputs/result.bin"),
])
def test_artifact_only_stage_derives_titles_from_requested_output(client, first_artifact, expected):
    workflow = _workflow(client, {"stages": [{"artifacts": [first_artifact], "title": " "}]})
    assert workflow["workflow"]["title"] == expected
    assert workflow["stages"][0]["title"] == expected
    assert workflow["stages"][0]["goal"].startswith("Produce the required outputs:")


@pytest.mark.parametrize("stages", [
    [{"goal": "A"}, {"id": "stage_1", "goal": "B"}],
    [{"id": "stage_2", "goal": "A"}, {"goal": "B"}],
])
def test_generated_id_collision_is_rejected_instead_of_silently_renumbered(client, stages):
    response = client.post("/workflows", json={"stages": stages})
    assert response.status_code == 422
    assert "position-generated IDs collide" in response.text
    assert client.get("/workflows").json() == []
    assert client.get("/projects").json() == []


@pytest.mark.parametrize("payload", [
    {"stages": [{"id": None, "goal": "A"}]}, {"stages": [{"id": "", "goal": "A"}]},
    {"stages": [{"id": 1, "goal": "A"}]}, {"stages": [{"title": "No goal"}]},
    {"stages": [{"goal": None}]}, {"stages": [{"goal": "A", "title": []}]},
    {"stages": [{"goal": "A"}], "origin": 3}, {"stages": [{"goal": "A"}], "title": False},
    {"stages": [{"goal": "A", "depends_on": ["missing"]}]},
    {"stages": [{"goal": "A", "depends_on": ["stage_2"]}, {"goal": "B", "depends_on": ["stage_1"]}]},
])
def test_invalid_minimal_workflows_still_validate_types_goals_and_dag(client, payload):
    assert client.post("/workflows", json=payload).status_code == 422
    assert client.get("/workflows").json() == []


def test_shared_models_and_openapi_expose_optional_metadata_and_ids(client):
    project_schema = CreateProjectRequest.model_json_schema()
    workflow_schema = CreateWorkflowRequest.model_json_schema()
    stage_schema = workflow_schema["$defs"]["WorkflowStageDefinition"]
    assert project_schema["required"] == ["goal"]
    assert workflow_schema["required"] == ["stages"]
    assert "id" not in stage_schema.get("required", [])
    assert "default" not in stage_schema["properties"]["id"]
    assert stage_schema["anyOf"][0]["required"] == ["goal"]
    assert stage_schema["anyOf"][1]["required"] == ["artifacts"]
    normalized = CreateWorkflowRequest.model_validate({"stages": [{"goal": "First"}, {"artifacts": [{"path": "result.bin"}]}]})
    assert CreateWorkflowRequest.model_validate(normalized.model_dump()).model_dump() == normalized.model_dump()
    internal = CreateWorkflowRequest(stages=[WorkflowStageDefinition(goal="A"), WorkflowStageDefinition(goal="B")])
    assert [stage.id for stage in internal.stages] == ["stage_1", "stage_2"]
    assert UpdateMetadataRequest.model_json_schema()["properties"]["title"]["type"] == "string"
    openapi = client.get("/openapi.json").json()
    assert openapi["components"]["schemas"]["CreateProjectRequest"]["required"] == ["goal"]
    assert "/projects/{project_id}/metadata" in openapi["paths"]
    assert "/workflows/{workflow_id}/metadata" in openapi["paths"]


def test_project_metadata_refinement_preserves_goal_and_triggers_origin_reconsideration(client):
    project = _project(client)
    project_id = project["project"]["id"]
    title_only = client.patch(f"/projects/{project_id}/metadata", json={"title": " Refined title "}).json()
    assert title_only["project"]["title"] == "Refined title" and title_only["hints"] == []
    response = client.patch(f"/projects/{project_id}/metadata", json={"origin": " Known starting situation "})
    assert response.status_code == 200
    updated = response.json()
    assert _fact(updated, "origin") == "Known starting situation"
    assert _fact(updated, "goal") == _fact(project, "goal")
    assert len(updated["facts"]) == len(project["facts"])
    assert len(updated["hints"]) == 1 and updated["hints"][0]["creator"] == "metadata"
    assert "Known starting situation" not in updated["hints"][0]["content"]
    assert len(updated["hints"][0]["content"]) < 200
    repeated = client.patch(f"/projects/{project_id}/metadata", json={"origin": "Known starting situation"}).json()
    assert repeated == updated


@pytest.mark.parametrize("payload", [{}, {"title": None}, {"origin": " "}, {"title": 123},
                                      {"title": "New", "origin": None}, {"goal": "New goal"}])
def test_metadata_patch_rejects_invalid_or_empty_updates_atomically(client, payload):
    project = _project(client)
    workflow = _workflow(client)
    for url, before in [(f"/projects/{project['project']['id']}", project),
                        (f"/workflows/{workflow['workflow']['id']}", workflow)]:
        assert client.patch(f"{url}/metadata", json=payload).status_code == 422
        assert client.get(url).json() == before


def test_project_metadata_refinement_is_active_only_but_display_title_route_stays_compatible(client):
    project = _project(client)
    project_id = project["project"]["id"]
    client.put(f"/projects/{project_id}/status", json={"status": "stopped"})
    assert client.patch(f"/projects/{project_id}/metadata", json={"origin": "New"}).status_code == 403
    client.put(f"/projects/{project_id}/status", json={"status": "active"})
    _complete(client, project_id)
    assert client.patch(f"/projects/{project_id}/metadata", json={"title": "New"}).status_code == 403
    assert client.put(f"/projects/{project_id}/title", json={"title": "Display rename"}).status_code == 200
    assert _fact(client.get(f"/projects/{project_id}").json(), "origin") == ""


def test_workflow_metadata_origin_applies_only_to_future_stages_and_history_stays_immutable(client):
    workflow = _workflow(client)
    workflow_id = workflow["workflow"]["id"]
    first_id = workflow["stages"][0]["project_id"]
    initial_first = client.get(f"/projects/{first_id}").json()
    response = client.patch(f"/workflows/{workflow_id}/metadata", json={"title": " Refined workflow ", "origin": " Future context "})
    assert response.status_code == 200
    assert response.json()["workflow"]["title"] == "Refined workflow"
    assert client.get(f"/projects/{first_id}").json() == initial_first
    assert client.patch(f"/projects/{first_id}/metadata", json={"origin": "Current stage context"}).status_code == 200
    _complete(client, first_id)
    current = client.get(f"/workflows/{workflow_id}").json()["stages"][1]
    second_id = current["project_id"]
    second = client.get(f"/projects/{second_id}").json()
    assert _fact(second, "origin") == "Future context"
    assert second["project"]["title"] == "Refined workflow / Review"
    assert client.get(f"{current['handoffs'][0]['context_url']}/facts/origin").json()["description"] == "Current stage context"
    assert client.patch(f"/projects/{first_id}/metadata", json={"origin": "Rewrite history"}).status_code == 403
    client.put(f"/workflows/{workflow_id}/status", json={"status": "stopped"})
    assert client.patch(f"/workflows/{workflow_id}/metadata", json={"origin": "Later context"}).status_code == 200
    assert _fact(client.get(f"/projects/{second_id}").json(), "origin") == "Future context"
    client.put(f"/workflows/{workflow_id}/status", json={"status": "active"})
    _complete(client, second_id)
    assert client.patch(f"/workflows/{workflow_id}/metadata", json={"title": "Too late"}).status_code == 409


def test_metadata_and_stage_unlock_are_serialized_without_rewriting_earlier_origin(client):
    workflow = _workflow(client)
    workflow_id = workflow["workflow"]["id"]
    first_id = workflow["stages"][0]["project_id"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        patched = pool.submit(client.patch, f"/workflows/{workflow_id}/metadata", json={"origin": "New context"})
        finished = pool.submit(_complete, client, first_id)
        assert patched.result().status_code == 200
        finished.result()
    stages = client.get(f"/workflows/{workflow_id}").json()["stages"]
    assert _fact(client.get(f"/projects/{first_id}").json(), "origin") == ""
    assert _fact(client.get(f"/projects/{stages[1]['project_id']}").json(), "origin") in ("", "New context")
    assert len(client.get("/projects").json()) == 2
