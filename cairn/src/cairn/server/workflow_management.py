from __future__ import annotations

import sqlite3

from fastapi import HTTPException

from cairn.server.models import WorkflowDetail, WorkflowSummary
from cairn.server.services import clear_project_reason, utcnow
from cairn.server.workflow_service import get_workflow, workflow_detail, workflow_summaries


def require_workflow_writable(conn: sqlite3.Connection, workflow_id: str) -> sqlite3.Row:
    workflow = get_workflow(conn, workflow_id)
    if workflow["deleted_at"] is not None:
        raise HTTPException(409, "Workflow is in Trash; restore it before making changes")
    return workflow


def require_project_writable(conn: sqlite3.Connection, project: sqlite3.Row) -> None:
    if project["workflow_id"] is not None:
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        require_workflow_writable(conn, project["workflow_id"])


def trash_summaries(conn: sqlite3.Connection) -> list[WorkflowSummary]:
    return workflow_summaries(conn, trashed=True)


def trash_workflow(conn: sqlite3.Connection, workflow_id: str) -> WorkflowDetail:
    """Pause and hide a workflow inside the caller's immediate write transaction."""
    workflow = get_workflow(conn, workflow_id)
    if workflow["deleted_at"] is not None:
        return workflow_detail(conn, workflow_id)

    from cairn.server.verification_service import invalidate_review_lease

    for project in conn.execute(
        "SELECT id FROM projects WHERE workflow_id = ? AND status != 'completed'", (workflow_id,),
    ).fetchall():
        project_id = project["id"]
        conn.execute("UPDATE projects SET status = 'stopped' WHERE id = ?", (project_id,))
        conn.execute(
            "UPDATE intents SET worker = NULL, last_heartbeat_at = NULL "
            "WHERE project_id = ? AND concluded_at IS NULL", (project_id,),
        )
        clear_project_reason(conn, project_id)
        invalidate_review_lease(conn, project_id)

    # Keep completed workflows completed, so restoring never changes their result.
    conn.execute(
        "UPDATE workflows SET deleted_at = ?, status = CASE "
        "WHEN status = 'completed' THEN 'completed' ELSE 'stopped' END WHERE id = ?",
        (utcnow(), workflow_id),
    )
    return workflow_detail(conn, workflow_id)


def restore_workflow(conn: sqlite3.Connection, workflow_id: str) -> WorkflowDetail:
    workflow = get_workflow(conn, workflow_id)
    if workflow["deleted_at"] is None:
        return workflow_detail(conn, workflow_id)
    conn.execute(
        "UPDATE workflows SET deleted_at = NULL, status = CASE "
        "WHEN status = 'completed' THEN 'completed' ELSE 'stopped' END WHERE id = ?",
        (workflow_id,),
    )
    # Restoring never activates projects or advances blocked stages. Resume is explicit.
    return workflow_detail(conn, workflow_id)
