from __future__ import annotations

import json
import sqlite3
import hashlib

from fastapi import HTTPException
from pydantic import ValidationError

from cairn.server.artifact_service import list_artifacts
from cairn.server.choice_service import is_choice, choice_satisfied, record_option_event, finish_selected_option
from cairn.server.models import (
    CreateWorkflowRequest, StageCompletion, WorkflowDetail,
    WorkflowMeta, WorkflowStage, WorkflowStageDefinition, WorkflowSummary, UpdateMetadataRequest,
    GeneratedGoalDefinition, GoalExpansion,
)
from cairn.server.services import (
    clear_project_reason, next_hint_id, next_intent_id, next_project_id, utcnow,
)
from cairn.server.workflow_context import CONTEXT_VERSION, history_index_url, history_pointer, stage_instructions, stage_reference


def get_workflow(conn: sqlite3.Connection, workflow_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM workflows WHERE id = ?", (workflow_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Workflow not found")
    return row


def workflow_detail(conn: sqlite3.Connection, workflow_id: str) -> WorkflowDetail:
    workflow = get_workflow(conn, workflow_id)
    stages = []
    for row in conn.execute(
        "SELECT s.*, p.status AS project_status, p.pending_review_id FROM workflow_stages s "
        "LEFT JOIN projects p ON p.id = s.project_id WHERE s.workflow_id = ? ORDER BY s.position",
        (workflow_id,),
    ):
        stages.append(WorkflowStage(
            **json.loads(row["definition_json"]),
            status=row["project_status"] or "blocked",
            project_id=row["project_id"],
            pending_review_id=row["pending_review_id"],
            completion=json.loads(row["completion_json"]) if row["completion_json"] else None,
            handoffs=json.loads(row["handoffs_json"]),
            collected_artifacts=list_artifacts(conn, row["project_id"]),
            artifact_recovery_attempts=row["artifact_recovery_attempts"],
            artifact_error=row["artifact_error"],
            history_index_url=history_index_url(row["project_id"]) if row["project_id"] else None,
            generated_by_stage_id=row["generated_by_stage_id"],
            expansion_recovery_attempts=row["expansion_recovery_attempts"],
            expansion_error=row["expansion_error"],
            option_state=row["option_state"], option_reason=row["option_reason"],
            option_history=json.loads(row["option_history_json"]), choice_error=row["choice_error"],
            choice_satisfied=choice_satisfied(conn, workflow_id, row["id"]) if is_choice(row) else None,
        ))
    return WorkflowDetail(workflow=WorkflowMeta(**dict(workflow)), stages=stages)


def workflow_summaries(conn: sqlite3.Connection, *, trashed: bool = False) -> list[WorkflowSummary]:
    # Home polling needs only counts. Do not load findings, handoff snapshots,
    # definitions or artifact records just to show workflow progress.
    rows = conn.execute("""
        SELECT w.id, w.title, w.origin, w.status, w.created_at, w.deleted_at,
               SUM(CASE WHEN s.option_state IN ('pending','unavailable','skipped') THEN 0 ELSE 1 END)
               + SUM(CASE WHEN s.choice_error IS NOT NULL THEN 1 ELSE 0 END) AS stage_count,
               SUM(CASE WHEN p.status = 'completed' THEN 1 ELSE 0 END) AS completed_stage_count,
               SUM(CASE WHEN p.status = 'active' THEN 1 ELSE 0 END) AS active_stage_count
        FROM workflows w
        LEFT JOIN workflow_stages s ON s.workflow_id = w.id
        LEFT JOIN projects p ON p.id = s.project_id
        WHERE (w.deleted_at IS NOT NULL) = ?
        GROUP BY w.id
        ORDER BY w.created_at, w.id
    """, (trashed,)).fetchall()
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


def _activate_stage(
    conn: sqlite3.Connection, workflow: sqlite3.Row, row: sqlite3.Row,
    definition: WorkflowStageDefinition, source_stages: list[sqlite3.Row], history_ids: list[str],
) -> None:
    project_id = next_project_id(conn)
    now = utcnow()
    conn.execute(
        "INSERT INTO projects (id, title, bootstrap_enabled, created_at, workflow_id, workflow_stage_id, verification_enabled) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (project_id, f"{workflow['title']} / {definition.title}", definition.bootstrap_enabled,
         now, workflow["id"], definition.id, definition.verification_enabled),
    )
    for fact_id, description in (("origin", workflow["origin"]), ("goal", definition.goal)):
        conn.execute("INSERT INTO facts (id, project_id, description) VALUES (?, ?, ?)",
                     (fact_id, project_id, description))
    hints = [stage_instructions(workflow["id"], definition), history_pointer(project_id)]
    if row["option_state"] == "selected":
        hints.append("This is the selected choice option. Preserve its goal and hard constraints. "
                     "If evidence shows this option cannot achieve its goal, report unavailable with evidence references "
                     "and the unmet goal; do not claim completion. Selection: " + (row["option_reason"] or "")
                     + f" Optional earlier unavailable attempts and evidence references: /projects/{project_id}/choice-history. "
                     "The optional history reader supports choice-list and choice-fact SOURCE_PROJECT FACT_ID. "
                     "Read only if useful; no history read is required to begin.")
    if definition.expansion:
        hints.append(
            f"This stage may generate at most {definition.expansion.max_goals} independent follow-up goals. "
            "At actual completion include an explicit goals array in the complete object, even if empty. "
            "Each item requires a concise goal and may specify id, title, kind, bootstrap_enabled, and artifacts. "
            "Do not supply dependencies or further expansion. "
            + ("Mode one requires ranked alternatives, selected_goal, and selection_reason; only one option runs."
               if definition.expansion.mode == "one" else "All proposed goals are required and run in parallel.")
        )
    if definition.artifacts:
        paths = ", ".join(artifact.path for artifact in definition.artifacts)
        hints.append(
            f"Required output files: {paths}. Create these files relative to the shared working "
            "directory before reporting completion. The dispatcher will collect them as downloadable "
            "artifacts. File existence does not by itself prove the stage's substantive goal."
        )
    for content in hints:
        hint_id = next_hint_id(conn, project_id)
        conn.execute(
            "INSERT INTO hints (id, project_id, content, creator, created_at) VALUES (?, ?, ?, ?, ?)",
            (hint_id, project_id, content, "workflow", now),
        )
    conn.execute(
        "UPDATE workflow_stages SET project_id = ?, handoffs_json = ?, history_stage_ids_json = ?, context_version = ? "
        "WHERE workflow_id = ? AND id = ?",
        (project_id, json.dumps([stage_reference(conn, source, project_id).model_dump() for source in source_stages]),
         json.dumps(history_ids), CONTEXT_VERSION, workflow["id"], row["id"]),
    )


def advance_workflow(conn: sqlite3.Connection, workflow_id: str) -> None:
    """Call inside the same write transaction that creates/completes a stage."""
    workflow = get_workflow(conn, workflow_id)
    if workflow["status"] != "active" or workflow["deleted_at"] is not None:
        return
    rows = conn.execute("SELECT * FROM workflow_stages WHERE workflow_id = ? ORDER BY position", (workflow_id,)).fetchall()
    by_id = {row["id"]: row for row in rows}
    def satisfied(row):
        return bool(row["completion_json"]) and (not is_choice(row) or choice_satisfied(conn, workflow_id, row["id"]))
    for row in rows:
        if row["choice_error"]:
            set_workflow_status(conn, workflow_id, "stopped")
            return
    required = [row for row in rows if row["option_state"] not in ("pending", "unavailable", "skipped")]
    if all(satisfied(row) for row in required):
        conn.execute("UPDATE workflows SET status = 'completed' WHERE id = ?", (workflow_id,))
        return
    for row in rows:
        if row["project_id"] is not None or row["option_state"] in ("pending", "unavailable", "skipped"):
            continue
        definition = WorkflowStageDefinition.model_validate_json(row["definition_json"])
        def dependency_ready(dependency):
            source = by_id[dependency]
            # A selected option starts from discovery's raw completion. Other
            # successors wait until that discovery's choice has actually succeeded.
            if row["option_state"] == "selected" and row["generated_by_stage_id"] == dependency:
                return bool(source["completion_json"])
            return satisfied(source)
        if all(dependency_ready(dependency) for dependency in definition.depends_on):
            _activate_stage(conn, workflow, row, definition,
                            [by_id[dependency] for dependency in definition.depends_on],
                            [source["id"] for source in rows if source["completion_json"]])


def complete_workflow_stage(
    conn: sqlite3.Connection, project: sqlite3.Row, description: str, from_ids: list[str], now: str,
    goals: list[GeneratedGoalDefinition] | None = None, selected_goal: str | None = None,
    selection_reason: str | None = None,
) -> None:
    if project["workflow_id"] is None:
        return
    completion = StageCompletion(project_id=project["id"], description=description,
                                 from_=from_ids, completed_at=now, goals=goals,
                                 selected_goal=selected_goal, selection_reason=selection_reason)
    conn.execute(
        "UPDATE workflow_stages SET completion_json = ? WHERE workflow_id = ? AND id = ?",
        (completion.model_dump_json(by_alias=True), project["workflow_id"], project["workflow_stage_id"]),
    )
    finish_selected_option(conn, project, description)
    advance_workflow(conn, project["workflow_id"])


def set_workflow_status(conn: sqlite3.Connection, workflow_id: str, status: str) -> WorkflowDetail:
    from cairn.server.workflow_management import require_workflow_writable
    workflow = require_workflow_writable(conn, workflow_id)
    if workflow["status"] == "completed":
        raise HTTPException(409, "Completed workflows cannot change status")
    if status == "active" and conn.execute("SELECT 1 FROM workflow_stages WHERE workflow_id=? AND choice_error IS NOT NULL", (workflow_id,)).fetchone():
        raise HTTPException(409, "A choice is exhausted. Retry an unavailable option with new circumstances or create a revised workflow before resuming.")
    if workflow["status"] == "stopped" and status == "active":
        conn.execute(
            "UPDATE workflow_stages SET artifact_recovery_attempts = 0, artifact_error = NULL, "
            "expansion_recovery_attempts = 0, expansion_error = NULL "
            "WHERE workflow_id = ? AND completion_json IS NULL",
            (workflow_id,),
        )
    conn.execute("UPDATE workflows SET status = ? WHERE id = ?", (status, workflow_id))
    projects = conn.execute(
        "SELECT p.id FROM projects p JOIN workflow_stages s ON s.project_id=p.id "
        "WHERE p.workflow_id = ? AND p.status != 'completed' AND (s.option_state IS NULL OR s.option_state='selected')", (workflow_id,),
    ).fetchall()
    for project in projects:
        conn.execute("UPDATE projects SET status = ? WHERE id = ?", (status, project["id"]))
        if status == "active":
            conn.execute("UPDATE projects SET verification_rejections=0 WHERE id=?", (project["id"],))
            conn.execute("UPDATE completion_claims SET review_attempts=0 WHERE project_id=? AND status='pending'", (project["id"],))
        if status == "stopped":
            from cairn.server.verification_service import invalidate_review_lease
            invalidate_review_lease(conn, project["id"])
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


def update_workflow_metadata(
    conn: sqlite3.Connection, workflow_id: str, body: UpdateMetadataRequest,
) -> WorkflowDetail:
    from cairn.server.workflow_management import require_workflow_writable
    workflow = require_workflow_writable(conn, workflow_id)
    if workflow["status"] == "completed":
        raise HTTPException(409, "Completed workflow metadata cannot change")
    if body.title is not None:
        conn.execute("UPDATE workflows SET title = ? WHERE id = ?", (body.title, workflow_id))
    if body.origin is not None:
        # Existing stage origin facts retain the context with which they started.
        # Only subsequently unlocked stages read the workflow's updated origin.
        conn.execute("UPDATE workflows SET origin = ? WHERE id = ?", (body.origin, workflow_id))
    return workflow_detail(conn, workflow_id)


def expansion_for_project(conn: sqlite3.Connection, project_id: str) -> GoalExpansion | None:
    row = conn.execute("SELECT definition_json FROM workflow_stages WHERE project_id = ?", (project_id,)).fetchone()
    if row is None:
        return None
    config = json.loads(row["definition_json"]).get("expansion")
    return GoalExpansion.model_validate(config) if config is not None else None


def _generated_goal_id(parent_id: str, index: int) -> str:
    candidate = f"{parent_id}_goal_{index}"
    if len(candidate) <= 64:
        return candidate
    suffix = f"_{hashlib.sha256(parent_id.encode()).hexdigest()[:8]}_goal_{index}"
    return parent_id[:64 - len(suffix)] + suffix


def append_generated_goals(
    conn: sqlite3.Connection, project: sqlite3.Row, goals: list[GeneratedGoalDefinition] | None,
    selected_goal: str | None = None, selection_reason: str | None = None,
) -> None:
    """Validate the combined DAG and append in the caller's completion transaction."""
    expansion = expansion_for_project(conn, project["id"])
    if expansion is None:
        if selected_goal is not None or selection_reason is not None:
            raise HTTPException(409, "Selection fields require expansion mode one")
        if goals:
            raise HTTPException(409, "This stage does not permit generated goals")
        return
    if goals is None:
        raise HTTPException(409, "Expandable stage completion requires an explicit goals array; use [] when none were found")
    if len(goals) > expansion.max_goals:
        raise HTTPException(409, f"This stage permits at most {expansion.max_goals} generated goals")
    if conn.execute("SELECT 1 FROM intents WHERE project_id = ? AND creator = 'goal-expansion' AND concluded_at IS NULL",
                    (project["id"],)).fetchone():
        raise HTTPException(409, "Finish the pending goal expansion correction before completing this stage")
    if expansion.mode == "all" and (selected_goal is not None or selection_reason is not None):
        raise HTTPException(409, "Selection fields require expansion mode one")
    if expansion.mode == "one" and not (selection_reason and selection_reason.strip()):
        raise HTTPException(409, "Choice completion requires selection_reason explaining the decision")
    if not goals:
        if expansion.mode == "one":
            if selected_goal is not None:
                raise HTTPException(409, "An empty choice cannot select a goal")
            error = "No viable alternatives were found. Create a revised workflow with new information or constraints. Reason: " + selection_reason
            conn.execute("UPDATE workflow_stages SET choice_error=? WHERE project_id=?", (error, project["id"]))
        return
    workflow_id = project["workflow_id"]
    parent_id = project["workflow_stage_id"]
    rows = conn.execute("SELECT id,position,definition_json,project_id FROM workflow_stages WHERE workflow_id = ? ORDER BY position",
                        (workflow_id,)).fetchall()
    definitions = [WorkflowStageDefinition.model_validate_json(row["definition_json"]) for row in rows]
    children = [WorkflowStageDefinition(
        **{**goal.model_dump(exclude_none=True), "id": goal.id or _generated_goal_id(parent_id, index), "depends_on": [parent_id],
            "verification_enabled": bool(project["verification_enabled"])},
    ) for index, goal in enumerate(goals, 1)]
    child_ids = [child.id for child in children]
    if expansion.mode == "one" and selected_goal not in child_ids:
        raise HTTPException(409, "selected_goal must reference one of the ranked generated goal IDs")
    updated_successors = []
    for row, definition in zip(rows, definitions):
        if expansion.mode == "all" and parent_id in definition.depends_on:
            if row["project_id"] is not None:
                raise HTTPException(409, "A direct successor has already started; expansion cannot change its dependencies")
            definition.depends_on = [*definition.depends_on, *child_ids]
            updated_successors.append(definition)
    workflow = get_workflow(conn, workflow_id)
    try:
        CreateWorkflowRequest(title=workflow["title"], origin=workflow["origin"], stages=[*definitions, *children])
    except ValidationError as exc:
        raise HTTPException(409, {"message": "Generated goals would create an invalid workflow",
                                  "errors": exc.errors(include_url=False, include_context=False, include_input=False)}) from exc
    for definition in updated_successors:
        conn.execute("UPDATE workflow_stages SET definition_json = ? WHERE workflow_id = ? AND id = ?",
                     (definition.model_dump_json(), workflow_id, definition.id))
    next_position = max(row["position"] for row in rows) + 1
    for index, child in enumerate(children):
        conn.execute(
            "INSERT INTO workflow_stages(workflow_id,id,position,definition_json,generated_by_stage_id) VALUES(?,?,?,?,?)",
            (workflow_id, child.id, next_position + index, child.model_dump_json(), parent_id),
        )
        if expansion.mode == "one":
            row = conn.execute("SELECT * FROM workflow_stages WHERE workflow_id=? AND id=?", (workflow_id, child.id)).fetchone()
            record_option_event(conn, row, "selected" if child.id == selected_goal else "pending", selection_reason)



def request_expansion_recovery(conn: sqlite3.Connection, project: sqlite3.Row, description: str) -> dict:
    expansion = expansion_for_project(conn, project["id"])
    if expansion is None:
        raise HTTPException(409, "This project has no goal expansion enabled")
    existing = conn.execute("SELECT id FROM intents WHERE project_id = ? AND creator = 'goal-expansion' AND concluded_at IS NULL",
                            (project["id"],)).fetchone()
    if existing is not None:
        return {"status": "existing", "intent_id": existing["id"]}
    stage = conn.execute("SELECT expansion_recovery_attempts FROM workflow_stages WHERE project_id = ?", (project["id"],)).fetchone()
    attempts = stage["expansion_recovery_attempts"]
    if attempts >= 3:
        error = (
            "Goal expansion paused after 3 correction attempts. Fix the completion response, then explicitly resume the workflow. "
            + description[:1_000]
        )
        conn.execute("UPDATE workflow_stages SET expansion_error = ? WHERE project_id = ?", (error, project["id"]))
        hint_id = next_hint_id(conn, project["id"])
        conn.execute("INSERT INTO hints(id,project_id,content,creator,created_at) VALUES(?,?,?,?,?)",
                     (hint_id, project["id"], error, "goal-expansion", utcnow()))
        set_workflow_status(conn, project["workflow_id"], "stopped")
        return {"status": "paused", "attempts": attempts}
    intent_id = next_intent_id(conn, project["id"])
    correction = (
        "Goal expansion correction: " + description[:1_000] +
        f" Complete this stage's original goal, then supply an explicit goals array with at most {expansion.max_goals} items, "
        "or [] if there are no follow-up goals. Each item requires goal and may include id, title, kind, bootstrap_enabled, artifacts. "
        "Do not include depends_on or expansion. Preserve other completion criteria."
    )
    conn.execute("INSERT INTO intents(id,project_id,description,creator,created_at) VALUES(?,?,?,?,?)",
                 (intent_id, project["id"], correction, "goal-expansion", utcnow()))
    conn.execute("INSERT INTO intent_sources(intent_id,project_id,fact_id) VALUES(?,?,'origin')", (intent_id, project["id"]))
    conn.execute("UPDATE workflow_stages SET expansion_recovery_attempts = expansion_recovery_attempts + 1 WHERE project_id = ?",
                 (project["id"],))
    return {"status": "scheduled", "attempts": attempts + 1, "intent_id": intent_id}
