from concurrent.futures import ThreadPoolExecutor
import pytest
from fastapi.testclient import TestClient
from wayline.server import db
from wayline.server.app import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, '_db_path', None)
    db.configure(tmp_path / 'reviews.db')
    with TestClient(app) as client:
        yield client


def project(client, enabled=True):
    return client.post('/projects', json={'goal': 'Produce correct result', 'verification_enabled': enabled}).json()['project']['id']


def complete(client, pid, **extra):
    return client.post(f'/projects/{pid}/complete', json={'from': ['origin'], 'description': 'Result is correct', 'worker': 'luna', 'producer_agent_id': 'producer-agent', 'producer_session_id': 'producer-session', **extra})


def reviewer(client, claim, **extra):
    identity = {'agent_id': 'review-agent', 'session_id': 'review-session', 'worker': 'luna', **extra}
    response = client.post(f'/reviews/{claim}/claim', json=identity)
    assert response.status_code == 200, response.text
    return {**identity, 'lease_token': response.json()['lease_token']}


def decide(client, claim, identity, decision='approved', **extra):
    return client.post(f'/reviews/{claim}/decision', json={**identity, 'decision': decision, 'feedback': 'Checked the actual result' if decision == 'approved' else 'Fix the missing result and run the test', 'evidence': ['test result verified'], **extra})


def test_default_off_preserves_immediate_completion(client):
    response = client.post('/projects', json={'goal': 'Finish'})
    pid = response.json()['project']['id']
    assert response.json()['project']['verification_enabled'] is False
    response = client.post(f'/projects/{pid}/complete', json={'from': ['origin'], 'description': 'Done', 'worker': 'luna'})
    assert response.status_code == 200
    assert response.json()['to'] == 'goal'
    assert client.get('/reviews').json()['items'] == []


def test_enabled_claim_freezes_evidence_and_waits_then_approves_once(client):
    pid = project(client)
    assert client.post(f'/projects/{pid}/complete', json={'from': ['origin'], 'description': 'Done', 'worker': 'luna'}).status_code == 422
    response = complete(client, pid)
    assert response.status_code == 200, response.text
    claim = response.json()
    cid = claim['id']
    assert claim['status'] == 'pending'
    assert complete(client, pid).json()['id'] == cid
    detail = client.get(f'/projects/{pid}').json()
    assert detail['project']['status'] == 'active'
    assert detail['project']['pending_review_id'] == cid
    assert detail['intents'] == []
    assert client.post(f'/projects/{pid}/reason/claim', json={'worker': 'luna', 'trigger': 'retry'}).status_code == 409
    assert client.post(f'/projects/{pid}/intents', json={'from': ['origin'], 'description': 'mutate', 'creator': 'other'}).status_code == 409
    assert complete(client, pid, description='different').status_code == 409
    for bad in [{'agent_id': 'producer-agent'}, {'session_id': 'producer-session'}]:
        identity = {'agent_id': 'review-agent', 'session_id': 'review-session', 'worker': 'luna', **bad}
        assert client.post(f'/reviews/{cid}/claim', json=identity).status_code == 409
    identity = reviewer(client, cid)
    assert client.get(f'/reviews/{cid}').json()['lease_token'] is None
    assert 'lease_token' not in client.get('/reviews').json()['items'][0]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: decide(client, cid, identity), range(2)))
    assert [result.status_code for result in results] == [200, 200]
    detail = client.get(f'/projects/{pid}').json()
    assert detail['project']['status'] == 'completed'
    assert detail['project']['pending_review_id'] is None
    assert len(detail['intents']) == 1
    assert complete(client, pid).json()['id'] == cid
    assert client.get(f'/reviews/{cid}').json()['reviewer_session_id'] == 'review-session'
    # Data survives a new SQLite connection and process-level schema initialization.
    with db.get_conn() as conn:
        assert conn.execute('SELECT COUNT(*) FROM completion_claims').fetchone()[0] == 1


def test_rejection_creates_rework_and_stale_reviews_cannot_complete(client):
    pid = project(client)
    cid = complete(client, pid).json()['id']
    identity = reviewer(client, cid)
    assert decide(client, cid, identity, 'rejected').status_code == 200
    detail = client.get(f'/projects/{pid}').json()
    assert detail['project']['status'] == 'active'
    assert detail['project']['pending_review_id'] is None
    assert detail['intents'][0]['creator'] == 'verification'
    assert 'Fix the missing result' in detail['intents'][0]['description']
    assert decide(client, cid, identity).status_code == 409
    assert complete(client, pid).json()['status'] == 'rejected'
    assert complete(client, pid, description='corrected').status_code == 409


def test_pause_resume_invalidates_lease_and_expired_attempts_are_bounded(client):
    pid = project(client)
    cid = complete(client, pid).json()['id']
    identity = reviewer(client, cid)
    assert client.put(f'/projects/{pid}/status', json={'status': 'stopped'}).status_code == 200
    assert decide(client, cid, identity).status_code == 403
    assert client.put(f'/projects/{pid}/status', json={'status': 'active'}).status_code == 200
    assert decide(client, cid, identity).status_code == 409
    for index in range(3):
        identity = reviewer(client, cid, agent_id=f'review-{index}', session_id=f'session-{index}')
        assert client.post(f'/reviews/{cid}/release', json=identity).status_code == 200
    final = reviewer(client, cid, agent_id='fourth', session_id='fourth')
    assert final['lease_token'] is None
    assert client.get(f'/projects/{pid}').json()['project']['status'] == 'stopped'
    assert client.put(f'/projects/{pid}/status', json={'status': 'active'}).status_code == 200
    assert reviewer(client, cid, agent_id='new', session_id='new')['lease_token']


def test_workflow_expansion_and_artifacts_wait_for_independent_approval(client):
    workflow = client.post('/workflows', json={'stages': [
        {'id': 'discover', 'goal': 'Find follow-up work', 'verification_enabled': True, 'expansion': {'max_goals': 2}, 'artifacts': [{'path': 'result.txt'}]},
        {'id': 'join', 'goal': 'Combine', 'depends_on': ['discover']},
    ]}).json()
    pid = workflow['stages'][0]['project_id']
    assert workflow['stages'][0]['verification_enabled'] is True
    assert complete(client, pid, goals=[]).status_code == 409
    assert client.post(f'/projects/{pid}/artifacts?path=result.txt', content=b'original evidence').status_code == 201
    goals = [{'id': 'child', 'goal': 'Investigate result'}]
    cid = complete(client, pid, goals=goals).json()['id']
    assert client.post(f'/projects/{pid}/artifacts?path=result.txt', content=b'changed evidence').status_code == 409
    workflow_id = workflow['workflow']['id']
    detail = client.get(f'/workflows/{workflow_id}').json()
    assert len(detail['stages']) == 2
    assert detail['stages'][1]['project_id'] is None
    assert detail['stages'][0]['pending_review_id'] == cid
    identity = reviewer(client, cid)
    assert decide(client, cid, identity).status_code == 200
    assert decide(client, cid, identity).status_code == 200
    detail = client.get(f'/workflows/{workflow_id}').json()
    assert len(detail['stages']) == 3
    assert detail['stages'][0]['status'] == 'completed'
    assert detail['stages'][1]['project_id'] is None
    assert detail['stages'][2]['project_id'] is not None
    assert detail['stages'][1]['depends_on'] == ['discover', 'child']


def test_invalid_goals_never_create_claim_or_children(client):
    workflow = client.post('/workflows', json={'stages': [{'id': 'start', 'goal': 'Find goals', 'verification_enabled': True, 'expansion': {'max_goals': 1}}]}).json()
    pid = workflow['stages'][0]['project_id']
    assert complete(client, pid, goals=[{'id': 'start', 'goal': 'Duplicate'}]).status_code == 409
    assert client.get('/reviews').json()['items'] == []
    assert len(client.get(f"/workflows/{workflow['workflow']['id']}").json()['stages']) == 1


def test_three_rejections_pause_then_explicit_resume_resets_budget(client):
    pid = project(client)
    for index in range(3):
        cid = complete(client, pid, description=f'attempt {index}', producer_session_id=f'producer-{index}').json()['id']
        identity = reviewer(client, cid, agent_id=f'reviewer-{index}', session_id=f'review-{index}')
        assert decide(client, cid, identity, 'rejected').status_code == 200
        detail = client.get(f'/projects/{pid}').json()
        assert detail['project']['status'] == ('stopped' if index == 2 else 'active')
        if index < 2:
            intent = detail['intents'][-1]
            assert client.post(f"/projects/{pid}/intents/{intent['id']}/heartbeat", json={'worker': 'fixer'}).status_code == 200
            assert client.post(f"/projects/{pid}/intents/{intent['id']}/conclude", json={'worker': 'fixer', 'description': 'Fixed the issue'}).status_code == 200
    assert client.put(f'/projects/{pid}/status', json={'status': 'active'}).status_code == 200
    with db.get_conn() as conn:
        assert conn.execute('SELECT verification_rejections FROM projects WHERE id=?', (pid,)).fetchone()[0] == 0


def test_review_history_is_bounded_summary_with_explicit_detail(client):
    for index in range(3):
        pid = project(client)
        complete(client, pid, description='long result ' * 100, producer_session_id=f'run-{index}')
    page = client.get('/reviews?limit=2').json()
    assert page['total'] == 3 and page['next_offset'] == 2
    assert len(page['items']) == 2
    assert len(page['items'][0]['completion_description']) == 240
    assert all('snapshot' not in item and 'completion' not in item for item in page['items'])
    last = client.get('/reviews?limit=2&offset=2').json()
    assert len(last['items']) == 1 and last['next_offset'] is None
    detail = client.get(f"/reviews/{page['items'][0]['id']}").json()
    assert detail['snapshot']['facts'] and len(detail['completion']['description']) > 240
    assert client.get('/reviews?limit=101').status_code == 422
    assert client.get('/reviews?offset=-1').status_code == 422


@pytest.mark.parametrize('evidence', [[], [''], ['   '], ['a' * 4001]])
def test_approval_requires_nonblank_bounded_observed_evidence(client, evidence):
    pid = project(client)
    cid = complete(client, pid).json()['id']
    identity = reviewer(client, cid)
    assert decide(client, cid, identity, evidence=evidence).status_code == 422
    assert client.get(f'/projects/{pid}').json()['project']['status'] == 'active'
    assert client.get(f'/reviews/{cid}').json()['status'] == 'pending'


def test_actual_cli_session_is_audited_and_cannot_match_producer(client):
    pid = project(client)
    cid = complete(client, pid).json()['id']
    identity = reviewer(client, cid)
    assert decide(client, cid, identity, cli_session_id='producer-session').status_code == 409
    assert decide(client, cid, identity, cli_session_id='actual-cli-thread').status_code == 200
    assert client.get(f'/reviews/{cid}').json()['reviewer_cli_session_id'] == 'actual-cli-thread'


def test_rejected_claim_keeps_original_artifact_bytes_after_rework(client):
    workflow = client.post('/workflows', json={'stages': [{'goal': 'Write file', 'verification_enabled': True, 'artifacts': [{'path': 'output.txt'}]}]}).json()
    pid = workflow['stages'][0]['project_id']
    assert client.post(f'/projects/{pid}/artifacts?path=output.txt', content=b'original').status_code == 201
    claim = complete(client, pid).json()
    frozen_url = claim['snapshot']['artifacts'][0]['download_url']
    assert client.get(frozen_url).content == b'original'
    identity = reviewer(client, claim['id'])
    assert decide(client, claim['id'], identity, 'rejected').status_code == 200
    intent = client.get(f'/projects/{pid}').json()['intents'][0]
    client.post(f"/projects/{pid}/intents/{intent['id']}/heartbeat", json={'worker': 'fixer'})
    client.post(f"/projects/{pid}/intents/{intent['id']}/conclude", json={'worker': 'fixer', 'description': 'Fixed'})
    assert client.post(f'/projects/{pid}/artifacts?path=output.txt', content=b'corrected').status_code == 201
    assert client.get(frozen_url).content == b'original'


def test_claim_rejects_duplicate_sources_and_other_producer_reason_lease(client):
    pid = project(client)
    assert complete(client, pid, **{'from': ['origin', 'origin']}).status_code == 422
    assert client.post(f'/projects/{pid}/reason/claim', json={'worker': 'other-worker', 'trigger': 'reason'}).status_code == 200
    assert complete(client, pid).status_code == 409
    assert client.get('/reviews').json()['items'] == []


@pytest.mark.parametrize('enabled', [False, True])
def test_generated_goals_inherit_parent_verification_policy(client, enabled):
    workflow = client.post('/workflows', json={'stages': [{'id': 'discover', 'goal': 'Discover work', 'verification_enabled': enabled, 'expansion': {'max_goals': 2}}]}).json()
    wid = workflow['workflow']['id']
    pid = workflow['stages'][0]['project_id']
    result = complete(client, pid, goals=[{'id': 'child', 'goal': 'Do discovered work'}])
    assert result.status_code == 200
    if enabled:
        before = client.get(f'/workflows/{wid}').json()
        assert len(before['stages']) == 1
        cid = result.json()['id']
        assert decide(client, cid, reviewer(client, cid)).status_code == 200
    stages = client.get(f'/workflows/{wid}').json()['stages']
    child = next(stage for stage in stages if stage['id'] == 'child')
    assert child['verification_enabled'] is enabled
    assert client.get(f"/projects/{child['project_id']}").json()['project']['verification_enabled'] is enabled
    # Proposals have no field through which to override the operator policy.
    bad = complete(client, child['project_id'], goals=[{'goal': 'Bypass', 'verification_enabled': False}])
    assert bad.status_code == 422
