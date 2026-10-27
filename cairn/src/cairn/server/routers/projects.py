from fastapi import APIRouter, HTTPException, Query

from cairn.server.db import get_conn
from cairn.server.verification_models import CompletionClaim
from cairn.server.verification_service import submit_claim, invalidate_review_lease
from cairn.server.artifact_service import requirements_for_project, require_stage_artifacts
from cairn.server.workflow_service import (
    complete_workflow_stage, require_standalone_project, expansion_for_project,
    append_generated_goals, request_expansion_recovery, result_contract_for_project, validate_project_result, request_result_recovery,
)
from cairn.server.models import (
    CompleteRequest, UnavailableRequest,
    ConcludeRequest,
    CreateProjectRequest,
    Fact,
    Hint,
    HeartbeatRequest,
    Intent,
    ProjectDetail,
    ProjectMeta,
    ProjectSummary,
    ReopenRequest,
    ReopenResponse,
    ReasonClaimRequest,
    UpdateProjectTitleRequest,
    UpdateProjectStatusRequest,
    UpdateMetadataRequest,
    StageCompletion,
)
from cairn.server.services import (
    build_intents,
    check_project_completed,
    check_project_active,
    clear_project_reason,
    expire_reason_leases,
    expire_workers,
    get_completion_intent_or_409,
    get_project_or_404,
    intent_to_model,
    next_fact_id,
    next_hint_id,
    next_intent_id,
    next_project_id,
    project_meta_from_row,
    project_reason_from_row,
    utcnow,
    validate_facts_exist,
    validate_goal_not_in_sources,
    update_project_metadata,
)

router = APIRouter(tags=["projects"])


@router.get("/projects", response_model=list[ProjectSummary])
def list_projects():
    with get_conn() as conn:
        expire_workers(conn)
        expire_reason_leases(conn)
        rows = conn.execute("""
            SELECT p.*,
                (SELECT COUNT(*) FROM facts WHERE project_id = p.id) AS fact_count,
                (SELECT COUNT(*) FROM intents WHERE project_id = p.id) AS intent_count,
                (SELECT COUNT(*) FROM intents WHERE project_id = p.id AND concluded_at IS NULL AND worker IS NOT NULL) AS working_intent_count,
                (SELECT COUNT(*) FROM intents WHERE project_id = p.id AND concluded_at IS NULL AND worker IS NULL) AS unclaimed_intent_count,
                (SELECT COUNT(*) FROM hints WHERE project_id = p.id) AS hint_count
            FROM projects p
            ORDER BY p.created_at
        """).fetchall()
        return [
            ProjectSummary(
                id=row["id"],
                title=row["title"],
                status=row["status"],
                bootstrap_enabled=bool(row["bootstrap_enabled"]),
                created_at=row["created_at"],
                reason=project_reason_from_row(row),
                workflow_id=row["workflow_id"],
                workflow_stage_id=row["workflow_stage_id"],
                verification_enabled=bool(row["verification_enabled"]),
                pending_review_id=row["pending_review_id"],
                fact_count=row["fact_count"],
                intent_count=row["intent_count"],
                working_intent_count=row["working_intent_count"],
                unclaimed_intent_count=row["unclaimed_intent_count"],
                hint_count=row["hint_count"],
            )
            for row in rows
        ]


@router.post("/projects", response_model=ProjectDetail, status_code=201)
def create_project(body: CreateProjectRequest):
    with get_conn() as conn:
        pid = next_project_id(conn)
        now = utcnow()

        conn.execute(
            "INSERT INTO projects (id, title, status, bootstrap_enabled, created_at, verification_enabled) VALUES (?, ?, 'active', ?, ?, ?)",
            (pid, body.title, body.bootstrap_enabled, now, body.verification_enabled),
        )
        conn.execute(
            "INSERT INTO facts (id, project_id, description) VALUES (?, ?, ?)",
            ("origin", pid, body.origin),
        )
        conn.execute(
            "INSERT INTO facts (id, project_id, description) VALUES (?, ?, ?)",
            ("goal", pid, body.goal),
        )

        hints = []
        if body.hints:
            for h in body.hints:
                hid = next_hint_id(conn, pid)
                conn.execute(
                    "INSERT INTO hints (id, project_id, content, creator, created_at) VALUES (?, ?, ?, ?, ?)",
                    (hid, pid, h.content, h.creator, now),
                )
                hints.append(Hint(id=hid, content=h.content, creator=h.creator, created_at=now))

        return ProjectDetail(
            project=ProjectMeta(
                id=pid,
                title=body.title,
                status="active",
                bootstrap_enabled=body.bootstrap_enabled,
                verification_enabled=body.verification_enabled,
                created_at=now,
                reason=None,
            ),
            facts=[
                Fact(id="origin", description=body.origin),
                Fact(id="goal", description=body.goal),
            ],
            intents=[],
            hints=hints,
        )


@router.get("/projects/{project_id}", response_model=ProjectDetail)
def get_project(project_id: str):
    with get_conn() as conn:
        expire_workers(conn, project_id)
        expire_reason_leases(conn, project_id)
        row = get_project_or_404(conn, project_id)
        return _project_detail(conn, row)


def _project_detail(conn, row) -> ProjectDetail:
    project_id = row["id"]
    facts = conn.execute("SELECT * FROM facts WHERE project_id = ?", (project_id,)).fetchall()
    hints = conn.execute("SELECT * FROM hints WHERE project_id = ? ORDER BY created_at", (project_id,)).fetchall()
    workflow = conn.execute("SELECT deleted_at FROM workflows WHERE id=?", (row["workflow_id"],)).fetchone() if row["workflow_id"] else None
    return ProjectDetail(
        workflow_deleted_at=workflow["deleted_at"] if workflow else None,
        project=project_meta_from_row(row),
        facts=[Fact(**dict(f)) for f in facts],
        intents=build_intents(conn, project_id),
        hints=[Hint(**dict(h)) for h in hints],
        required_artifacts=requirements_for_project(conn, project_id),
        expansion=expansion_for_project(conn, project_id),
        result_contract=result_contract_for_project(conn, project_id),
        choice_option=conn.execute("SELECT 1 FROM workflow_stages WHERE project_id=? AND option_state='selected'", (project_id,)).fetchone() is not None,
    )


@router.patch("/projects/{project_id}/metadata", response_model=ProjectDetail)
def refine_project_metadata(project_id: str, body: UpdateMetadataRequest):
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        return _project_detail(conn, update_project_metadata(conn, project_id, body))


@router.delete("/projects/{project_id}", status_code=204)
def delete_project(project_id: str):
    with get_conn() as conn:
        require_standalone_project(get_project_or_404(conn, project_id))
        conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))


@router.put("/projects/{project_id}/title", response_model=ProjectMeta)
def update_project_title(project_id: str, body: UpdateProjectTitleRequest):
    with get_conn() as conn:
        project = get_project_or_404(conn, project_id)
        from cairn.server.workflow_management import require_project_writable
        require_project_writable(conn, project)
        conn.execute(
            "UPDATE projects SET title = ? WHERE id = ?",
            (body.title, project_id),
        )
        updated = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        return project_meta_from_row(updated)


@router.put("/projects/{project_id}/status", response_model=ProjectMeta)
def update_project_status(project_id: str, body: UpdateProjectStatusRequest):
    with get_conn() as conn:
        expire_reason_leases(conn, project_id)
        row = get_project_or_404(conn, project_id)
        require_standalone_project(row)
        current_status = row["status"]
        if current_status == "completed":
            raise HTTPException(409, "Completed projects cannot change status")
        if current_status == body.status:
            return project_meta_from_row(row)

        conn.execute(
            "UPDATE projects SET status = ? WHERE id = ?",
            (body.status, project_id),
        )
        if body.status == "active":
            conn.execute("UPDATE projects SET verification_rejections=0 WHERE id=?", (project_id,))
            conn.execute("UPDATE completion_claims SET review_attempts=0 WHERE project_id=? AND status='pending'", (project_id,))
        if body.status == "stopped":
            invalidate_review_lease(conn, project_id)
            conn.execute(
                "UPDATE intents SET worker = NULL WHERE project_id = ? AND concluded_at IS NULL",
                (project_id,),
            )
            clear_project_reason(conn, project_id)
        updated = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        return project_meta_from_row(updated)


@router.post("/projects/{project_id}/reason/claim", response_model=ProjectMeta)
def claim_project_reason(project_id: str, body: ReasonClaimRequest):
    with get_conn() as conn:
        check_project_active(conn, project_id)
        expire_reason_leases(conn, project_id)
        row = get_project_or_404(conn, project_id)
        current_worker = row["reason_worker"]
        if current_worker is not None and current_worker != body.worker:
            raise HTTPException(409, f"Project reason is currently claimed by {current_worker}")
        if current_worker == body.worker:
            return project_meta_from_row(row)

        now = utcnow()
        conn.execute(
            """
            UPDATE projects
            SET reason_worker = ?,
                reason_trigger = ?,
                reason_started_at = ?,
                reason_last_heartbeat_at = ?
            WHERE id = ?
            """,
            (body.worker, body.trigger, now, now, project_id),
        )
        updated = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        return project_meta_from_row(updated)


@router.post("/projects/{project_id}/reason/heartbeat", response_model=ProjectMeta)
def heartbeat_project_reason(project_id: str, body: HeartbeatRequest):
    with get_conn() as conn:
        check_project_active(conn, project_id)
        expire_reason_leases(conn, project_id)
        row = get_project_or_404(conn, project_id)
        current_worker = row["reason_worker"]
        if current_worker is None:
            raise HTTPException(409, "Project reason is not currently claimed")
        if current_worker != body.worker:
            raise HTTPException(409, f"Project reason is currently claimed by {current_worker}")

        now = utcnow()
        conn.execute(
            "UPDATE projects SET reason_last_heartbeat_at = ? WHERE id = ?",
            (now, project_id),
        )
        updated = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        return project_meta_from_row(updated)


@router.post("/projects/{project_id}/reason/release", response_model=ProjectMeta)
def release_project_reason(project_id: str, body: HeartbeatRequest):
    with get_conn() as conn:
        check_project_active(conn, project_id)
        expire_reason_leases(conn, project_id)
        row = get_project_or_404(conn, project_id)
        current_worker = row["reason_worker"]
        if current_worker is None:
            return project_meta_from_row(row)
        if current_worker != body.worker:
            raise HTTPException(409, f"Project reason is currently claimed by {current_worker}")

        clear_project_reason(conn, project_id)
        updated = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        return project_meta_from_row(updated)


@router.post("/projects/{project_id}/complete", response_model=Intent | CompletionClaim)
def complete_project(project_id: str, body: CompleteRequest):
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing_project = get_project_or_404(conn, project_id)
        from cairn.server.workflow_management import require_project_writable
        require_project_writable(conn, existing_project)
        validate_facts_exist(conn, project_id, body.from_)
        validate_goal_not_in_sources(body.from_)
        validate_project_result(conn, project_id, body.result)
        if existing_project["verification_enabled"]:
            require_stage_artifacts(conn, project_id)
            return submit_claim(conn, existing_project, body)
        if existing_project["status"] == "completed" and expansion_for_project(conn, project_id) is not None:
            stage = conn.execute("SELECT completion_json FROM workflow_stages WHERE project_id = ?", (project_id,)).fetchone()
            recorded = StageCompletion.model_validate_json(stage["completion_json"])
            intent = get_completion_intent_or_409(conn, project_id)
            if (recorded.description == body.description and recorded.from_ == body.from_
                    and recorded.goals == body.goals and recorded.selected_goal == body.selected_goal
                    and recorded.selection_reason == body.selection_reason and recorded.result == body.result and intent["worker"] == body.worker):
                return intent_to_model(conn, intent, project_id)
            raise HTTPException(409, "This expandable stage is already completed with different results")
        project = check_project_active(conn, project_id)
        expire_reason_leases(conn, project_id)
        return apply_completion(conn, project, body)


def apply_completion(conn, project, body):
    project_id = project["id"]
    require_stage_artifacts(conn, project_id)
    validate_project_result(conn, project_id, body.result)
    append_generated_goals(conn, project, body.goals, body.selected_goal, body.selection_reason)

    now = utcnow()
    iid = next_intent_id(conn, project_id)

    conn.execute(
        "INSERT INTO intents (id, project_id, to_fact_id, description, creator, worker, last_heartbeat_at, created_at, concluded_at) VALUES (?, ?, 'goal', ?, ?, ?, ?, ?, ?)",
        (iid, project_id, body.description, body.worker, body.worker, now, now, now),
    )
    for fid in body.from_:
        conn.execute(
            "INSERT INTO intent_sources (intent_id, project_id, fact_id) VALUES (?, ?, ?)",
            (iid, project_id, fid),
        )
    conn.execute(
        """
        UPDATE projects
        SET status = 'completed',
            reason_worker = NULL,
            reason_trigger = NULL,
            reason_started_at = NULL,
            reason_last_heartbeat_at = NULL
        WHERE id = ?
        """,
        (project_id,),
    )

    complete_workflow_stage(conn, project, body.description, body.from_, now, body.goals, body.selected_goal, body.selection_reason, body.result)

    return Intent(
        id=iid,
        **{"from": body.from_},
        to="goal",
        description=body.description,
        creator=body.worker,
        worker=body.worker,
        last_heartbeat_at=now,
        created_at=now,
        concluded_at=now,
    )

@router.post("/projects/{project_id}/expansion-recovery")
def expansion_recovery(project_id: str, body: ConcludeRequest):
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        project = check_project_active(conn, project_id)
        return request_expansion_recovery(conn, project, body.description)


@router.post("/projects/{project_id}/result-recovery")
def result_recovery(project_id: str, body: ConcludeRequest):
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        project = check_project_active(conn, project_id)
        return request_result_recovery(conn, project, body.description)


@router.post("/projects/{project_id}/reopen", response_model=ReopenResponse)
def reopen_project(project_id: str, body: ReopenRequest):
    with get_conn() as conn:
        require_standalone_project(get_project_or_404(conn, project_id))
        expire_reason_leases(conn, project_id)
        check_project_completed(conn, project_id)
        completion = get_completion_intent_or_409(conn, project_id)

        source_rows = conn.execute(
            "SELECT fact_id FROM intent_sources WHERE intent_id = ? AND project_id = ? ORDER BY rowid",
            (completion["id"], project_id),
        ).fetchall()
        source_ids = [row["fact_id"] for row in source_rows]
        if not source_ids:
            raise HTTPException(409, "Completion intent is missing its source facts")

        now = utcnow()
        fact_id = next_fact_id(conn, project_id)
        intent_id = next_intent_id(conn, project_id)
        description = body.description
        creator = body.creator

        conn.execute(
            "DELETE FROM intents WHERE id = ? AND project_id = ?",
            (completion["id"], project_id),
        )
        conn.execute(
            "INSERT INTO facts (id, project_id, description) VALUES (?, ?, ?)",
            (fact_id, project_id, description),
        )
        conn.execute(
            "INSERT INTO intents (id, project_id, to_fact_id, description, creator, worker, last_heartbeat_at, created_at, concluded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (intent_id, project_id, fact_id, "external_feedback", creator, creator, now, now, now),
        )
        for source_id in source_ids:
            conn.execute(
                "INSERT INTO intent_sources (intent_id, project_id, fact_id) VALUES (?, ?, ?)",
                (intent_id, project_id, source_id),
            )
        clear_project_reason(conn, project_id)
        conn.execute(
            "UPDATE projects SET status = 'active' WHERE id = ?",
            (project_id,),
        )

        updated_project = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        updated_intent = conn.execute(
            "SELECT * FROM intents WHERE id = ? AND project_id = ?",
            (intent_id, project_id),
        ).fetchone()
        assert updated_project is not None
        assert updated_intent is not None
        return ReopenResponse(
            project=project_meta_from_row(updated_project),
            fact=Fact(id=fact_id, description=description),
            intent=intent_to_model(conn, updated_intent, project_id),
        )


@router.post("/projects/{project_id}/unavailable")
def unavailable(project_id: str, body: UnavailableRequest):
    from cairn.server.choice_service import mark_unavailable
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        project = get_project_or_404(conn, project_id)
        validate_facts_exist(conn, project_id, body.from_)
        validate_goal_not_in_sources(body.from_)
        return mark_unavailable(conn, project, body)


@router.get("/projects/{project_id}/choice-history")
def previous_choice_attempts(project_id: str, offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100)):
    from cairn.server.choice_service import choice_history
    with get_conn() as conn:
        history = choice_history(conn, project_id)
        items = [{**event, 'description': event['description'][:512], 'truncated': len(event['description']) > 512} for event in history[offset:offset + limit]]
        return {'items': items, 'offset': offset, 'limit': limit, 'total': len(history),
                'next_offset': offset + limit if offset + limit < len(history) else None}


@router.get("/projects/{project_id}/choice-history/{source_id}/facts/{fact_id}", response_model=Fact)
def previous_choice_evidence(project_id: str, source_id: str, fact_id: str):
    from cairn.server.choice_service import choice_history_fact
    with get_conn() as conn:
        return choice_history_fact(conn, project_id, source_id, fact_id)
