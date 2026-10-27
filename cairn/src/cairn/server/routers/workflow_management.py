from fastapi import APIRouter

from cairn.server.db import get_conn
from cairn.server.models import WorkflowDetail, WorkflowSummary
from cairn.server.workflow_management import restore_workflow, trash_summaries, trash_workflow

router = APIRouter(tags=["workflow management"])


@router.get("/workflow-trash", response_model=list[WorkflowSummary])
def list_trash():
    with get_conn() as conn:
        return trash_summaries(conn)


@router.delete("/workflows/{workflow_id}", response_model=WorkflowDetail)
def move_to_trash(workflow_id: str):
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        return trash_workflow(conn, workflow_id)


@router.post("/workflows/{workflow_id}/restore", response_model=WorkflowDetail)
def restore(workflow_id: str):
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        return restore_workflow(conn, workflow_id)
