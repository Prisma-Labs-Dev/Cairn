from datetime import datetime, timezone
from fastapi import APIRouter, Query
from cairn.server.db import get_conn
from cairn.server.usage_models import UsageAttempt, UsageReporter
from cairn.server.usage_service import record_attempt, usage_attempts, usage_summary

router = APIRouter(tags=["usage"])


@router.post("/usage/attempts")
def record(body: UsageAttempt):
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        return record_attempt(conn, body)


@router.get("/usage/summary")
def summary(since: datetime | None = None, until: datetime | None = None, project_id: str | None = None):
    with get_conn() as conn:
        return usage_summary(conn, since, until, project_id)


@router.get("/usage/attempts")
def attempts(since: datetime | None = None, until: datetime | None = None, project_id: str | None = None,
             offset: int = Query(0, ge=0), limit: int = Query(25, ge=1, le=100)):
    with get_conn() as conn:
        return usage_attempts(conn, since, until, project_id, offset, limit)


@router.post("/usage/reporters")
def report_status(body: UsageReporter):
    with get_conn() as conn:
        conn.execute("INSERT INTO usage_reporters(reporter_id,last_seen,pending_attempts,running_attempts,rejected_attempts) VALUES(?,?,?,?,?) "
                     "ON CONFLICT(reporter_id) DO UPDATE SET last_seen=excluded.last_seen,pending_attempts=excluded.pending_attempts,running_attempts=excluded.running_attempts,rejected_attempts=excluded.rejected_attempts",
                     (body.reporter_id, datetime.now(timezone.utc).isoformat(), body.pending_attempts, body.running_attempts, body.rejected_attempts))
    return {"ok": True}
