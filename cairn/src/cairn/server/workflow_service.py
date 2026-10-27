from __future__ import annotations

import json
import sqlite3

from fastapi import HTTPException

from cairn.server.artifact_service import list_artifacts
from cairn.server.models import (
    CreateWorkflowRequest, Fact, StageCompletion, StageHandoff, WorkflowDetail,
    WorkflowMeta, WorkflowStage, WorkflowStageDefinition, WorkflowSummary,
)
from cairn.server.services import (
    clear_project_reason, next_fact_id, next_hint_id, next_project_id, utcnow,
)


def get_workflow(conn: sqlite3.Connection, workflow_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM workflows WHERE id = ?", (workflow_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Workflow not found")
    return row


def workflow_detail(conn: sqlite3.Connection, workflow_id: str) -> WorkflowDetail:
    workflow = get_workflow(conn, workflow_id)
    stages = []
    for row in conn.execute(
        "SELECT s.*, p.status AS project_status FROM workflow_stages s "
        "LEFT JOIN projects p ON p.id = s.project_id WHERE s.workflow_id = ? ORDER BY s.position",
        (workflow_id,),
    ):
        stages.append(WorkflowStage(
            **json.loads(row["definition_json"]),
            status=row["project_status"] or "blocked",
            project_id=row["project_id"],
            completion=json.loads(row["completion_json"]) if row["completion_json"] else None,
            handoffs=json.loads(row["handoffs_json"]),
            collected_artifacts=list_artifacts(conn, row["project_id"]),
            artifact_recovery_attempts=row["artifact_recovery_attempts"],
            artifact_error=row["artifact_error"],
        ))
    return WorkflowDetail(workflow=WorkflowMeta(**dict(workflow)), stages=stages)


def workflow_summaries(conn: sqlite3.Connection) -> list[WorkflowSummary]:
    # Home polling needs only counts. Do not load findings, handoff snapshots,
    # definitions or artifact records just to show workflow progress.
    rows = conn.execute("""
        SELECT w.id, w.title, w.origin, w.status, w.created_at,
               COUNT(s.id) AS stage_count,
               SUM(CASE WHEN p.status = 'completed' THEN 1 ELSE 0 END) AS completed_stage_count,
               SUM(CASE WHEN p.status = 'active' THEN 1 ELSE 0 END) AS active_stage_count
        FROM workflows w
        LEFT JOIN workflow_stages s ON s.workflow_id = w.id
        LEFT JOIN projects p ON p.id = s.project_id
        GROUP BY w.id
        ORDER BY w.created_at, w.id
    """).fetchall()
    return [WorkflowSummary(**dict(row)) for row in rows]


def create_workflow(conn: sqlite3.Connection, body: CreateWorkflowRequest) -> WorkflowDetail:
    conn.execute("UPDATE counters SET value = value + 1 WHERE name = 'workflow'")
    number = conn.execute("SELECT value FROM counters WHERE name = 'workflow'").fetchone()["value"]
    workflow_id = f"workflow_{number:03d}"
    conn.execute(
        "INSERT INTO workflows (id, title, origin, created_at) VALUES (?, ?, ?, ?)",
        (workflow_id, body.title, body.origin, utcnow()),
    )
    for position, stage in enumerate(body.stages):
        conn.execute(
            "INSERT INTO workflow_stages (workflow_id, id, position, definition_json) VALUES (?, ?, ?, ?)",
            (workflow_id, stage.id, position, stage.model_dump_json()),
        )
    advance_workflow(conn, workflow_id)
    return workflow_detail(conn, workflow_id)


def _handoff(conn: sqlite3.Connection, row: sqlite3.Row) -> StageHandoff:
    completion = StageCompletion.model_validate_json(row["completion_json"])
    facts = conn.execute(
        "SELECT id, description FROM facts WHERE project_id = ? AND id != 'goal' ORDER BY rowid",
        (row["project_id"],),
    ).fetchall()
    return StageHandoff(
        stage_id=row["id"], project_id=row["project_id"],
        completion_description=completion.description, from_=completion.from_,
        facts=[Fact(**dict(fact)) for fact in facts],
        artifacts=list_artifacts(conn, row["project_id"]),
    )


def _activate_stage(
    conn: sqlite3.Connection, workflow: sqlite3.Row, row: sqlite3.Row,
    definition: WorkflowStageDefinition, handoffs: list[StageHandoff],
) -> None:
    project_id = next_project_id(conn)
    now = utcnow()
    conn.execute(
        "INSERT INTO projects (id, title, bootstrap_enabled, created_at, workflow_id, workflow_stage_id) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (project_id, f"{workflow['title']} / {definition.title}", definition.bootstrap_enabled,
         now, workflow["id"], definition.id),
    )
    for fact_id, description in (("origin", workflow["origin"]), ("goal", definition.goal)):
        conn.execute("INSERT INTO facts (id, project_id, description) VALUES (?, ?, ?)",
                     (fact_id, project_id, description))
    instructions = (
        f"Workflow {workflow['id']}, stage {definition.id} ({definition.kind}). "
        "Achieve this stage's goal only. Other stages have separate goals. "
        "All stages share this working directory. Preserve prerequisite files. "
        f"Write new artifacts under artifacts/{definition.id}/ to avoid overwriting other stages. "
        "Include useful file paths and evidence references in your findings and completion explanation. "
        "Prerequisite findings below are inherited claims, not independent verification."
    )
    hints = [instructions]
    if definition.artifacts:
        paths = ", ".join(artifact.path for artifact in definition.artifacts)
        hints.append(
            f"Required output files: {paths}. Create these files relative to the shared working "
            "directory before reporting completion. The dispatcher will collect them as downloadable "
            "artifacts. File existence does not by itself prove the stage's substantive goal."
        )
    for handoff in handoffs:
        # A local fact lets reason workers cite dependency completion without confusing
        # identically named fact IDs from different projects.
        fact_id = next_fact_id(conn, project_id)
        sources = ", ".join(f"{handoff.project_id}/{fid}" for fid in handoff.from_)
        conn.execute("INSERT INTO facts (id, project_id, description) VALUES (?, ?, ?)", (
            fact_id, project_id,
            f"Stage {handoff.stage_id} completed: {handoff.completion_description}\nEvidence: {sources}",
        ))
        # Bootstrap receives hints instead of the full graph, so it needs the same
        # frozen dependency snapshot available to later reason/explore tasks.
        hints.append("Prerequisite handoff:\n" + handoff.model_dump_json(by_alias=True))
    for content in hints:
        hint_id = next_hint_id(conn, project_id)
        conn.execute(
            "INSERT INTO hints (id, project_id, content, creator, created_at) VALUES (?, ?, ?, ?, ?)",
            (hint_id, project_id, content, "workflow", now),
        )
    conn.execute(
        "UPDATE workflow_stages SET project_id = ?, handoffs_json = ? WHERE workflow_id = ? AND id = ?",
        (project_id, json.dumps([h.model_dump(by_alias=True) for h in handoffs]), workflow["id"], row["id"]),
    )


def advance_workflow(conn: sqlite3.Connection, workflow_id: str) -> None:
    """Call inside the same write transaction that creates/completes a stage."""
    workflow = get_workflow(conn, workflow_id)
    if workflow["status"] != "active":
        return
    rows = conn.execute(
        "SELECT * FROM workflow_stages WHERE workflow_id = ? ORDER BY position", (workflow_id,),
    ).fetchall()
    by_id = {row["id"]: row for row in rows}
    if all(row["completion_json"] is not None for row in rows):
        conn.execute("UPDATE workflows SET status = 'completed' WHERE id = ?", (workflow_id,))
        return
    for row in rows:
        if row["project_id"] is not None:
            continue
        definition = WorkflowStageDefinition.model_validate_json(row["definition_json"])
        if all(by_id[dependency]["completion_json"] is not None for dependency in definition.depends_on):
            _activate_stage(conn, workflow, row, definition,
                            [_handoff(conn, by_id[dependency]) for dependency in definition.depends_on])


def complete_workflow_stage(
    conn: sqlite3.Connection, project: sqlite3.Row, description: str, from_ids: list[str], now: str,
) -> None:
    if project["workflow_id"] is None:
        return
    completion = StageCompletion(project_id=project["id"], description=description,
                                 from_=from_ids, completed_at=now)
    conn.execute(
        "UPDATE workflow_stages SET completion_json = ? WHERE workflow_id = ? AND id = ?",
        (completion.model_dump_json(by_alias=True), project["workflow_id"], project["workflow_stage_id"]),
    )
    advance_workflow(conn, project["workflow_id"])


def set_workflow_status(conn: sqlite3.Connection, workflow_id: str, status: str) -> WorkflowDetail:
    workflow = get_workflow(conn, workflow_id)
    if workflow["status"] == "completed":
        raise HTTPException(409, "Completed workflows cannot change status")
    if workflow["status"] == "stopped" and status == "active":
        conn.execute(
            "UPDATE workflow_stages SET artifact_recovery_attempts = 0, artifact_error = NULL "
            "WHERE workflow_id = ? AND completion_json IS NULL",
            (workflow_id,),
        )
    conn.execute("UPDATE workflows SET status = ? WHERE id = ?", (status, workflow_id))
    projects = conn.execute(
        "SELECT id FROM projects WHERE workflow_id = ? AND status != 'completed'", (workflow_id,),
    ).fetchall()
    for project in projects:
        conn.execute("UPDATE projects SET status = ? WHERE id = ?", (status, project["id"]))
        if status == "stopped":
            conn.execute(
                "UPDATE intents SET worker = NULL WHERE project_id = ? AND concluded_at IS NULL",
                (project["id"],),
            )
            clear_project_reason(conn, project["id"])
    if status == "active":
        advance_workflow(conn, workflow_id)
    return workflow_detail(conn, workflow_id)


def require_standalone_project(project: sqlite3.Row) -> None:
    if project["workflow_id"] is not None:
        raise HTTPException(409, "This project belongs to a workflow; control it through the workflow")
