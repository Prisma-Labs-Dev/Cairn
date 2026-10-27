from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import sqlite3

from fastapi import HTTPException
from cairn.server.usage_models import TOKEN_FIELDS, UsageAttempt


def record_attempt(conn: sqlite3.Connection, attempt: UsageAttempt) -> dict:
    encoded = json.dumps(attempt.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(encoded.encode()).hexdigest()
    existing = conn.execute("SELECT payload_hash FROM usage_attempts WHERE attempt_id = ?", (str(attempt.attempt_id),)).fetchone()
    if existing is not None:
        if existing["payload_hash"] != digest:
            raise HTTPException(409, "Attempt ID already exists with different usage data")
        return {"attempt_id": str(attempt.attempt_id), "duplicate": True}
    project = conn.execute("SELECT workflow_id FROM projects WHERE id = ?", (attempt.project_id,)).fetchone()
    workflow_id = project["workflow_id"] if project else None
    conn.execute("INSERT INTO usage_attempts(attempt_id,project_id,workflow_id,worker_type,phase,started_at,coverage,payload_hash,payload_json) VALUES(?,?,?,?,?,?,?,?,?)",
        (str(attempt.attempt_id), attempt.project_id, workflow_id, attempt.worker_type, attempt.phase,
         attempt.started_at.isoformat(), attempt.coverage, digest, encoded))
    for index, reading in enumerate(attempt.readings):
        conn.execute("INSERT INTO usage_readings(attempt_id,position,model,model_source,input_tokens,cached_input_tokens,uncached_input_tokens,cache_write_input_tokens,output_tokens) VALUES(?,?,?,?,?,?,?,?,?)",
            (str(attempt.attempt_id), index, reading.model, reading.model_source, *(getattr(reading, field) for field in TOKEN_FIELDS)))
    return {"attempt_id": str(attempt.attempt_id), "duplicate": False}


def _filters(since: datetime | None, until: datetime | None, project_id: str | None) -> tuple[str, list, dict]:
    values, clauses, normalized = [], [], {}
    for name, value in (("since", since), ("until", until)):
        if value is not None:
            if value.tzinfo is None:
                raise HTTPException(422, "Usage time filters require a timezone")
            value = value.astimezone(timezone.utc)
            normalized[name] = value.isoformat()
            clauses.append(f"a.started_at {'>=' if name == 'since' else '<'} ?")
            values.append(value.isoformat())
        else:
            normalized[name] = None
    if since is not None and until is not None and since >= until:
        raise HTTPException(422, "since must precede until")
    if project_id:
        clauses.append("a.project_id = ?")
        values.append(project_id)
    normalized["project_id"] = project_id
    return " AND ".join(clauses) or "1=1", values, normalized


SESSION_COVERAGE_LIMIT = 25


def _session_coverage(conn, where, params):
    # Pi capture and usage share the exact invocation UUID. Count only known
    # registrations, without reading transcript bytes or JSON payload columns.
    missing = "NOT EXISTS (SELECT 1 FROM usage_attempts u WHERE u.attempt_id=a.id AND u.project_id=a.project_id)"
    counts = conn.execute(
        "SELECT COUNT(*) AS known, COALESCE(SUM(" + missing + "),0) AS missing "
        "FROM agent_invocations a WHERE " + where, params,
    ).fetchone()
    rows = conn.execute(
        "SELECT a.id AS invocation_id,a.project_id,a.workflow_id,a.started_at "
        "FROM agent_invocations a WHERE (" + where + ") AND " + missing +
        " ORDER BY a.started_at DESC,a.id DESC LIMIT ?", (*params, SESSION_COVERAGE_LIMIT),
    ).fetchall()
    return {"known_pi_invocations": counts["known"], "missing_usage_attempts": counts["missing"],
            "missing_usage_items": [dict(row) for row in rows], "limit": SESSION_COVERAGE_LIMIT,
            "has_more": counts["missing"] > SESSION_COVERAGE_LIMIT}


def usage_summary(conn: sqlite3.Connection, since=None, until=None, project_id=None) -> dict:
    where, params, window = _filters(since, until, project_id)
    counts = conn.execute("SELECT COUNT(*) AS attempts, SUM(a.coverage='reported') AS reported_attempts, SUM(a.coverage='partial') AS partial_attempts, SUM(a.coverage='unreported') AS unreported_attempts "
                         f"FROM usage_attempts a WHERE {where}", params).fetchone()
    columns = ",".join(f"SUM(r.{field}) AS {field}, COUNT(r.{field}) AS {field}_reports" for field in TOKEN_FIELDS)
    rows = conn.execute("SELECT r.model,r.model_source,a.worker_type,COUNT(DISTINCT a.attempt_id) AS attempts, "
        "COUNT(DISTINCT CASE WHEN a.coverage='reported' THEN a.attempt_id END) AS reported_attempts, "
        "COUNT(DISTINCT CASE WHEN a.coverage='partial' THEN a.attempt_id END) AS partial_attempts, "
        "COUNT(DISTINCT CASE WHEN a.coverage='unreported' THEN a.attempt_id END) AS unreported_attempts, "
        + columns + " FROM usage_readings r JOIN usage_attempts a ON a.attempt_id=r.attempt_id WHERE "
        + where + " GROUP BY r.model,r.model_source,a.worker_type ORDER BY r.model,r.model_source,a.worker_type", params).fetchall()
    models = []
    for row in rows:
        data = dict(row)
        reports = {field: data.pop(f"{field}_reports") for field in TOKEN_FIELDS}
        models.append({**data, "metric_reports": reports})
    totals = {key: int(counts[key] or 0) for key in counts.keys()}
    for field in TOKEN_FIELDS:
        present = [model[field] for model in models if model[field] is not None]
        totals[field] = sum(present) if present else None
    totals["metric_reports"] = {field: sum(model["metric_reports"][field] for model in models) for field in TOKEN_FIELDS}
    phases = conn.execute(f"SELECT phase,COUNT(*) AS attempts FROM usage_attempts a WHERE {where} GROUP BY phase", params).fetchall()
    reporters = [dict(row) for row in conn.execute("SELECT * FROM usage_reporters ORDER BY last_seen DESC")]
    return {"window": window, "totals": totals, "models": models, "phase_attempts": {row["phase"]: row["attempts"] for row in phases},
            "reporting": {"scope": "all_reporters_latest_state", "reporters": reporters},
            "session_coverage": _session_coverage(conn, where, params)}


def usage_attempts(conn: sqlite3.Connection, since=None, until=None, project_id=None, offset=0, limit=25) -> dict:
    where, params, _ = _filters(since, until, project_id)
    total = conn.execute(f"SELECT COUNT(*) FROM usage_attempts a WHERE {where}", params).fetchone()[0]
    rows = conn.execute(f"SELECT payload_json,workflow_id FROM usage_attempts a WHERE {where} ORDER BY a.started_at DESC,a.attempt_id LIMIT ? OFFSET ?", (*params, limit, offset)).fetchall()
    return {"items": [{**json.loads(row["payload_json"]), "workflow_id": row["workflow_id"]} for row in rows],
            "offset": offset, "limit": limit, "total": total, "next_offset": offset + limit if offset + limit < total else None}
