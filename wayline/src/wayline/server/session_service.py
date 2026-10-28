from __future__ import annotations

import base64
import binascii
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
from uuid import UUID

from fastapi import HTTPException
from wayline.server import db
from wayline.server.session_models import CHUNK_BYTES, EVENT_BYTES, SESSION_BYTES, SessionChunk, SessionInvocation, SessionResult

SCHEMA = '''
CREATE TABLE IF NOT EXISTS agent_conversations (
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL, pi_session_id TEXT, bytes_captured INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS agent_invocations (
 id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES agent_conversations(id), project_id TEXT NOT NULL,
 workflow_id TEXT, stage_id TEXT, started_at TEXT NOT NULL, updated_at TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
 payload_json TEXT NOT NULL, events_bytes INTEGER NOT NULL DEFAULT 0,
 result_facts_json TEXT NOT NULL DEFAULT '[]', result_intents_json TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS agent_invocations_project ON agent_invocations(project_id,started_at);
CREATE INDEX IF NOT EXISTS agent_invocations_workflow ON agent_invocations(workflow_id,started_at);
'''


def ensure_schema(conn):
    conn.executescript(SCHEMA)


def agent_dir() -> Path:
    # Adjacent to the configured DB by default, so separate test/production DBs
    # never share histories. No fallback to the user's global Pi directory.
    root = Path(os.environ['WAYLINE_PI_VIEWER_DIR']).expanduser() if os.environ.get('WAYLINE_PI_VIEWER_DIR') else db._db_path.parent / 'pi-viewer'
    root = root.resolve()
    host_agent = Path(os.environ.get('PI_CODING_AGENT_DIR', str(Path.home()/'.pi/agent'))).expanduser().resolve()
    if root == host_agent or root.is_relative_to(Path.home()/'.pi'):
        raise HTTPException(503, 'Wayline viewer directory must be separate from host Pi configuration')
    if any((root/name).exists() for name in ('auth.json', 'models.json')):
        raise HTTPException(503, 'Wayline viewer directory cannot contain worker credentials or model configuration')
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


def _managed_file(parts, identifier, limit):
    root = agent_dir()
    for part in parts:
        root = root/part
        if root.is_symlink():
            raise HTTPException(503, 'Wayline session storage cannot follow symlinks')
        root.mkdir(exist_ok=True, mode=0o700)
    target = root/f'{UUID(identifier)}.jsonl'
    if target.is_symlink() or (target.exists() and (not target.is_file() or target.stat().st_size > limit)):
        raise HTTPException(503, 'Wayline session storage contains an unsafe file')
    return target


def conversation_file(conversation_id: str) -> Path:
    return _managed_file(('sessions', 'wayline'), conversation_id, SESSION_BYTES)


def event_file(invocation_id: str) -> Path:
    return _managed_file(('activity',), invocation_id, EVENT_BYTES)


def register(conn: sqlite3.Connection, invocation: SessionInvocation) -> dict:
    identifier, conversation = str(invocation.id), str(invocation.conversation_id)
    existing = conn.execute('SELECT payload_json FROM agent_invocations WHERE id=?', (identifier,)).fetchone()
    project = conn.execute('SELECT workflow_id,workflow_stage_id FROM projects WHERE id=?', (invocation.project_id,)).fetchone()
    # Historical invocation identity already validated at creation remains valid
    # after standalone deletion. Its final cancellation must not stay "running".
    if existing is None:
        if project is None:
            raise HTTPException(404, 'Session project does not exist')
        if invocation.intent_id and conn.execute('SELECT 1 FROM intents WHERE project_id=? AND id=?', (invocation.project_id, invocation.intent_id)).fetchone() is None:
            raise HTTPException(422, 'Session intent is outside this project')
        for fact_id in invocation.source_fact_ids:
            if conn.execute('SELECT 1 FROM facts WHERE project_id=? AND id=?', (invocation.project_id, fact_id)).fetchone() is None:
                raise HTTPException(422, 'Session source fact is outside this project')
    existing_conversation = conn.execute('SELECT project_id FROM agent_conversations WHERE id=?', (conversation,)).fetchone()
    if existing_conversation and existing_conversation['project_id'] != invocation.project_id:
        raise HTTPException(409, 'Conversation already belongs to another project')
    if invocation.delivery_status == 'delivered':
        stored = conn.execute('SELECT bytes_captured FROM agent_conversations WHERE id=?', (conversation,)).fetchone()
        event_bytes = conn.execute('SELECT events_bytes FROM agent_invocations WHERE id=?', (identifier,)).fetchone()
        if (stored['bytes_captured'] if stored else 0) < invocation.delivery_session_bytes or (event_bytes['events_bytes'] if event_bytes else 0) < invocation.delivery_event_bytes:
            raise HTTPException(409, 'Session delivery targets have not been acknowledged')
    encoded = invocation.model_dump(mode='json')
    if existing:
        previous = json.loads(existing['payload_json'])
        identity = ('id', 'conversation_id', 'project_id', 'worker_name', 'phase', 'intent_id', 'source_fact_ids', 'started_at', 'dispatcher_instance_id')
        if any(previous[key] != encoded[key] for key in identity):
            raise HTTPException(409, 'Invocation identity cannot change')
        # Replaying the initial record must never revert a completed invocation.
        if previous['status'] != 'running' and encoded['status'] == 'running':
            return session_detail(conn, identifier)
        # An old pending replay for an already delivered terminal snapshot must
        # not briefly undo the acknowledged delivery state.
        if (previous.get('delivery_status') == 'delivered' and encoded['delivery_status'] == 'pending'
                and previous['status'] != 'running'
                and encoded['delivery_session_bytes'] <= previous.get('delivery_session_bytes', 0)
                and encoded['delivery_event_bytes'] <= previous.get('delivery_event_bytes', 0)):
            return session_detail(conn, identifier)
        if previous != encoded:
            conn.execute('UPDATE agent_invocations SET payload_json=?,updated_at=?,revision=revision+1 WHERE id=?',
                         (json.dumps(encoded), _now(), identifier))
    else:
        conn.execute('INSERT OR IGNORE INTO agent_conversations(id,project_id) VALUES(?,?)', (conversation, invocation.project_id))
        conn.execute('INSERT INTO agent_invocations(id,conversation_id,project_id,workflow_id,stage_id,started_at,updated_at,payload_json) VALUES(?,?,?,?,?,?,?,?)',
                     (identifier, conversation, invocation.project_id, project['workflow_id'], project['workflow_stage_id'], invocation.started_at.isoformat(), _now(), json.dumps(encoded)))
    return session_detail(conn, identifier)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _row(conn, identifier):
    row = conn.execute('SELECT i.*,c.bytes_captured,c.pi_session_id FROM agent_invocations i JOIN agent_conversations c ON c.id=i.conversation_id WHERE i.id=?', (identifier,)).fetchone()
    if row is None:
        raise HTTPException(404, 'Session invocation not found')
    return row


def _approved_claim_match(alias='agent_invocations'):
    reference = "'wayline-sessions/'||"+alias+".conversation_id||'.jsonl'"
    return f"c.project_id={alias}.project_id AND c.status='approved' AND (json_extract(c.payload_json,'$.producer_session_id')={reference} OR c.reviewer_session_id={reference} OR json_extract(c.payload_json,'$.reviewer_cli_session_id')={reference})"


def session_detail(conn, identifier):
    row = _row(conn, identifier)
    payload = json.loads(row['payload_json'])
    result_facts = json.loads(row['result_facts_json'])
    result_intents = json.loads(row['result_intents_json'])
    if payload['intent_id']:
        intent = conn.execute('SELECT to_fact_id FROM intents WHERE project_id=? AND id=?', (row['project_id'], payload['intent_id'])).fetchone()
        if intent and intent['to_fact_id']:
            result_facts = sorted(set([*result_facts, intent['to_fact_id']]))
    approved = conn.execute("SELECT json_extract(c.payload_json,'$.accepted_completion_intent_id') AS accepted FROM completion_claims c JOIN agent_invocations i ON i.id=? WHERE "+_approved_claim_match('i'), (identifier,)).fetchall()
    accepted_ids = [claim['accepted'] for claim in approved if claim['accepted']]
    if accepted_ids:
        result_facts = sorted(set([*result_facts, 'goal']))
        result_intents = sorted(set([*result_intents, *accepted_ids]))
    last_reported = payload.get('heartbeat_at') or row['started_at']
    age = (datetime.now(timezone.utc)-datetime.fromisoformat(last_reported.replace('Z', '+00:00'))).total_seconds()
    runtime_status = 'unconfirmed' if payload['status'] == 'running' and not -30 <= age <= 45 else payload['status']
    events = _read_event_records(event_file(identifier), 0, 1, count_only=True)
    return {**payload, 'runtime_status': runtime_status, 'runtime_last_reported_at': last_reported, 'workflow_id': row['workflow_id'], 'stage_id': row['stage_id'], 'revision': row['revision'],
            'bytes_captured': row['bytes_captured'], 'events_captured': events['total'], 'events_bytes': row['events_bytes'],
            'pi_session_id': row['pi_session_id'], 'result_fact_ids': result_facts, 'result_intent_ids': result_intents,
            'viewer_url': f'/session?id={row["conversation_id"]}.jsonl' if row['pi_session_id'] else None,
            'updated_at': row['updated_at']}


def list_sessions(conn, *, project_id=None, workflow_id=None, intent_id=None, fact_id=None, offset=0, limit=25):
    clauses, values = [], []
    for column, value in (('project_id', project_id), ('workflow_id', workflow_id)):
        if value:
            clauses.append(f'{column}=?'); values.append(value)
    where = ' AND '.join(clauses) or '1=1'
    # Keep graph alternatives inside their own predicate so an approved claim
    # can never bypass project/workflow filters.
    if intent_id:
        predicate = "json_extract(payload_json,'$.intent_id')=? OR EXISTS (SELECT 1 FROM json_each(result_intents_json) WHERE value=?)"
        predicate += " OR EXISTS (SELECT 1 FROM completion_claims c WHERE "+_approved_claim_match()+" AND json_extract(c.payload_json,'$.accepted_completion_intent_id')=?)"
        where += ' AND ('+predicate+')'
        values += [intent_id, intent_id, intent_id]
    if fact_id:
        predicate = "EXISTS (SELECT 1 FROM json_each(payload_json,'$.source_fact_ids') WHERE value=?) OR EXISTS (SELECT 1 FROM json_each(result_facts_json) WHERE value=?) OR EXISTS (SELECT 1 FROM intents t WHERE t.project_id=agent_invocations.project_id AND t.id=json_extract(payload_json,'$.intent_id') AND t.to_fact_id=?)"
        if fact_id == 'goal':
            predicate += " OR EXISTS (SELECT 1 FROM completion_claims c WHERE "+_approved_claim_match()+" AND json_extract(c.payload_json,'$.accepted_completion_intent_id') IS NOT NULL)"
        where += ' AND ('+predicate+')'
        values += [fact_id, fact_id, fact_id]
    total = conn.execute('SELECT COUNT(*) FROM agent_invocations WHERE '+where, values).fetchone()[0]
    rows = conn.execute('SELECT id FROM agent_invocations WHERE '+where+' ORDER BY started_at DESC,id DESC LIMIT ? OFFSET ?', (*values, limit, offset)).fetchall()
    return {'items': [session_detail(conn, row['id']) for row in rows], 'total': total, 'next_offset': offset+limit if offset+limit < total else None}


def append_chunk(conn, identifier: str, chunk: SessionChunk, *, events=False):
    row = _row(conn, identifier)
    try:
        content = base64.b64decode(chunk.data, validate=True)
    except (ValueError, binascii.Error):
        raise HTTPException(422, 'Invalid chunk encoding')
    if len(content) > CHUNK_BYTES:
        raise HTTPException(413, 'Session chunk exceeds the transport limit')
    limit = EVENT_BYTES if events else SESSION_BYTES
    if chunk.offset + len(content) > limit:
        raise HTTPException(413, 'Session capture exceeds its storage limit')
    path = event_file(identifier) if events else conversation_file(row['conversation_id'])
    current = path.stat().st_size if path.exists() else 0
    if chunk.offset > current:
        raise HTTPException(409, {'message': 'Missing earlier session bytes', 'offset': current})
    duplicate = chunk.offset < current
    if duplicate:
        overlap = min(current-chunk.offset, len(content))
        with path.open('rb') as source:
            source.seek(chunk.offset)
            if source.read(overlap) != content[:overlap]:
                raise HTTPException(409, 'Conflicting replayed session bytes')
        # Recover a process interruption after only part of a chunk reached disk.
        content = content[overlap:]
    pi_session_id = row['pi_session_id']
    if not events and (current == 0 or pi_session_id is None) and (content or current):
        prefix = b''
        if current:
            with path.open('rb') as source:
                prefix = source.read(CHUNK_BYTES)
        try:
            header = json.loads((prefix+content).split(b'\n', 1)[0])
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(422, 'First session chunk must contain the actual Pi header')
        if not isinstance(header, dict) or header.get('type') != 'session' or not isinstance(header.get('id'), str) or not header['id']:
            raise HTTPException(422, 'Session source does not contain a Pi header')
        duplicate_id = conn.execute('SELECT id FROM agent_conversations WHERE pi_session_id=? AND id<>?', (header['id'], row['conversation_id'])).fetchone()
        if duplicate_id:
            raise HTTPException(409, 'Pi session header already belongs to another Wayline conversation')
        pi_session_id = header['id']
    if content:
        with path.open('ab') as target:
            os.chmod(path, 0o600)
            target.write(content); target.flush(); os.fsync(target.fileno())
        current += len(content)
    # Files are fsynced before SQLite commits. Reconcile a replay after a crash
    # between those operations, including the original Pi header identity.
    if events:
        if current != row['events_bytes']:
            conn.execute('UPDATE agent_invocations SET events_bytes=?,updated_at=?,revision=revision+1 WHERE id=?', (current, _now(), identifier))
    elif current != row['bytes_captured'] or pi_session_id != row['pi_session_id']:
        conn.execute('UPDATE agent_conversations SET bytes_captured=?,pi_session_id=? WHERE id=?', (current, pi_session_id, row['conversation_id']))
        conn.execute('UPDATE agent_invocations SET updated_at=?,revision=revision+1 WHERE conversation_id=?', (_now(), row['conversation_id']))
    return {'offset': current, 'duplicate': duplicate and not content}


def _read_event_records(path: Path, after: int, limit: int, *, count_only=False):
    items, total = [], 0
    if path.exists():
        with path.open('rb') as source:
            for raw in source:
                if not raw.endswith(b'\n'):
                    break
                if len(raw) > CHUNK_BYTES:
                    continue
                try:
                    event = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    continue
                if not isinstance(event, dict) or not isinstance(event.get('type'), str):
                    continue
                total += 1
                if not count_only and total > after and len(items) < limit:
                    items.append({'sequence': total, 'event': event})
    next_after = items[-1]['sequence'] if items else after
    return {'items': items, 'next_after': next_after, 'has_more': total > next_after, 'total': total}


def read_events(conn, identifier, after=0, limit=50):
    row = _row(conn, identifier)
    result = _read_event_records(event_file(identifier), after, limit)
    result['truncated'] = json.loads(row['payload_json'])['events_truncated']
    return result


def link_result(conn, conversation_id, result: SessionResult):
    row = conn.execute('SELECT project_id FROM agent_conversations WHERE id=?', (conversation_id,)).fetchone()
    if row is None or row['project_id'] != result.project_id:
        raise HTTPException(404, 'Conversation not found for this project')
    for table, identifiers in (('facts', result.result_fact_ids), ('intents', result.result_intent_ids)):
        for identifier in identifiers:
            if conn.execute(f'SELECT 1 FROM {table} WHERE project_id=? AND id=?', (result.project_id, identifier)).fetchone() is None:
                raise HTTPException(422, 'Session result is outside this project')
    rows = conn.execute('SELECT id,result_facts_json,result_intents_json FROM agent_invocations WHERE conversation_id=?', (conversation_id,)).fetchall()
    for invocation in rows:
        facts = sorted(set([*json.loads(invocation['result_facts_json']), *result.result_fact_ids]))
        intents = sorted(set([*json.loads(invocation['result_intents_json']), *result.result_intent_ids]))
        if facts != json.loads(invocation['result_facts_json']) or intents != json.loads(invocation['result_intents_json']):
            conn.execute('UPDATE agent_invocations SET result_facts_json=?,result_intents_json=?,revision=revision+1,updated_at=? WHERE id=?',
                         (json.dumps(facts), json.dumps(intents), _now(), invocation['id']))
    return {'ok': True}
