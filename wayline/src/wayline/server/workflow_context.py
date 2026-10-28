from __future__ import annotations

import json
import sqlite3

from fastapi import HTTPException

from wayline.server.artifact_service import list_artifacts
from wayline.server.workflow_conditions import dependency_stage
from wayline.server.models import Fact, Hint, StageCompletion, StageHandoff, WorkflowStageDefinition
from wayline.server.services import get_project_or_404, intent_to_model, next_hint_id, utcnow

CONTEXT_VERSION = 1
EXCERPT_LENGTH = 512


def history_index_url(project_id: str) -> str:
    return f"/projects/{project_id}/history"


def history_pointer(project_id: str) -> str:
    return (
        f"Earlier completed work is available on demand at {history_index_url(project_id)}. "
        "Read prior context only if useful for this stage's current goal. "
        "You do not need to load history to proceed."
    )


def stage_instructions(workflow_id: str, definition: WorkflowStageDefinition) -> str:
    return (
        f"Workflow {workflow_id}, stage {definition.id} ({definition.kind}). "
        "Achieve this stage's goal only. Other stages have separate goals. "
        "All stages share this working directory. Preserve prerequisite files. "
        f"Write new artifacts under artifacts/{definition.id}/ to avoid overwriting other stages. "
        "Include useful file paths and evidence references in your findings and completion explanation."
    )


def stage_reference(conn: sqlite3.Connection, row: sqlite3.Row, requesting_project_id: str) -> StageHandoff:
    project_id = row["project_id"]
    counts = conn.execute("""
        SELECT
            (SELECT COUNT(*) FROM facts WHERE project_id = ?) AS fact_count,
            (SELECT COUNT(*) FROM intents WHERE project_id = ?) AS intent_count,
            (SELECT COUNT(*) FROM artifacts WHERE project_id = ?) AS artifact_count
    """, (project_id, project_id, project_id)).fetchone()
    return StageHandoff(
        stage_id=row["id"], project_id=project_id,
        fact_count=counts["fact_count"], intent_count=counts["intent_count"], artifact_count=counts["artifact_count"],
        context_url=f"{history_index_url(requesting_project_id)}/{row['id']}",
    )


def _history_scope(conn: sqlite3.Connection, requesting_project_id: str) -> tuple[str, list[str]]:
    project = get_project_or_404(conn, requesting_project_id)
    if project["workflow_id"] is None:
        raise HTTPException(404, "Project has no workflow history")
    from wayline.server.choice_service import stage_attempt
    stage = stage_attempt(conn, requesting_project_id)
    if stage is None:
        raise HTTPException(404, "Workflow stage not found")
    return project["workflow_id"], json.loads(stage["history_stage_ids_json"])


def accessible_stage(conn: sqlite3.Connection, requesting_project_id: str, stage_id: str) -> sqlite3.Row:
    workflow_id, allowed = _history_scope(conn, requesting_project_id)
    if stage_id not in allowed:
        raise HTTPException(404, "Stage is not part of this project's earlier completed work")
    stage = conn.execute(
        "SELECT s.* FROM workflow_stages s JOIN projects p ON p.id = s.project_id "
        "WHERE s.workflow_id = ? AND s.id = ? AND p.status = 'completed' AND s.completion_json IS NOT NULL",
        (workflow_id, stage_id),
    ).fetchone()
    if stage is None:
        raise HTTPException(404, "Completed source stage not found")
    return stage


def _page(items: list, offset: int, limit: int, total: int) -> dict:
    return {"items": items, "offset": offset, "limit": limit, "total": total,
            "next_offset": offset + limit if offset + limit < total else None}


def history_index(conn: sqlite3.Connection, requesting_project_id: str, offset: int, limit: int) -> dict:
    workflow_id, allowed = _history_scope(conn, requesting_project_id)
    if not allowed:
        return _page([], offset, limit, 0)
    placeholders = ",".join("?" for _ in allowed)
    condition = (
        f"s.workflow_id = ? AND s.id IN ({placeholders}) "
        "AND p.status = 'completed' AND s.completion_json IS NOT NULL"
    )
    params = (workflow_id, *allowed)
    total = conn.execute(
        "SELECT COUNT(*) FROM workflow_stages s JOIN projects p ON p.id = s.project_id WHERE " + condition,
        params,
    ).fetchone()[0]
    rows = conn.execute(
        "SELECT s.id, s.project_id FROM workflow_stages s JOIN projects p ON p.id = s.project_id WHERE "
        + condition + " ORDER BY s.position, s.id LIMIT ? OFFSET ?", (*params, limit, offset),
    ).fetchall()
    return _page([stage_reference(conn, row, requesting_project_id).model_dump() for row in rows], offset, limit, total)


def history_stage(conn: sqlite3.Connection, requesting_project_id: str, stage_id: str) -> dict:
    stage = accessible_stage(conn, requesting_project_id, stage_id)
    definition = json.loads(stage["definition_json"])
    reference = stage_reference(conn, stage, requesting_project_id)
    base = reference.context_url
    hint_count = conn.execute("SELECT COUNT(*) FROM hints WHERE project_id = ?", (stage["project_id"],)).fetchone()[0]
    return {**reference.model_dump(), "title": definition["title"][:200], "kind": definition.get("kind", "work"),
            "facts_url": f"{base}/facts", "intents_url": f"{base}/intents",
            "completion_url": f"{base}/completion", "artifacts_url": f"{base}/artifacts",
            "hints_url": f"{base}/hints", "hint_count": hint_count,
            "goal_url": f"{base}/facts/goal", "origin_url": f"{base}/facts/origin"}


def history_facts(conn: sqlite3.Connection, requesting_project_id: str, stage_id: str, offset: int, limit: int) -> dict:
    stage = accessible_stage(conn, requesting_project_id, stage_id)
    total = conn.execute("SELECT COUNT(*) FROM facts WHERE project_id = ?", (stage["project_id"],)).fetchone()[0]
    rows = conn.execute(
        "SELECT id, substr(description,1,?) AS excerpt, length(description) > ? AS truncated "
        "FROM facts WHERE project_id = ? ORDER BY rowid LIMIT ? OFFSET ?",
        (EXCERPT_LENGTH, EXCERPT_LENGTH, stage["project_id"], limit, offset),
    ).fetchall()
    return _page([{**dict(row), "truncated": bool(row["truncated"])} for row in rows], offset, limit, total)


def history_fact(conn: sqlite3.Connection, requesting_project_id: str, stage_id: str, fact_id: str) -> Fact:
    stage = accessible_stage(conn, requesting_project_id, stage_id)
    row = conn.execute("SELECT id, description FROM facts WHERE project_id = ? AND id = ?",
                       (stage["project_id"], fact_id)).fetchone()
    if row is None:
        raise HTTPException(404, "Source fact not found")
    return Fact(**dict(row))


def history_intents(conn: sqlite3.Connection, requesting_project_id: str, stage_id: str, offset: int, limit: int) -> dict:
    stage = accessible_stage(conn, requesting_project_id, stage_id)
    total = conn.execute("SELECT COUNT(*) FROM intents WHERE project_id = ?", (stage["project_id"],)).fetchone()[0]
    rows = conn.execute(
        "SELECT id, to_fact_id AS 'to', substr(description,1,?) AS excerpt, length(description) > ? AS truncated "
        "FROM intents WHERE project_id = ? ORDER BY rowid LIMIT ? OFFSET ?",
        (EXCERPT_LENGTH, EXCERPT_LENGTH, stage["project_id"], limit, offset),
    ).fetchall()
    return _page([{**dict(row), "truncated": bool(row["truncated"])} for row in rows], offset, limit, total)


def history_intent(conn: sqlite3.Connection, requesting_project_id: str, stage_id: str, intent_id: str):
    stage = accessible_stage(conn, requesting_project_id, stage_id)
    row = conn.execute("SELECT * FROM intents WHERE project_id = ? AND id = ?",
                       (stage["project_id"], intent_id)).fetchone()
    if row is None:
        raise HTTPException(404, "Source intent not found")
    return intent_to_model(conn, row, stage["project_id"])


def history_hints(conn: sqlite3.Connection, requesting_project_id: str, stage_id: str, offset: int, limit: int) -> dict:
    stage = accessible_stage(conn, requesting_project_id, stage_id)
    total = conn.execute("SELECT COUNT(*) FROM hints WHERE project_id = ?", (stage["project_id"],)).fetchone()[0]
    rows = conn.execute(
        "SELECT id, substr(content,1,?) AS excerpt, length(content) > ? AS truncated, "
        "substr(creator,1,128) AS creator FROM hints WHERE project_id = ? ORDER BY rowid LIMIT ? OFFSET ?",
        (EXCERPT_LENGTH, EXCERPT_LENGTH, stage["project_id"], limit, offset),
    ).fetchall()
    return _page([{**dict(row), "truncated": bool(row["truncated"])} for row in rows], offset, limit, total)


def history_hint(conn: sqlite3.Connection, requesting_project_id: str, stage_id: str, hint_id: str) -> Hint:
    stage = accessible_stage(conn, requesting_project_id, stage_id)
    row = conn.execute("SELECT * FROM hints WHERE project_id = ? AND id = ?",
                       (stage["project_id"], hint_id)).fetchone()
    if row is None:
        raise HTTPException(404, "Source hint not found")
    return Hint(**dict(row))


def history_completion(conn: sqlite3.Connection, requesting_project_id: str, stage_id: str) -> StageCompletion:
    return StageCompletion.model_validate_json(accessible_stage(conn, requesting_project_id, stage_id)["completion_json"])


def history_artifacts(conn: sqlite3.Connection, requesting_project_id: str, stage_id: str):
    return list_artifacts(conn, accessible_stage(conn, requesting_project_id, stage_id)["project_id"])


def migrate_legacy_stage_contexts(conn: sqlite3.Connection) -> None:
    """Compact only exact copies made by the previous stage-context generator.

    Preserve source projects and all local IDs. Existing references therefore
    remain valid. Legacy history scope uses completed dependency ancestors because
    second-resolution timestamps cannot reliably reconstruct activation order.
    """
    rows = conn.execute("SELECT * FROM workflow_stages WHERE context_version < ?", (CONTEXT_VERSION,)).fetchall()
    for row in rows:
        if row["project_id"] is None:
            conn.execute("UPDATE workflow_stages SET context_version = ? WHERE workflow_id = ? AND id = ?",
                         (CONTEXT_VERSION, row["workflow_id"], row["id"]))
            continue
        all_rows = conn.execute("SELECT * FROM workflow_stages WHERE workflow_id = ?", (row["workflow_id"],)).fetchall()
        by_id = {stage["id"]: stage for stage in all_rows}
        ancestors: set[str] = set()
        definitions = {stage["id"]: WorkflowStageDefinition.model_validate_json(stage["definition_json"]) for stage in all_rows}
        collections = {definition.emits.collection: definition.id for definition in definitions.values() if definition.emits}
        pending = [dependency_stage(dependency, collections) for dependency in definitions[row["id"]].depends_on]
        while pending:
            source_id = pending.pop()
            if source_id in ancestors or source_id not in by_id:
                continue
            ancestors.add(source_id)
            pending.extend(dependency_stage(dependency, collections) for dependency in definitions[source_id].depends_on)
        history_ids = [stage["id"] for stage in all_rows if stage["id"] in ancestors and stage["completion_json"]]
        legacy_handoffs = json.loads(row["handoffs_json"])
        refs = []
        for index, legacy in enumerate(legacy_handoffs):
            source = by_id.get(legacy.get("stage_id"))
            if source is None or source["project_id"] != legacy.get("project_id"):
                continue
            refs.append(stage_reference(conn, source, row["project_id"]).model_dump())
            if "completion_description" in legacy and "from" in legacy:
                sources = ", ".join(f"{legacy['project_id']}/{fid}" for fid in legacy["from"])
                exact = f"Stage {legacy['stage_id']} completed: {legacy['completion_description']}\nEvidence: {sources}"
                pointer = f"Earlier stage {legacy['stage_id']} completed. Optional source context: {history_index_url(row['project_id'])}/{legacy['stage_id']}"
                conn.execute("UPDATE facts SET description = ? WHERE project_id = ? AND description = ? "
                             "AND id = ? AND NOT EXISTS "
                             "(SELECT 1 FROM intents WHERE project_id = facts.project_id AND to_fact_id = facts.id)",
                             (pointer, row["project_id"], exact, f"f{index + 1:03d}"))
        definition = WorkflowStageDefinition.model_validate_json(row["definition_json"])
        current_instructions = stage_instructions(row["workflow_id"], definition)
        legacy_instructions = current_instructions + " Prerequisite findings below are inherited claims, not independent verification."
        conn.execute("UPDATE hints SET content = ? WHERE project_id = ? AND creator = 'workflow' AND content = ?",
                     (current_instructions, row["project_id"], legacy_instructions))
        for hint in conn.execute("SELECT id, content FROM hints WHERE project_id = ? AND creator = 'workflow'", (row["project_id"],)).fetchall():
            prefix = "Prerequisite handoff:\n"
            if not hint["content"].startswith(prefix):
                continue
            try:
                payload = json.loads(hint["content"][len(prefix):])
            except (ValueError, TypeError):
                continue
            if any(payload == legacy for legacy in legacy_handoffs):
                conn.execute("DELETE FROM hints WHERE project_id = ? AND id = ?", (row["project_id"], hint["id"]))
        pointer = history_pointer(row["project_id"])
        if conn.execute("SELECT 1 FROM hints WHERE project_id = ? AND creator = 'workflow' AND content = ?",
                        (row["project_id"], pointer)).fetchone() is None:
            conn.execute("INSERT INTO hints (id,project_id,content,creator,created_at) VALUES (?,?,?,'workflow',?)",
                         (next_hint_id(conn, row["project_id"]), row["project_id"], pointer, utcnow()))
        conn.execute("UPDATE workflow_stages SET handoffs_json = ?, history_stage_ids_json = ?, context_version = ? "
                     "WHERE workflow_id = ? AND id = ?",
                     (json.dumps(refs), json.dumps(history_ids), CONTEXT_VERSION, row["workflow_id"], row["id"]))
