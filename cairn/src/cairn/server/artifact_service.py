from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid

from fastapi import HTTPException

from cairn.server.artifact_models import (
    ARTIFACT_RECOVERY_CREATOR, ARTIFACT_RECOVERY_PREFIX, MAX_ARTIFACT_BYTES,
    MAX_ARTIFACT_RECOVERIES, ArtifactRecord, ArtifactRequirement,
)
from cairn.server.services import check_project_active, next_hint_id, next_intent_id, utcnow

ARTIFACT_COLUMNS = "id, workflow_id, stage_id, project_id, path, label, size, sha256, created_at"


def requirements_for_project(conn: sqlite3.Connection, project_id: str) -> list[ArtifactRequirement]:
    from cairn.server.choice_service import stage_attempt
    row = stage_attempt(conn, project_id)
    if row is None:
        return []
    return [ArtifactRequirement.model_validate(item)
            for item in json.loads(row["definition_json"]).get("artifacts", [])]


def _record(row: sqlite3.Row) -> ArtifactRecord:
    return ArtifactRecord(
        id=row["id"], workflow_id=row["workflow_id"], stage_id=row["stage_id"],
        project_id=row["project_id"], path=row["path"], label=row["label"],
        size=row["size"], sha256=row["sha256"], created_at=row["created_at"],
        download_url=f"/artifacts/{row['id']}/download",
    )


def list_artifacts(conn: sqlite3.Connection, project_id: str | None) -> list[ArtifactRecord]:
    if project_id is None:
        return []
    return [_record(row) for row in conn.execute(
        f"SELECT {ARTIFACT_COLUMNS} FROM artifacts WHERE project_id = ? ORDER BY path", (project_id,),
    )]


def store_artifact(conn: sqlite3.Connection, project_id: str, path: str, content: bytes) -> ArtifactRecord:
    project = check_project_active(conn, project_id)
    declaration = next((item for item in requirements_for_project(conn, project_id) if item.path == path), None)
    if declaration is None:
        raise HTTPException(400, "Artifact path is not declared by this stage")
    require_exploration_finished(conn, project_id)
    if len(content) > MAX_ARTIFACT_BYTES:
        raise HTTPException(413, "Artifact exceeds the 10 MiB limit")
    existing = conn.execute(
        "SELECT id FROM artifacts WHERE project_id = ? AND path = ?", (project_id, path),
    ).fetchone()
    artifact_id = existing["id"] if existing else f"artifact_{uuid.uuid4().hex}"
    conn.execute(
        "INSERT INTO artifacts (id, workflow_id, stage_id, project_id, path, label, size, sha256, created_at, content) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(project_id, path) DO UPDATE SET "
        "label=excluded.label, size=excluded.size, sha256=excluded.sha256, created_at=excluded.created_at, content=excluded.content",
        (artifact_id, project["workflow_id"], project["workflow_stage_id"], project_id,
         path, declaration.label, len(content), hashlib.sha256(content).hexdigest(), utcnow(), content),
    )
    return _record(conn.execute(f"SELECT {ARTIFACT_COLUMNS} FROM artifacts WHERE id = ?", (artifact_id,)).fetchone())


def require_stage_artifacts(conn: sqlite3.Connection, project_id: str) -> None:
    required = requirements_for_project(conn, project_id)
    if required:
        require_exploration_finished(conn, project_id)
    present = {item.path for item in list_artifacts(conn, project_id)}
    missing = [item.path for item in required if item.path not in present]
    if missing:
        raise HTTPException(409, {"message": "Required artifacts have not been collected", "missing": missing})


def require_exploration_finished(conn: sqlite3.Connection, project_id: str) -> None:
    if conn.execute(
        "SELECT 1 FROM intents WHERE project_id = ? AND concluded_at IS NULL",
        (project_id,),
    ).fetchone():
        raise HTTPException(409, "Artifact recovery or exploration is still running or pending")


def record_artifact_recovery(
    conn: sqlite3.Connection, project_id: str, description: str, worker: str,
) -> dict[str, str]:
    """Call in a write transaction: deduplicate recovery and persist its spend limit."""
    project = check_project_active(conn, project_id)
    stage = conn.execute("SELECT * FROM workflow_stages WHERE project_id = ?", (project_id,)).fetchone()
    if stage is None or not requirements_for_project(conn, project_id):
        raise HTTPException(400, "Artifact recovery requires a workflow stage with declared outputs")
    existing = conn.execute(
        "SELECT id FROM intents WHERE project_id = ? AND creator = ? AND concluded_at IS NULL",
        (project_id, ARTIFACT_RECOVERY_CREATOR),
    ).fetchone()
    if existing:
        return {"status": "existing", "intent_id": existing["id"]}
    now = utcnow()
    if stage["artifact_recovery_attempts"] >= MAX_ARTIFACT_RECOVERIES:
        # Local import keeps normal workflow metadata independent of recovery controls.
        from cairn.server.workflow_service import set_workflow_status

        hint_id = next_hint_id(conn, project_id)
        message = (
            f"Automatic artifact recovery paused after {MAX_ARTIFACT_RECOVERIES} attempts. "
            "The workflow has been stopped to prevent further automatic spending. "
            "Inspect the output files and recovery intents, correct the problem, then resume "
            f"the workflow to allow another {MAX_ARTIFACT_RECOVERIES} attempts. "
            f"Last collection failure reported by {worker}: {description}"
        )
        conn.execute(
            "INSERT INTO hints (id, project_id, content, creator, created_at) VALUES (?, ?, ?, ?, ?)",
            (hint_id, project_id, message, ARTIFACT_RECOVERY_CREATOR, now),
        )
        conn.execute("UPDATE workflow_stages SET artifact_error = ? WHERE project_id = ?", (message, project_id))
        set_workflow_status(conn, project["workflow_id"], "stopped")
        return {"status": "paused"}
    intent_id = next_intent_id(conn, project_id)
    correction = f"{ARTIFACT_RECOVERY_PREFIX} {description}"
    conn.execute(
        "INSERT INTO intents (id, project_id, description, creator, created_at) VALUES (?, ?, ?, ?, ?)",
        (intent_id, project_id, correction, ARTIFACT_RECOVERY_CREATOR, now),
    )
    conn.execute(
        "INSERT INTO intent_sources (intent_id, project_id, fact_id) VALUES (?, ?, 'origin')",
        (intent_id, project_id),
    )
    conn.execute(
        "UPDATE workflow_stages SET artifact_recovery_attempts = artifact_recovery_attempts + 1 "
        "WHERE project_id = ?", (project_id,),
    )
    return {"status": "scheduled", "intent_id": intent_id}
