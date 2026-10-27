from fastapi import APIRouter, Query

from cairn.server.db import get_conn
from cairn.server.models import CreateWorkflowRequest, UpdateProjectStatusRequest, UpdateMetadataRequest, WorkflowDetail, WorkflowSummary, Fact, Hint, Intent, StageCompletion
from cairn.server.artifact_models import ArtifactRecord
from cairn.server import workflow_context
from cairn.server.workflow_service import (
    create_workflow, set_workflow_status, workflow_detail, workflow_summaries, update_workflow_metadata,
)

router = APIRouter(tags=["workflows"])


@router.get("/workflows", response_model=list[WorkflowSummary])
def list_workflows():
    with get_conn() as conn:
        return workflow_summaries(conn)


@router.post("/workflows", response_model=WorkflowDetail, status_code=201)
def create(body: CreateWorkflowRequest):
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        return create_workflow(conn, body)


@router.get("/workflows/{workflow_id}", response_model=WorkflowDetail)
def detail(workflow_id: str):
    with get_conn() as conn:
        return workflow_detail(conn, workflow_id)


@router.patch("/workflows/{workflow_id}/metadata", response_model=WorkflowDetail)
def refine_metadata(workflow_id: str, body: UpdateMetadataRequest):
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        return update_workflow_metadata(conn, workflow_id, body)


@router.put("/workflows/{workflow_id}/status", response_model=WorkflowDetail)
def status(workflow_id: str, body: UpdateProjectStatusRequest):
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        return set_workflow_status(conn, workflow_id, body.status)


@router.get("/projects/{project_id}/history")
def history_index(project_id: str, offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100)):
    with get_conn() as conn:
        return workflow_context.history_index(conn, project_id, offset, limit)


@router.get("/projects/{project_id}/history/{stage_id}")
def history_stage(project_id: str, stage_id: str):
    with get_conn() as conn:
        return workflow_context.history_stage(conn, project_id, stage_id)


@router.get("/projects/{project_id}/history/{stage_id}/facts")
def history_facts(project_id: str, stage_id: str, offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100)):
    with get_conn() as conn:
        return workflow_context.history_facts(conn, project_id, stage_id, offset, limit)


@router.get("/projects/{project_id}/history/{stage_id}/facts/{fact_id}", response_model=Fact)
def history_fact(project_id: str, stage_id: str, fact_id: str):
    with get_conn() as conn:
        return workflow_context.history_fact(conn, project_id, stage_id, fact_id)


@router.get("/projects/{project_id}/history/{stage_id}/intents")
def history_intents(project_id: str, stage_id: str, offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100)):
    with get_conn() as conn:
        return workflow_context.history_intents(conn, project_id, stage_id, offset, limit)


@router.get("/projects/{project_id}/history/{stage_id}/intents/{intent_id}", response_model=Intent)
def history_intent(project_id: str, stage_id: str, intent_id: str):
    with get_conn() as conn:
        return workflow_context.history_intent(conn, project_id, stage_id, intent_id)


@router.get("/projects/{project_id}/history/{stage_id}/hints")
def history_hints(project_id: str, stage_id: str, offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100)):
    with get_conn() as conn:
        return workflow_context.history_hints(conn, project_id, stage_id, offset, limit)


@router.get("/projects/{project_id}/history/{stage_id}/hints/{hint_id}", response_model=Hint)
def history_hint(project_id: str, stage_id: str, hint_id: str):
    with get_conn() as conn:
        return workflow_context.history_hint(conn, project_id, stage_id, hint_id)


@router.get("/projects/{project_id}/history/{stage_id}/completion", response_model=StageCompletion)
def history_completion(project_id: str, stage_id: str):
    with get_conn() as conn:
        return workflow_context.history_completion(conn, project_id, stage_id)


@router.get("/projects/{project_id}/history/{stage_id}/artifacts", response_model=list[ArtifactRecord])
def history_artifacts(project_id: str, stage_id: str):
    with get_conn() as conn:
        return workflow_context.history_artifacts(conn, project_id, stage_id)
