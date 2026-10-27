from fastapi import APIRouter

from cairn.server.db import get_conn
from cairn.server.models import CreateWorkflowRequest, UpdateProjectStatusRequest, WorkflowDetail, WorkflowSummary
from cairn.server.workflow_service import (
    create_workflow, set_workflow_status, workflow_detail, workflow_summaries,
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


@router.put("/workflows/{workflow_id}/status", response_model=WorkflowDetail)
def status(workflow_id: str, body: UpdateProjectStatusRequest):
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        return set_workflow_status(conn, workflow_id, body.status)
