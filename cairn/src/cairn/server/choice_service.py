"""Ranked alternatives. Only a selected option can execute or satisfy a choice."""
from __future__ import annotations

import json
import sqlite3

from fastapi import HTTPException

from cairn.server.models import RetryChoiceRequest, UnavailableRequest
from cairn.server.services import clear_project_reason, next_hint_id, utcnow


def is_choice(row: sqlite3.Row) -> bool:
    return (json.loads(row['definition_json']).get('expansion') or {}).get('mode', 'all') == 'one'


def choice_satisfied(conn, workflow_id, parent_id) -> bool:
    return conn.execute("SELECT 1 FROM workflow_stages WHERE workflow_id=? AND generated_by_stage_id=? AND option_state='succeeded' AND completion_json IS NOT NULL",
                        (workflow_id, parent_id)).fetchone() is not None


def record_option_event(conn, row, state, description, *, from_ids=None, worker='workflow', project_id=None):
    history = json.loads(row['option_history_json'])
    history.append({'state': state, 'description': description, 'from': from_ids or [], 'worker': worker,
                    'project_id': project_id or row['project_id'], 'created_at': utcnow()})
    conn.execute('UPDATE workflow_stages SET option_state=?,option_reason=?,option_history_json=? WHERE workflow_id=? AND id=?',
                 (state, description, json.dumps(history), row['workflow_id'], row['id']))


def finish_selected_option(conn, project, description):
    row = conn.execute('SELECT * FROM workflow_stages WHERE project_id=?', (project['id'],)).fetchone()
    if row is None or row['option_state'] is None:
        return
    if row['option_state'] != 'selected':
        raise HTTPException(409, 'Only the selected choice option may complete')
    record_option_event(conn, row, 'succeeded', description)
    for other in conn.execute("SELECT * FROM workflow_stages WHERE workflow_id=? AND generated_by_stage_id=? AND option_state='pending'",
                              (row['workflow_id'], row['generated_by_stage_id'])).fetchall():
        record_option_event(conn, other, 'skipped', f"Option {row['id']} satisfied this choice; this alternative was not run.")


def mark_unavailable(conn, project, body: UnavailableRequest):
    from cairn.server.workflow_management import require_project_writable
    from cairn.server.workflow_service import advance_workflow, set_workflow_status, workflow_detail
    require_project_writable(conn, project)
    row = conn.execute('SELECT * FROM workflow_stages WHERE project_id=?', (project['id'],)).fetchone()
    if row is None or row['option_state'] is None:
        raise HTTPException(409, 'Only a selected choice option may report unavailable')
    if row['option_state'] == 'unavailable':
        last = json.loads(row['option_history_json'])[-1]
        if (last['description'], last['from'], last['worker']) == (body.description, body.from_, body.worker):
            return workflow_detail(conn, project['workflow_id'])
        raise HTTPException(409, 'This attempt was already recorded as unavailable')
    if row['option_state'] != 'selected':
        raise HTTPException(409, 'This choice option is no longer selected')
    workflow = conn.execute('SELECT * FROM workflows WHERE id=?', (row['workflow_id'],)).fetchone()
    if project['status'] != 'active' or workflow['status'] != 'active':
        raise HTTPException(403, 'The selected option is paused or completed')
    if project['pending_review_id']:
        raise HTTPException(409, 'Resolve the pending completion review before switching choice options')
    if project['reason_worker'] is not None and project['reason_worker'] != body.worker:
        raise HTTPException(409, 'Unavailable reporter does not own the reason lease')
    record_option_event(conn, row, 'unavailable', body.description, from_ids=body.from_, worker=body.worker)
    conn.execute("UPDATE projects SET status='stopped' WHERE id=?", (project['id'],))
    conn.execute('UPDATE intents SET worker=NULL,last_heartbeat_at=NULL WHERE project_id=? AND concluded_at IS NULL', (project['id'],))
    clear_project_reason(conn, project['id'])
    conn.execute('INSERT INTO hints(id,project_id,content,creator,created_at) VALUES(?,?,?,?,?)',
                 (next_hint_id(conn, project['id']), project['id'], 'Goal unavailable, not achieved: ' + body.description, 'choice', utcnow()))
    next_option = conn.execute("SELECT * FROM workflow_stages WHERE workflow_id=? AND generated_by_stage_id=? AND option_state='pending' ORDER BY position LIMIT 1",
                               (row['workflow_id'], row['generated_by_stage_id'])).fetchone()
    if next_option:
        record_option_event(conn, next_option, 'selected', f"Selected next ranked untried alternative after {row['id']} was unavailable: {body.description}")
        advance_workflow(conn, row['workflow_id'])
    else:
        error = f"All alternatives for {row['generated_by_stage_id']} are unavailable. No goal was achieved by fallback. Retry an option with new circumstances, or create a revised workflow. Last unmet goal: {json.loads(row['definition_json'])['goal']}. Reason: {body.description}"
        conn.execute('UPDATE workflow_stages SET choice_error=? WHERE workflow_id=? AND id=?',
                     (error, row['workflow_id'], row['generated_by_stage_id']))
        set_workflow_status(conn, row['workflow_id'], 'stopped')
    return workflow_detail(conn, row['workflow_id'])


def retry_choice(conn, workflow_id: str, parent_id: str, body: RetryChoiceRequest):
    from cairn.server.workflow_management import require_workflow_writable
    from cairn.server.workflow_service import workflow_detail
    workflow = require_workflow_writable(conn, workflow_id)
    if workflow['status'] != 'stopped':
        raise HTTPException(409, 'Pause the workflow before retrying an exhausted choice')
    parent = conn.execute('SELECT * FROM workflow_stages WHERE workflow_id=? AND id=?', (workflow_id, parent_id)).fetchone()
    if parent is None or not is_choice(parent) or not parent['choice_error']:
        raise HTTPException(409, 'Only an exhausted choice can be retried')
    row = conn.execute('SELECT * FROM workflow_stages WHERE workflow_id=? AND id=? AND generated_by_stage_id=?',
                       (workflow_id, body.goal_id, parent_id)).fetchone()
    if row is None or row['option_state'] != 'unavailable':
        raise HTTPException(409, 'Choose an existing unavailable option to retry')
    conn.execute('INSERT INTO workflow_stage_attempts(project_id,stage_json) VALUES(?,?)', (row['project_id'], json.dumps(dict(row))))
    record_option_event(conn, row, 'selected', 'Operator authorized a fresh attempt. New circumstances: ' + body.reason, worker='operator')
    # Retain the old project and its evidence. A new project on Resume fences out
    # stale workers from the previous attempt, including delayed HTTP retries.
    conn.execute('UPDATE workflow_stages SET project_id=NULL,choice_error=NULL,artifact_recovery_attempts=0,artifact_error=NULL WHERE workflow_id=? AND id=?',
                 (workflow_id, row['id']))
    conn.execute('UPDATE workflow_stages SET choice_error=NULL WHERE workflow_id=? AND id=?', (workflow_id, parent_id))
    return workflow_detail(conn, workflow_id)


def stage_attempt(conn, project_id):
    row = conn.execute('SELECT * FROM workflow_stages WHERE project_id=?', (project_id,)).fetchone()
    if row is not None:
        return row
    archived = conn.execute('SELECT stage_json FROM workflow_stage_attempts WHERE project_id=?', (project_id,)).fetchone()
    return json.loads(archived['stage_json']) if archived else None


def choice_history(conn, project_id):
    from cairn.server.services import get_project_or_404
    project = get_project_or_404(conn, project_id)
    stage = stage_attempt(conn, project_id)
    if stage is None or stage['option_state'] is None:
        raise HTTPException(404, 'Project has no choice history')
    result = []
    for row in conn.execute('SELECT id,option_history_json FROM workflow_stages WHERE workflow_id=? AND generated_by_stage_id=? ORDER BY position',
                            (project['workflow_id'], stage['generated_by_stage_id'])):
        for event in json.loads(row['option_history_json']):
            if event['state'] == 'unavailable':
                result.append({**event, 'stage_id': row['id'], 'facts': [
                    {'id': fid, 'url': f"/projects/{project_id}/choice-history/{event['project_id']}/facts/{fid}"} for fid in event['from']]})
    return result


def choice_history_fact(conn, project_id, source_id, fact_id):
    allowed = any(event['project_id'] == source_id and fact_id in event['from'] for event in choice_history(conn, project_id))
    if not allowed:
        raise HTTPException(404, 'Fact is outside this choice history')
    fact = conn.execute('SELECT id,description FROM facts WHERE project_id=? AND id=?', (source_id, fact_id)).fetchone()
    if fact is None:
        raise HTTPException(404, 'Choice evidence not found')
    return dict(fact)
