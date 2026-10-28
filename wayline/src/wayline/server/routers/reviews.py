from fastapi import APIRouter, Query, HTTPException
from fastapi.responses import Response
from pathlib import PurePosixPath
from urllib.parse import quote
from wayline.server.db import get_conn
from wayline.server import verification_service as service
from wayline.server.verification_models import CompletionClaim, ReviewIdentity, ReviewLeaseRequest, ReviewDecisionRequest, ReviewPage

router = APIRouter(tags=['reviews'])


@router.get('/reviews', response_model=ReviewPage)
def reviews(project_id: str | None = None, offset: int = Query(default=0, ge=0), limit: int = Query(default=20, ge=1, le=100)):
    with get_conn() as conn:
        return service.claim_summaries(conn, project_id, offset, limit)


@router.get('/reviews/{claim_id}', response_model=CompletionClaim)
def review(claim_id: str):
    with get_conn() as conn:
        return service.get_claim(conn, claim_id)


@router.post('/reviews/{claim_id}/claim', response_model=CompletionClaim)
def claim(claim_id: str, body: ReviewIdentity):
    with get_conn() as conn:
        conn.execute('BEGIN IMMEDIATE')
        return service.claim_review(conn, claim_id, body)


@router.post('/reviews/{claim_id}/heartbeat', response_model=CompletionClaim)
def heartbeat(claim_id: str, body: ReviewLeaseRequest):
    with get_conn() as conn:
        conn.execute('BEGIN IMMEDIATE')
        return service.heartbeat_review(conn, claim_id, body)


@router.post('/reviews/{claim_id}/release', response_model=CompletionClaim)
def release(claim_id: str, body: ReviewLeaseRequest):
    with get_conn() as conn:
        conn.execute('BEGIN IMMEDIATE')
        return service.release_review(conn, claim_id, body)


@router.post('/reviews/{claim_id}/decision', response_model=CompletionClaim)
def decision(claim_id: str, body: ReviewDecisionRequest):
    with get_conn() as conn:
        conn.execute('BEGIN IMMEDIATE')
        return service.decide_review(conn, claim_id, body)


@router.get('/reviews/{claim_id}/artifacts/{artifact_id}/download')
def review_artifact(claim_id: str, artifact_id: str):
    with get_conn() as conn:
        row = conn.execute('SELECT path,content FROM completion_claim_artifacts WHERE claim_id=? AND artifact_id=?', (claim_id, artifact_id)).fetchone()
        if row is None:
            raise HTTPException(404, 'Frozen claim artifact not found')
        name = quote(PurePosixPath(row['path']).name, safe='')
        return Response(content=row['content'], media_type='application/octet-stream',
                        headers={'Content-Disposition': f"attachment; filename=artifact; filename*=UTF-8''{name}", 'X-Content-Type-Options': 'nosniff'})
