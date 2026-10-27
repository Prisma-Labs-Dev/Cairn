from __future__ import annotations

import asyncio
import base64
import json
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
import pytest

from cairn.server import db, session_service
from cairn.server.app import app
from cairn.server.routers.sessions import stream
from cairn.server.session_models import CHUNK_BYTES, SESSION_BYTES


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, '_db_path', None)
    monkeypatch.setenv('CAIRN_PI_VIEWER_DIR', str(tmp_path / 'scoped-viewer'))
    monkeypatch.setenv('CAIRN_SESSION_OUTBOX', str(tmp_path / 'outbox'))
    monkeypatch.delenv('CAIRN_PI_WEB_BINARY', raising=False)
    db.configure(tmp_path / 'sessions.db')
    with TestClient(app) as http:
        yield http


def create_project(client):
    response = client.post('/projects', json={'origin': 'Fixture origin', 'goal': 'Fixture goal'})
    assert response.status_code == 201, response.text
    return response.json()['project']['id']


def invocation(project_id, **changes):
    return {'id': str(uuid4()), 'conversation_id': str(uuid4()), 'project_id': project_id,
            'worker_name': 'fixture-pi', 'phase': 'explore_execute', 'intent_id': None,
            'source_fact_ids': ['origin'], 'started_at': '2026-10-27T10:00:00Z',
            'finished_at': None, 'status': 'running', 'capture_status': 'waiting',
            'source_error': None, 'events_truncated': False, 'cancel_reason': None, **changes}


def session_bytes():
    header = {'type': 'session', 'version': 3, 'id': 'actual-pi-header-id',
              'timestamp': '2026-10-27T10:00:00.123Z', 'cwd': '/fixture/worker'}
    message = {'type': 'message', 'id': 'original-entry-id', 'parentId': None,
               'timestamp': '2026-10-27T10:00:01.456Z',
               'message': {'role': 'user', 'content': [{'type': 'text', 'text': 'Fixture € text'}]}}
    return b''.join((json.dumps(record, ensure_ascii=False) + '\n').encode() for record in (header, message))


def upload(client, identifier, raw, offset=0, *, events=False):
    suffix = 'event-chunks' if events else 'chunks'
    return client.post(f'/agent-sessions/{identifier}/{suffix}',
                       json={'offset': offset, 'data': base64.b64encode(raw).decode()})


def test_invocation_identity_replay_terminal_state_and_phase_split(client):
    project = create_project(client)
    record = invocation(project)
    first = client.post('/agent-sessions', json=record)
    assert first.status_code == 200
    revision = first.json()['revision']
    assert client.post('/agent-sessions', json=record).json()['revision'] == revision
    for key, changed in [('worker_name', 'other'), ('phase', 'reason_execute'),
                         ('started_at', '2026-10-27T10:01:00Z'), ('source_fact_ids', ['goal'])]:
        response = client.post('/agent-sessions', json={**record, key: changed})
        assert response.status_code == 409, (key, response.text)
    finished = {**record, 'status': 'success', 'finished_at': '2026-10-27T10:00:02Z', 'capture_status': 'complete'}
    assert client.post('/agent-sessions', json=finished).status_code == 200
    assert client.post('/agent-sessions', json=record).json()['status'] == 'success'
    resumed = invocation(project, conversation_id=record['conversation_id'], phase='explore_conclude')
    assert client.post('/agent-sessions', json=resumed).status_code == 200
    page = client.get('/agent-sessions', params={'project_id': project}).json()
    assert page['total'] == 2 and {item['conversation_id'] for item in page['items']} == {record['conversation_id']}
    assert client.post('/agent-sessions', json=invocation(create_project(client), conversation_id=record['conversation_id'])).status_code == 409


def test_raw_pi_header_and_original_entries_are_preserved_with_chunk_replay(client):
    record = invocation(create_project(client))
    client.post('/agent-sessions', json=record)
    raw = session_bytes()
    split = raw.index(b'\n') + 1
    first = upload(client, record['id'], raw[:split])
    assert first.json() == {'offset': split, 'duplicate': False}
    assert upload(client, record['id'], raw[:split]).json() == {'offset': split, 'duplicate': True}
    assert upload(client, record['id'], b'conflict', 0).status_code == 409
    assert upload(client, record['id'], b'later', len(raw) + 1).status_code == 409
    assert upload(client, record['id'], raw[split:], split).json()['offset'] == len(raw)
    stored = session_service.conversation_file(record['conversation_id']).read_bytes()
    assert stored == raw
    detail = client.get('/agent-sessions/' + record['id']).json()
    assert detail['pi_session_id'] == 'actual-pi-header-id' and detail['bytes_captured'] == len(raw)
    assert detail['viewer_url'] == '/session?id=' + record['conversation_id'] + '.jsonl'
    assert json.loads(stored.splitlines()[1])['timestamp'] == '2026-10-27T10:00:01.456Z'


@pytest.mark.parametrize('header', [[], 7, None, {'type': 'session'}, {'type': 'message', 'id': 'wrong'}])
def test_invalid_session_header_is_rejected_without_exposing_file(client, header):
    record = invocation(create_project(client))
    client.post('/agent-sessions', json=record)
    response = upload(client, record['id'], (json.dumps(header) + '\n').encode())
    assert response.status_code == 422
    assert not session_service.conversation_file(record['conversation_id']).exists()
    assert client.get('/agent-sessions/' + record['id']).json()['viewer_url'] is None


def test_transport_bounds_encoding_and_source_scope_are_enforced(client):
    project = create_project(client)
    record = invocation(project)
    client.post('/agent-sessions', json=record)
    url = '/agent-sessions/' + record['id'] + '/chunks'
    assert client.post(url, json={'offset': -1, 'data': ''}).status_code == 422
    assert client.post(url, json={'offset': True, 'data': ''}).status_code == 422
    assert client.post(url, json={'offset': 0, 'data': '!!!!'}).status_code == 422
    assert upload(client, record['id'], b'x' * (CHUNK_BYTES + 1)).status_code in (413, 422)
    assert upload(client, record['id'], b'xx', SESSION_BYTES).status_code == 413
    assert client.post('/agent-sessions', json=invocation(project, source_fact_ids=['private-other-project'])).status_code == 422
    assert client.post('/agent-sessions', json=invocation(project, intent_id='nonexistent')).status_code == 422
    assert client.get('/agent-sessions', params={'limit': 101}).status_code == 422


def test_session_graph_source_result_and_edge_filters_follow_explicit_links(client):
    project = create_project(client)
    created = client.post(f'/projects/{project}/intents', json={'from': ['origin'], 'description': 'Fixture edge',
                                                             'creator': 'fixture-pi', 'worker': 'fixture-pi'})
    assert created.status_code == 201
    intent = created.json()['id']
    record = invocation(project, intent_id=intent)
    client.post('/agent-sessions', json=record)
    concluded = client.post(f'/projects/{project}/intents/{intent}/conclude',
                            json={'worker': 'fixture-pi', 'description': 'Fixture result'})
    fact = concluded.json()['fact']['id']
    second = client.post(f'/projects/{project}/intents', json={'from': [fact], 'description': 'Next fixture edge',
                                                            'creator': 'fixture-pi'}).json()['id']
    response = client.post('/agent-conversations/' + record['conversation_id'] + '/results',
                           json={'project_id': project, 'result_fact_ids': [fact], 'result_intent_ids': [second]})
    assert response.status_code == 200
    for query in ({'fact_id': 'origin'}, {'fact_id': fact}, {'intent_id': intent}, {'intent_id': second}):
        result = client.get('/agent-sessions', params={'project_id': project, **query}).json()
        assert [item['id'] for item in result['items']] == [record['id']]
    detail = client.get('/agent-sessions/' + record['id']).json()
    assert detail['source_fact_ids'] == ['origin'] and detail['result_fact_ids'] == [fact]
    assert detail['intent_id'] == intent and detail['result_intent_ids'] == [second]
    assert client.post('/agent-conversations/' + record['conversation_id'] + '/results',
                       json={'project_id': project, 'result_fact_ids': ['outside']}).status_code == 422
    assert client.get('/agent-sessions', params={'project_id': create_project(client), 'fact_id': 'origin'}).json()['total'] == 0


def test_choice_retry_keeps_original_attempt_project_and_workflow_provenance(client):
    from test_workflow_choices import create, discover, stages, unavailable
    workflow = create(client)
    discover(client, workflow)
    old = {}
    record = None
    for option in ('pancakes', 'eggs', 'benedict'):
        old[option] = stages(client, workflow)[option]['project_id']
        if option == 'eggs':
            record = invocation(old[option])
            assert client.post('/agent-sessions', json=record).status_code == 200
            upload(client, record['id'], session_bytes())
        assert unavailable(client, old[option]).status_code == 200
    wid = workflow['workflow']['id']
    assert client.post(f'/workflows/{wid}/choices/research/retry',
                       json={'goal_id': 'eggs', 'reason': 'New fixture evidence permits another attempt'}).status_code == 200
    assert client.put(f'/workflows/{wid}/status', json={'status': 'active'}).status_code == 200
    fresh = stages(client, workflow)['eggs']['project_id']
    assert fresh != old['eggs']
    historical = client.get('/agent-sessions/' + record['id']).json()
    assert historical['project_id'] == old['eggs'] and historical['stage_id'] == 'eggs' and historical['workflow_id'] == wid
    assert client.get('/agent-sessions', params={'project_id': fresh}).json()['total'] == 0
    assert client.get('/agent-sessions', params={'workflow_id': wid}).json()['items'][0]['id'] == record['id']


def test_paused_and_trashed_workflows_keep_late_final_capture_and_history(client, tmp_path, monkeypatch):
    workflow = client.post('/workflows', json={'origin': 'Fixture origin', 'stages': [{'id': 'work', 'goal': 'Fixture goal'}]}).json()
    wid, project = workflow['workflow']['id'], workflow['stages'][0]['project_id']
    record = invocation(project)
    assert client.put(f'/workflows/{wid}/status', json={'status': 'stopped'}).status_code == 200
    assert client.post('/agent-sessions', json=record).status_code == 200
    assert client.delete(f'/workflows/{wid}').status_code == 200
    assert upload(client, record['id'], session_bytes()).status_code == 200
    terminal = {**record, 'status': 'cancelled', 'finished_at': '2026-10-27T10:01:00Z', 'capture_status': 'complete', 'cancel_reason': 'workflow trashed'}
    assert client.post('/agent-sessions', json=terminal).status_code == 200
    assert client.get('/agent-sessions', params={'workflow_id': wid}).json()['total'] == 1
    global_sessions = tmp_path / 'fake-home' / '.pi' / 'agent' / 'sessions' / 'unrelated'
    global_sessions.mkdir(parents=True)
    (global_sessions / 'private.jsonl').write_text('PRIVATE_UNRELATED_HISTORY')
    monkeypatch.setenv('HOME', str(tmp_path / 'fake-home'))
    listed = client.get('/agent-sessions').json()
    assert listed['total'] == 1 and 'PRIVATE_UNRELATED_HISTORY' not in json.dumps(listed)
    assert session_service.agent_dir() != global_sessions.parents[1]
    assert client.get('/agent-sessions/' + str(uuid4())).status_code == 404


def test_actual_event_pagination_and_sse_emit_recorded_events(client):
    record = invocation(create_project(client))
    client.post('/agent-sessions', json=record)
    events = [{'type': 'agent_start'}, {'type': 'tool_execution_start', 'toolName': 'bash'},
              {'type': 'message_update', 'assistantMessageEvent': {'type': 'text_delta', 'delta': 'Fixture progress'}}]
    raw = b''.join((json.dumps(event) + '\n').encode() for event in events)
    assert upload(client, record['id'], raw, events=True).status_code == 200
    page = client.get(f"/agent-sessions/{record['id']}/events", params={'limit': 2}).json()
    assert page['items'] == [{'sequence': i+1, 'event': event} for i, event in enumerate(events[:2])]
    assert page['next_after'] == 2 and page['has_more'] is True
    tail = client.get(f"/agent-sessions/{record['id']}/events", params={'after': 2}).json()
    assert tail['items'] == [{'sequence': 3, 'event': events[2]}] and tail['has_more'] is False
    class Request:
        async def is_disconnected(self): return False
    async def read_first():
        response = await stream(UUID(record['id']), Request(), after=1)
        iterator = response.body_iterator
        first = await anext(iterator)
        await iterator.aclose()
        return first
    actual = asyncio.run(read_first())
    assert actual.startswith('event: update\ndata: ')
    payload = json.loads(actual.split('data: ', 1)[1])
    assert payload['events'] == [{'sequence': 2, 'event': events[1]}, {'sequence': 3, 'event': events[2]}]
    assert payload['session']['status'] == 'running' and payload['next_after'] == 3


def test_invalid_event_tail_never_traps_pagination_in_has_more(client):
    record = invocation(create_project(client))
    client.post('/agent-sessions', json=record)
    raw = b'{"type":"agent_start"}\nnot-json\n{"other":"not-an-event"}\n'
    assert upload(client, record['id'], raw, events=True).status_code in (200, 422)
    page = client.get(f"/agent-sessions/{record['id']}/events", params={'after': 1}).json()
    assert page['has_more'] is False or page['next_after'] > 1


@pytest.mark.parametrize('events', [False, True])
def test_replayed_fsynced_chunk_repairs_database_after_crash_window(client, events):
    record = invocation(create_project(client))
    client.post('/agent-sessions', json=record)
    raw = b'{"type":"agent_start"}\n' if events else session_bytes()
    assert upload(client, record['id'], raw, events=events).status_code == 200
    # Simulate fsync completing and the surrounding SQLite transaction rolling
    # back. The original raw file survives; replay must reconcile its metadata.
    with db.get_conn() as conn:
        if events:
            conn.execute('UPDATE agent_invocations SET events_bytes=0 WHERE id=?', (record['id'],))
        else:
            conn.execute('UPDATE agent_conversations SET bytes_captured=0,pi_session_id=NULL WHERE id=?', (record['conversation_id'],))
    response = upload(client, record['id'], raw, events=events)
    assert response.status_code == 200 and response.json()['duplicate'] is True
    metadata = client.get('/agent-sessions/' + record['id']).json()
    assert metadata['events_bytes' if events else 'bytes_captured'] == len(raw)
    if not events:
        assert metadata['pi_session_id'] == 'actual-pi-header-id' and metadata['viewer_url']
    path = session_service.event_file(record['id']) if events else session_service.conversation_file(record['conversation_id'])
    assert path.read_bytes() == raw


def test_partial_overlap_replay_verifies_prefix_and_appends_new_suffix(client):
    record = invocation(create_project(client))
    client.post('/agent-sessions', json=record)
    raw = session_bytes()
    assert upload(client, record['id'], raw).status_code == 200
    suffix = b'{"type":"session_info","name":"Fixture appended"}\n'
    response = upload(client, record['id'], raw[-10:] + suffix, len(raw) - 10)
    assert response.status_code == 200 and response.json()['offset'] == len(raw) + len(suffix)
    assert session_service.conversation_file(record['conversation_id']).read_bytes() == raw + suffix
    conflicting = upload(client, record['id'], b'wrong-prefix' + suffix, len(raw) - 10)
    assert conflicting.status_code == 409
    assert session_service.conversation_file(record['conversation_id']).read_bytes() == raw + suffix


def test_partial_initial_header_crash_recovers_original_header_and_exact_bytes(client):
    record = invocation(create_project(client))
    client.post('/agent-sessions', json=record)
    raw = session_bytes()
    path = session_service.conversation_file(record['conversation_id'])
    path.write_bytes(raw[:19])
    response = upload(client, record['id'], raw)
    assert response.status_code == 200 and response.json()['offset'] == len(raw)
    assert path.read_bytes() == raw
    metadata = client.get('/agent-sessions/' + record['id']).json()
    assert metadata['bytes_captured'] == len(raw) and metadata['pi_session_id'] == 'actual-pi-header-id'


@pytest.mark.parametrize('unsafe', ['host-agent', 'auth.json', 'models.json'])
def test_viewer_root_rejects_host_pi_configuration_or_credentials(client, tmp_path, monkeypatch, unsafe):
    fake_home = tmp_path / 'fake-home'
    host_agent = fake_home / '.pi' / 'agent'
    host_agent.mkdir(parents=True)
    monkeypatch.setenv('HOME', str(fake_home))
    monkeypatch.setenv('PI_CODING_AGENT_DIR', str(host_agent))
    root = host_agent if unsafe == 'host-agent' else tmp_path / 'unsafe-viewer'
    root.mkdir(exist_ok=True)
    if unsafe != 'host-agent':
        (root / unsafe).write_text('fixture marker only')
    monkeypatch.setenv('CAIRN_PI_VIEWER_DIR', str(root))
    response = client.post('/agent-sessions', json=invocation(create_project(client)))
    assert response.status_code == 503
    assert 'fixture marker' not in response.text


def test_actual_pi_header_cannot_be_rebound_to_another_conversation(client):
    project = create_project(client)
    first = invocation(project)
    second = invocation(project)
    assert client.post('/agent-sessions', json=first).status_code == 200
    assert client.post('/agent-sessions', json=second).status_code == 200
    raw = session_bytes()
    assert upload(client, first['id'], raw).status_code == 200
    response = upload(client, second['id'], raw)
    assert response.status_code == 409 and 'another Cairn conversation' in response.text
    assert not session_service.conversation_file(second['conversation_id']).exists()
    assert client.get('/agent-sessions/' + second['id']).json()['pi_session_id'] is None


def test_execute_and_conclude_share_original_pi_header_and_append_only_history(client):
    project = create_project(client)
    execute = invocation(project)
    client.post('/agent-sessions', json=execute)
    raw = session_bytes()
    assert upload(client, execute['id'], raw).status_code == 200
    client.post('/agent-sessions', json={**execute, 'status': 'timed_out', 'finished_at': '2026-10-27T10:01:00Z'})
    conclude = invocation(project, conversation_id=execute['conversation_id'], phase='explore_conclude', started_at='2026-10-27T10:01:01Z')
    registered = client.post('/agent-sessions', json=conclude)
    assert registered.status_code == 200 and registered.json()['bytes_captured'] == len(raw)
    assert upload(client, conclude['id'], raw).json()['duplicate'] is True
    suffix = b'{"type":"session_info","name":"Fixture conclude"}\n'
    assert upload(client, conclude['id'], suffix, len(raw)).status_code == 200
    client.post('/agent-sessions', json={**conclude, 'status': 'success', 'finished_at': '2026-10-27T10:01:02Z'})
    first = client.get('/agent-sessions/' + execute['id']).json()
    second = client.get('/agent-sessions/' + conclude['id']).json()
    assert first['pi_session_id'] == second['pi_session_id'] == 'actual-pi-header-id'
    assert first['conversation_id'] == second['conversation_id'] and first['id'] != second['id']
    assert first['status'] == 'timed_out' and second['status'] == 'success'
    assert session_service.conversation_file(execute['conversation_id']).read_bytes() == raw + suffix


def test_deleted_standalone_project_can_finalize_existing_invocation_without_accepting_new_work(client):
    project = create_project(client)
    record = invocation(project)
    assert client.post('/agent-sessions', json=record).status_code == 200
    assert client.delete('/projects/' + project).status_code == 204
    terminal = {**record, 'status': 'cancelled', 'finished_at': '2026-10-27T10:01:00Z',
                'capture_status': 'unavailable', 'cancel_reason': 'project deleted'}
    final = client.post('/agent-sessions', json=terminal)
    assert final.status_code == 200
    assert client.get('/agent-sessions/' + record['id']).json()['status'] == 'cancelled'
    assert client.post('/agent-sessions', json=invocation(project)).status_code == 404


@pytest.mark.parametrize(('decision', 'delayed'), [('approved', False), ('approved', True), ('rejected', False)])
def test_review_links_exact_producer_and_reviewer_sessions_without_bypassing_scope(client, decision, delayed):
    response = client.post('/projects', json={'origin': 'Fixture origin', 'goal': 'Verified fixture goal', 'verification_enabled': True})
    assert response.status_code == 201
    project = response.json()['project']['id']
    producer = invocation(project, phase='reason_execute')
    reviewer = invocation(project, phase='review_execute', worker_name='fixture-verifier')
    producer_path = 'cairn-sessions/' + producer['conversation_id'] + '.jsonl'
    reviewer_path = 'cairn-sessions/' + reviewer['conversation_id'] + '.jsonl'
    if not delayed:
        assert client.post('/agent-sessions', json=producer).status_code == 200
        assert client.post('/agent-sessions', json=reviewer).status_code == 200
    completion = client.post(f'/projects/{project}/complete', json={
        'from': ['origin'], 'description': 'Fixture verified result', 'worker': producer['worker_name'],
        'producer_agent_id': 'fixture-producer-agent', 'producer_session_id': producer_path})
    assert completion.status_code == 200, completion.text
    claim_id = completion.json()['id']
    # A claim has not completed the goal and must not create fabricated links.
    assert client.get('/agent-sessions', params={'project_id': project, 'fact_id': 'goal'}).json()['total'] == 0
    identity = {'agent_id': 'fixture-reviewer-agent', 'session_id': reviewer_path, 'worker': reviewer['worker_name']}
    claimed = client.post('/reviews/' + claim_id + '/claim', json=identity)
    assert claimed.status_code == 200
    result = client.post('/reviews/' + claim_id + '/decision', json={
        **identity, 'lease_token': claimed.json()['lease_token'], 'decision': decision,
        'feedback': 'Fixture independently checked', 'evidence': ['Fixture deterministic test passed'],
        'cli_session_id': reviewer_path})
    assert result.status_code == 200, result.text
    if delayed:
        assert client.post('/agent-sessions', json=producer).status_code == 200
        assert client.post('/agent-sessions', json=reviewer).status_code == 200
    unrelated = invocation(create_project(client), phase='reason_execute')
    assert client.post('/agent-sessions', json=unrelated).status_code == 200
    goal_page = client.get('/agent-sessions', params={'project_id': project, 'fact_id': 'goal'}).json()
    expected_ids = {producer['id'], reviewer['id']} if decision == 'approved' else set()
    assert {item['id'] for item in goal_page['items']} == expected_ids
    for item in goal_page['items']:
        assert item['result_fact_ids'] == ['goal'] and item['source_fact_ids'] == ['origin']
    assert client.get('/agent-sessions', params={'project_id': unrelated['project_id'], 'fact_id': 'goal'}).json()['total'] == 0
    assert client.get('/agent-sessions', params={'workflow_id': 'unrelated-workflow', 'fact_id': 'goal'}).json()['total'] == 0
    if decision == 'approved':
        edge = next(item for item in client.get('/projects/' + project).json()['intents'] if item['to'] == 'goal')['id']
        edge_page = client.get('/agent-sessions', params={'project_id': project, 'intent_id': edge}).json()
        assert {item['id'] for item in edge_page['items']} == expected_ids
        assert all(item['result_intent_ids'] == [edge] for item in edge_page['items'])
        assert client.get('/agent-sessions', params={'project_id': unrelated['project_id'], 'intent_id': edge}).json()['total'] == 0
        assert client.get('/agent-sessions', params={'workflow_id': 'unrelated-workflow', 'intent_id': edge}).json()['total'] == 0
        assert client.get('/agent-sessions', params={'project_id': project, 'intent_id': edge, 'fact_id': 'unrelated-fact'}).json()['total'] == 0


@pytest.mark.parametrize(('age', 'expected'), [(0, 'running'), (20, 'running'), (60, 'unconfirmed'), (-60, 'unconfirmed')])
def test_runtime_liveness_uses_recent_dispatcher_report_not_message_activity(client, monkeypatch, age, expected):
    from datetime import datetime, timedelta, timezone
    now = datetime(2026, 10, 27, 10, 2, tzinfo=timezone.utc)
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None): return now if tz is None else now.astimezone(tz)
    monkeypatch.setattr(session_service, 'datetime', FrozenDatetime)
    heartbeat = (now - timedelta(seconds=age)).isoformat()
    record = invocation(create_project(client), heartbeat_at=heartbeat, dispatcher_instance_id=str(uuid4()))
    assert client.post('/agent-sessions', json=record).status_code == 200
    raw = session_bytes()
    upload(client, record['id'], raw)
    upload(client, record['id'], b'{"type":"agent_start"}\n', events=True)
    detail = client.get('/agent-sessions/' + record['id']).json()
    assert detail['status'] == 'running' and detail['runtime_status'] == expected
    assert detail['bytes_captured'] == len(raw) and detail['events_captured'] == 1
    assert detail['pi_session_id'] == 'actual-pi-header-id' and detail['viewer_url']
    assert datetime.fromisoformat(detail['runtime_last_reported_at'].replace('Z', '+00:00')) == datetime.fromisoformat(heartbeat)
    # A fresh dispatcher heartbeat restores confirmed runtime activity without
    # adding a message, event, or transcript byte.
    refreshed = client.post('/agent-sessions', json={**record, 'heartbeat_at': now.isoformat()}).json()
    assert refreshed['runtime_status'] == 'running' and refreshed['events_captured'] == 1
    assert refreshed['bytes_captured'] == len(raw)


def test_delivery_ack_requires_declared_raw_and_event_targets_and_cannot_regress(client):
    record = invocation(create_project(client), status='success', finished_at='2026-10-27T10:00:02Z', capture_status='complete',
                        delivery_status='pending', delivery_session_bytes=1, delivery_event_bytes=1)
    assert client.post('/agent-sessions', json=record).status_code == 200
    assert client.post('/agent-sessions', json={**record, 'delivery_status': 'delivered'}).status_code == 409
    raw = session_bytes()
    assert upload(client, record['id'], raw).status_code == 200
    assert client.post('/agent-sessions', json={**record, 'delivery_status': 'delivered'}).status_code == 409
    event = b'{"type":"agent_end"}\n'
    assert upload(client, record['id'], event, events=True).status_code == 200
    final = {**record, 'delivery_status': 'delivered', 'delivery_session_bytes': len(raw), 'delivery_event_bytes': len(event)}
    assert client.post('/agent-sessions', json=final).json()['delivery_status'] == 'delivered'
    assert client.post('/agent-sessions', json=record).json()['delivery_status'] == 'delivered'
