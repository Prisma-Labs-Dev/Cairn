from pathlib import PurePosixPath

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from cairn.server.artifact_models import ArtifactRecord, ArtifactRecoveryRequest, MAX_ARTIFACT_BYTES, validate_artifact_path
from cairn.server.artifact_service import list_artifacts, record_artifact_recovery, store_artifact
from cairn.server.db import get_conn
from cairn.server.services import get_project_or_404

router = APIRouter(tags=["artifacts"])


@router.get("/projects/{project_id}/artifacts", response_model=list[ArtifactRecord])
def list_project_artifacts(project_id: str):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        return list_artifacts(conn, project_id)


@router.post("/projects/{project_id}/artifacts", response_model=ArtifactRecord, status_code=201)
async def upload_artifact(project_id: str, path: str, request: Request):
    try:
        validate_artifact_path(path)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    content = bytearray()
    async for chunk in request.stream():
        if len(content) + len(chunk) > MAX_ARTIFACT_BYTES:
            raise HTTPException(413, "Artifact exceeds the 10 MiB limit")
        content.extend(chunk)
    return await run_in_threadpool(_store_upload, project_id, path, bytes(content))


def _store_upload(project_id: str, path: str, content: bytes) -> ArtifactRecord:
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        return store_artifact(conn, project_id, path, content)


@router.post("/projects/{project_id}/artifact-recovery")
def request_artifact_recovery(project_id: str, body: ArtifactRecoveryRequest):
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        return record_artifact_recovery(conn, project_id, body.description, body.worker)


@router.get("/artifacts/{artifact_id}/download")
def download_artifact(artifact_id: str):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM artifacts WHERE id = ?", (artifact_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "Artifact not found")
        # The ASCII fallback is deliberately constant; RFC 5987 carries the original name.
        from urllib.parse import quote
        name = quote(PurePosixPath(row["path"]).name, safe="")
        return Response(
            content=row["content"], media_type="application/octet-stream",
            headers={"Content-Disposition": f"attachment; filename=artifact; filename*=UTF-8''{name}",
                     "X-Content-Type-Options": "nosniff"},
        )
