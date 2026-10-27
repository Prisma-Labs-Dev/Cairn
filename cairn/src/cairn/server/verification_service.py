from __future__ import annotations

import json
import sqlite3
import uuid
from fastapi import HTTPException
from cairn.server.verification_models import CompletionClaim, ReviewIdentity, ReviewLeaseRequest, ReviewDecisionRequest
from cairn.server.services import get_project_or_404, utcnow, clear_project_reason, next_intent_id, next_hint_id

MAX_REJECTIONS = 3
MAX_REVIEW_ATTEMPTS = 3
REVIEW_LEASE_SECONDS = 120


def ensure_schema(conn):
    columns = {row['name'] for row in conn.execute('PRAGMA table_info(projects)')}
    for name, definition in [('verification_enabled', 'INTEGER NOT NULL DEFAULT 0'), ('pending_review_id', 'TEXT'), ('verification_rejections', 'INTEGER NOT NULL DEFAULT 0')]:
        if name not in columns:
            conn.execute(f'ALTER TABLE projects ADD COLUMN {name} {definition}')
    conn.executescript('''
    CREATE TABLE IF NOT EXISTS completion_claims (
        id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        status TEXT NOT NULL, created_at TEXT NOT NULL, payload_json TEXT NOT NULL,
        reviewer_agent_id TEXT, reviewer_session_id TEXT, reviewer_worker TEXT,
        lease_token TEXT, last_heartbeat_at TEXT, decided_at TEXT, feedback TEXT,
        evidence_json TEXT NOT NULL DEFAULT '[]', review_attempts INTEGER NOT NULL DEFAULT 0
    );
    CREATE UNIQUE INDEX IF NOT EXISTS one_pending_completion_claim ON completion_claims(project_id) WHERE status='pending';
    CREATE TABLE IF NOT EXISTS completion_claim_artifacts (
        claim_id TEXT NOT NULL REFERENCES completion_claims(id) ON DELETE CASCADE,
        artifact_id TEXT NOT NULL, path TEXT NOT NULL, content BLOB NOT NULL,
        PRIMARY KEY(claim_id,artifact_id)
    );
    ''')


def get_claim(conn, claim_id, *, reveal_lease=False):
    row = conn.execute('SELECT * FROM completion_claims WHERE id=?', (claim_id,)).fetchone()
    if row is None:
        raise HTTPException(404, 'Review not found')
    data = json.loads(row['payload_json'])
    data.update({key: row[key] for key in ('status', 'reviewer_agent_id', 'reviewer_session_id', 'reviewer_worker', 'last_heartbeat_at', 'decided_at', 'feedback', 'review_attempts')})
    data['evidence'] = json.loads(row['evidence_json'])
    data['lease_token'] = row['lease_token'] if reveal_lease else None
    return CompletionClaim.model_validate(data)


def claim_summaries(conn, project_id=None, offset=0, limit=20):
    from cairn.server.verification_models import ReviewPage, CompletionClaimSummary
    where = ' WHERE project_id=?' if project_id else ''
    params = (project_id,) if project_id else ()
    total = conn.execute('SELECT COUNT(*) FROM completion_claims' + where, params).fetchone()[0]
    rows = conn.execute("""
        SELECT id,project_id,status,created_at,reviewer_agent_id,reviewer_session_id,reviewer_worker,review_attempts,
               substr(feedback,1,240) AS feedback,
               json_extract(payload_json,'$.workflow_id') AS workflow_id,
               json_extract(payload_json,'$.project_title') AS project_title,
               json_extract(payload_json,'$.producer_agent_id') AS producer_agent_id,
               json_extract(payload_json,'$.producer_session_id') AS producer_session_id,
               json_extract(payload_json,'$.producer_worker') AS producer_worker,
               substr(json_extract(payload_json,'$.completion.description'),1,240) AS completion_description
        FROM completion_claims
    """ + where + ' ORDER BY created_at DESC,rowid DESC LIMIT ? OFFSET ?', (*params, limit, offset))
    return ReviewPage(items=[CompletionClaimSummary(**dict(row)) for row in rows], total=total, offset=offset, limit=limit,
                      next_offset=offset+limit if offset+limit < total else None)


def submit_claim(conn, project, body):
    from cairn.server.artifact_service import list_artifacts, require_exploration_finished
    from cairn.server.workflow_service import append_generated_goals
    if not body.producer_agent_id or not body.producer_session_id:
        raise HTTPException(422, 'Verification requires producer_agent_id and producer_session_id')
    pending = project['pending_review_id']
    completion = body.model_dump(mode='json', by_alias=True)
    if pending:
        claim = get_claim(conn, pending)
        if claim.completion == completion:
            return claim
        raise HTTPException(409, 'A completion claim is already awaiting independent review')
    for previous in conn.execute("SELECT id,json_extract(payload_json,'$.completion') AS completion_json FROM completion_claims WHERE project_id=? ORDER BY rowid DESC", (project['id'],)):
        if json.loads(previous['completion_json']) == completion:
            return get_claim(conn, previous['id'])
    if project['status'] != 'active':
        raise HTTPException(403, f"Project is {project['status']}")
    if project['reason_worker'] is not None and project['reason_worker'] != body.worker:
        raise HTTPException(409, 'Completion producer does not own the active reason lease')
    if len(set(body.from_)) != len(body.from_):
        raise HTTPException(422, 'Completion source fact IDs must be unique')
    require_exploration_finished(conn, project['id'])
    # Reuse full proposal validation without exposing any generated children.
    conn.execute('SAVEPOINT validate_proposals')
    try:
        append_generated_goals(conn, project, body.goals, body.selected_goal, body.selection_reason)
    finally:
        conn.execute('ROLLBACK TO validate_proposals')
        conn.execute('RELEASE validate_proposals')
    claim_id = f'review_{uuid.uuid4().hex}'
    now = utcnow()
    snapshot = {
        'facts': [dict(row) for row in conn.execute('SELECT id,description FROM facts WHERE project_id=? ORDER BY rowid', (project['id'],))],
        'hints': [dict(row) for row in conn.execute('SELECT content,creator FROM hints WHERE project_id=? ORDER BY rowid', (project['id'],))],
        'artifacts': [item.model_dump(mode='json') for item in list_artifacts(conn, project['id'])],
    }
    for artifact in snapshot['artifacts']:
        artifact['download_url'] = f"/reviews/{claim_id}/artifacts/{artifact['id']}/download"
    claim = CompletionClaim(id=claim_id, project_id=project['id'], workflow_id=project['workflow_id'], project_title=project['title'], status='pending', created_at=now,
                            producer_agent_id=body.producer_agent_id, producer_session_id=body.producer_session_id, producer_worker=body.worker,
                            completion=completion, snapshot=snapshot)
    conn.execute('INSERT INTO completion_claims(id,project_id,status,created_at,payload_json) VALUES(?,?,?,?,?)',
                 (claim_id, project['id'], 'pending', now, claim.model_dump_json()))
    conn.execute('INSERT INTO completion_claim_artifacts(claim_id,artifact_id,path,content) SELECT ?,id,path,content FROM artifacts WHERE project_id=?', (claim_id, project['id']))
    conn.execute('UPDATE projects SET pending_review_id=? WHERE id=?', (claim_id, project['id']))
    clear_project_reason(conn, project['id'])
    return claim


def invalidate_review_lease(conn, project_id):
    conn.execute("UPDATE completion_claims SET lease_token=NULL,last_heartbeat_at=NULL WHERE project_id=? AND status='pending'", (project_id,))


def _active_claim(conn, claim_id):
    claim = get_claim(conn, claim_id, reveal_lease=True)
    project = get_project_or_404(conn, claim.project_id)
    from cairn.server.workflow_management import require_project_writable
    require_project_writable(conn, project)
    if project['status'] != 'active':
        raise HTTPException(403, f"Project is {project['status']}")
    if claim.status != 'pending' or project['pending_review_id'] != claim_id:
        raise HTTPException(409, 'Review is no longer pending')
    return claim, project


def _independent(claim, body):
    if (body.agent_id == claim.producer_agent_id or body.session_id == claim.producer_session_id
            or getattr(body, 'cli_session_id', None) == claim.producer_session_id):
        raise HTTPException(409, 'The producer agent or session cannot review its own completion claim')


def _lease_valid(conn, claim):
    if not claim.lease_token or not claim.last_heartbeat_at:
        return False
    return conn.execute('SELECT (julianday(?) - julianday(?))*86400 <= ?', (utcnow(), claim.last_heartbeat_at, REVIEW_LEASE_SECONDS)).fetchone()[0] == 1


def claim_review(conn, claim_id, body: ReviewIdentity):
    claim, project = _active_claim(conn, claim_id)
    _independent(claim, body)
    if _lease_valid(conn, claim):
        if (claim.reviewer_agent_id, claim.reviewer_session_id, claim.reviewer_worker) == (body.agent_id, body.session_id, body.worker):
            return claim
        raise HTTPException(409, 'Review is claimed by another agent')
    if claim.review_attempts >= MAX_REVIEW_ATTEMPTS:
        _pause(conn, project, 'Independent review paused after 3 failed or interrupted attempts. Inspect the claim and resume explicitly to retry.')
        return get_claim(conn, claim_id)
    token = uuid.uuid4().hex
    conn.execute('UPDATE completion_claims SET reviewer_agent_id=?,reviewer_session_id=?,reviewer_worker=?,lease_token=?,last_heartbeat_at=?,review_attempts=review_attempts+1 WHERE id=?',
                 (body.agent_id, body.session_id, body.worker, token, utcnow(), claim_id))
    return get_claim(conn, claim_id, reveal_lease=True)


def _require_lease(conn, claim, body):
    _independent(claim, body)
    if not _lease_valid(conn, claim) or (claim.reviewer_agent_id, claim.reviewer_session_id, claim.reviewer_worker, claim.lease_token) != (body.agent_id, body.session_id, body.worker, body.lease_token):
        raise HTTPException(409, 'Review lease expired or belongs to another agent/session')


def heartbeat_review(conn, claim_id, body: ReviewLeaseRequest):
    claim, _ = _active_claim(conn, claim_id)
    _require_lease(conn, claim, body)
    conn.execute('UPDATE completion_claims SET last_heartbeat_at=? WHERE id=?', (utcnow(), claim_id))
    return get_claim(conn, claim_id, reveal_lease=True)


def release_review(conn, claim_id, body: ReviewLeaseRequest):
    claim, _ = _active_claim(conn, claim_id)
    _require_lease(conn, claim, body)
    conn.execute('UPDATE completion_claims SET lease_token=NULL,last_heartbeat_at=NULL WHERE id=?', (claim_id,))
    return get_claim(conn, claim_id)


def _pause(conn, project, feedback):
    if project['workflow_id']:
        from cairn.server.workflow_service import set_workflow_status
        set_workflow_status(conn, project['workflow_id'], 'stopped')
    else:
        conn.execute("UPDATE projects SET status='stopped' WHERE id=?", (project['id'],))
        clear_project_reason(conn, project['id'])
    invalidate_review_lease(conn, project['id'])
    conn.execute("UPDATE completion_claims SET feedback=? WHERE project_id=? AND status='pending'", (feedback, project['id']))
    conn.execute('INSERT INTO hints(id,project_id,content,creator,created_at) VALUES(?,?,?,?,?)',
                 (next_hint_id(conn, project['id']), project['id'], feedback, 'verification', utcnow()))


def decide_review(conn, claim_id, body: ReviewDecisionRequest):
    claim = get_claim(conn, claim_id, reveal_lease=True)
    from cairn.server.workflow_management import require_project_writable
    require_project_writable(conn, get_project_or_404(conn, claim.project_id))
    if claim.status != 'pending':
        if (claim.status, claim.reviewer_agent_id, claim.reviewer_session_id, claim.reviewer_worker, claim.lease_token, claim.feedback, claim.evidence, claim.reviewer_cli_session_id) == (body.decision, body.agent_id, body.session_id, body.worker, body.lease_token, body.feedback, body.evidence, body.cli_session_id):
            return get_claim(conn, claim_id)
        raise HTTPException(409, 'Review already decided')
    claim, project = _active_claim(conn, claim_id)
    _require_lease(conn, claim, body)
    now = utcnow()
    conn.execute("UPDATE completion_claims SET payload_json=json_set(payload_json,'$.reviewer_cli_session_id',?) WHERE id=?", (body.cli_session_id, claim_id))
    conn.execute('UPDATE completion_claims SET status=?,decided_at=?,feedback=?,evidence_json=? WHERE id=?',
                 (body.decision, now, body.feedback, json.dumps(body.evidence), claim_id))
    conn.execute('UPDATE projects SET pending_review_id=NULL WHERE id=?', (project['id'],))
    if body.decision == 'approved':
        from cairn.server.models import CompleteRequest
        from cairn.server.routers.projects import apply_completion
        apply_completion(conn, project, CompleteRequest.model_validate(claim.completion))
    else:
        iid = next_intent_id(conn, project['id'])
        conn.execute('INSERT INTO intents(id,project_id,description,creator,created_at) VALUES(?,?,?,?,?)',
                     (iid, project['id'], 'Independent review rejected the completion claim. Correct the work and provide evidence before claiming completion again. Feedback: ' + body.feedback, 'verification', now))
        for source in claim.completion['from']:
            conn.execute('INSERT INTO intent_sources(intent_id,project_id,fact_id) VALUES(?,?,?)', (iid, project['id'], source))
        conn.execute("UPDATE projects SET verification_rejections=verification_rejections+1 WHERE id=?", (project["id"],))
        count = conn.execute("SELECT verification_rejections FROM projects WHERE id=?", (project["id"],)).fetchone()[0]
        if count >= MAX_REJECTIONS:
            _pause(conn, project, 'Independent review rejected 3 completion claims. Automatic correction is paused. Inspect the feedback and resume explicitly when ready.')
    return get_claim(conn, claim_id)
