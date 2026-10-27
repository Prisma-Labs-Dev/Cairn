from __future__ import annotations

import sqlite3
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from cairn.server import db
from cairn.server.app import app
from cairn.server.usage_service import usage_summary


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, '_db_path', None)
    monkeypatch.setenv('CAIRN_PI_VIEWER_DIR', str(tmp_path / 'viewer'))
    db.configure(tmp_path / 'usage.db')
    with TestClient(app) as http:
        yield http


def project(client):
    response = client.post('/projects', json={'origin': 'Fixture', 'goal': 'Fixture goal'})
    assert response.status_code == 201
    return response.json()['project']['id']


def session(client, project_id, *, second=0):
    identifier = str(uuid4())
    record = {'id': identifier, 'conversation_id': str(uuid4()), 'project_id': project_id,
              'worker_name': 'a-worker-name-is-not-a-model', 'phase': 'explore_execute',
              'started_at': f'2026-10-27T10:00:{second:02d}Z', 'status': 'failed',
              'finished_at': '2026-10-27T10:01:00Z', 'capture_status': 'waiting'}
    assert client.post('/agent-sessions', json=record).status_code == 200
    return identifier


def usage(client, identifier, project_id, *, worker_type='pi', second=0):
    record = {'attempt_id': identifier, 'project_id': project_id, 'worker_name': 'fixture',
              'worker_type': worker_type, 'phase': 'explore_execute', 'status': 'success',
              'started_at': f'2026-10-27T10:00:{second:02d}Z', 'finished_at': '2026-10-27T10:01:00Z',
              'readings': [{'model': 'reported-fixture', 'model_source': 'reported',
                            'input_tokens': 10, 'cached_input_tokens': 3,
                            'uncached_input_tokens': 7, 'output_tokens': 2}]}
    response = client.post('/usage/attempts', json=record)
    assert response.status_code == 200
    return response.json()


def test_three_known_pi_sessions_two_ledger_rows_and_late_idempotent_replay(client):
    owner = project(client)
    identifiers = [session(client, owner, second=n) for n in range(3)]
    for n in range(2):
        usage(client, identifiers[n], owner, second=n)
    summary = client.get('/usage/summary').json()
    coverage = summary['session_coverage']
    assert coverage['known_pi_invocations'] == 3
    assert coverage['missing_usage_attempts'] == 1
    assert coverage['missing_usage_items'] == [{'invocation_id': identifiers[2], 'project_id': owner,
                                               'workflow_id': None, 'started_at': '2026-10-27T10:00:02+00:00'}]
    assert summary['totals']['attempts'] == 2
    assert summary['totals']['input_tokens'] == 20
    assert summary['totals']['output_tokens'] == 4
    assert 'model' not in coverage['missing_usage_items'][0]
    assert 'input_tokens' not in coverage['missing_usage_items'][0]
    assert usage(client, identifiers[2], owner, second=2)['duplicate'] is False
    assert usage(client, identifiers[2], owner, second=2)['duplicate'] is True
    complete = client.get('/usage/summary').json()
    assert complete['session_coverage']['missing_usage_attempts'] == 0
    assert complete['session_coverage']['missing_usage_items'] == []
    assert complete['totals']['attempts'] == 3
    assert complete['totals']['input_tokens'] == 30


def test_known_session_gap_obeys_same_utc_window_and_project_filters(client):
    first, second = project(client), project(client)
    before = session(client, first, second=0)
    middle = session(client, first, second=1)
    after = session(client, first, second=2)
    other = session(client, second, second=1)
    summary = client.get('/usage/summary', params={'project_id': first, 'since': '2026-10-27T10:00:01Z',
                                                  'until': '2026-10-27T10:00:02Z'}).json()
    assert summary['session_coverage']['known_pi_invocations'] == 1
    assert summary['session_coverage']['missing_usage_items'][0]['invocation_id'] == middle
    assert summary['totals']['input_tokens'] is None
    assert client.get('/usage/summary', params={'project_id': second}).json()['session_coverage']['missing_usage_items'][0]['invocation_id'] == other
    ids = {item['invocation_id'] for item in client.get('/usage/summary').json()['session_coverage']['missing_usage_items']}
    assert ids == {before, middle, after, other}
    # A colliding UUID attributed to another project cannot cover this session.
    usage(client, middle, second, second=1)
    assert client.get('/usage/summary', params={'project_id': first}).json()['session_coverage']['missing_usage_attempts'] == 3


def test_missing_session_list_bounded_and_summary_never_reads_payloads(client):
    owner = project(client)
    ids = [session(client, owner, second=n) for n in range(30)]
    with db.get_conn() as conn:
        conn.set_authorizer(lambda action, table, column, *_: sqlite3.SQLITE_DENY
                            if action == sqlite3.SQLITE_READ and column == 'payload_json' else sqlite3.SQLITE_OK)
        coverage = usage_summary(conn)['session_coverage']
    assert coverage['known_pi_invocations'] == 30
    assert coverage['missing_usage_attempts'] == 30
    assert coverage['limit'] == len(coverage['missing_usage_items']) == 25
    assert coverage['has_more'] is True
    assert coverage['missing_usage_items'][0]['invocation_id'] == ids[-1]


def test_existing_non_pi_usage_keeps_totals_and_has_no_invented_session_gap(client):
    usage(client, str(uuid4()), 'retained-project', worker_type='codex')
    usage(client, str(uuid4()), 'retained-project', worker_type='claudecode')
    summary = client.get('/usage/summary').json()
    assert summary['totals']['attempts'] == 2
    assert summary['totals']['input_tokens'] == 20
    assert summary['session_coverage'] == {'known_pi_invocations': 0, 'missing_usage_attempts': 0,
                                           'missing_usage_items': [], 'limit': 25, 'has_more': False}
