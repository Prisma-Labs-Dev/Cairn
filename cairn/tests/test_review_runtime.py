import pytest
from concurrent.futures import Future
from cairn.dispatcher.protocol.client import ApiResult
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.process import ProcessResult
from cairn.dispatcher.tasks import review
from conftest import FakeContainerManager, FakeDriver, FakeLease, make_config, make_project
from test_runtime_controls import loop_for_test, shutdown, summary


class ReviewClient:
    def __init__(self):
        self.claims = []
        self.decisions = []
        self.releases = []

    def claim_review(self, claim_id, identity):
        self.claims.append(dict(identity))
        return ApiResult(200, {'id': claim_id, 'lease_token': 'lease', 'snapshot': {'facts': [{'id': 'goal', 'description': 'Correct result'}]}, 'producer_session_id': 'producer-session'})

    def review_heartbeat(self, *_args):
        return ApiResult(200, {})

    def release_review(self, claim_id, identity):
        self.releases.append((claim_id, identity))
        return ApiResult(200, {})

    def decide_review(self, claim_id, payload):
        self.decisions.append((claim_id, payload))
        return ApiResult(200, {})


def test_review_uses_fresh_execute_and_records_usage_context(monkeypatch):
    config = make_config()
    project = make_project()
    project.project.pending_review_id = 'review-1'
    client = ReviewClient()
    driver = FakeDriver()
    lease = FakeLease()
    calls = []
    monkeypatch.setattr(review, 'get_driver', lambda *_a: driver)
    monkeypatch.setattr(review, 'HeartbeatLease', lambda *_a: lease)
    def process(*args, **kwargs):
        calls.append(kwargs)
        return ProcessResult(0, '{"decision":"approved","feedback":"Goal checked with passing tests","evidence":["pytest passed"]}', '')
    monkeypatch.setattr(review, 'run_worker_process', process)
    assert review.run_review_task(config, client, FakeContainerManager(), project, config.workers[0], TaskCancellation()) == 'success'
    assert len(driver.execute_prompts) == 1
    assert driver.conclude_prompts == []
    assert 'NEW agent session' in driver.execute_prompts[0]
    assert client.claims[0]['agent_id'].startswith('verifier_')
    assert client.claims[0]['session_id'] == 'session-001'
    assert calls[0]['usage_client'] is client
    assert calls[0]['usage_project_id'] == project.project.id
    assert calls[0]['phase'] == 'review_execute'
    assert client.decisions[0][1]['lease_token'] == 'lease'
    assert lease.started and lease.stopped
    assert len(client.releases) == 1


def test_malformed_reviewer_output_releases_claim_without_deciding(monkeypatch):
    config = make_config()
    project = make_project()
    project.project.pending_review_id = 'review-1'
    client = ReviewClient()
    monkeypatch.setattr(review, 'get_driver', lambda *_a: FakeDriver())
    monkeypatch.setattr(review, 'HeartbeatLease', lambda *_a: FakeLease())
    monkeypatch.setattr(review, 'run_worker_process', lambda *_a, **_kw: ProcessResult(0, '{"decision":"approved"}', ''))
    assert review.run_review_task(config, client, FakeContainerManager(), project, config.workers[0], TaskCancellation()) == 'failed'
    assert client.decisions == [] and len(client.releases) == 1


def test_review_reuses_single_reason_worker_and_obeys_shared_caps(monkeypatch):
    loop = loop_for_test()
    loop.config.runtime.max_workers = 1
    loop.config.runtime.max_running_projects = 1
    loop.config.runtime.max_project_workers = 1
    project = make_project()
    project.project.pending_review_id = 'review-1'
    loop.client = type('Client', (), {'get_project': lambda _, pid: project.model_copy(deep=True, update={'project': project.project.model_copy(update={'id': pid})})})()
    loop.container_manager = type('Backend', (), {'container_name': lambda _, pid: pid})()
    submitted = []
    class Executor:
        def submit(self, action, *args):
            submitted.append((action, args))
            return Future()
        def shutdown(self, **_kw):
            return None
    loop.executor.shutdown(wait=True)
    loop.executor = Executor()
    try:
        loop._dispatch_available([summary('proj_001'), summary('proj_002')])
        assert len(submitted) == 1 and len(loop.futures) == 1
        assert next(iter(loop.futures.values())).task_type == 'review'
        assert next(iter(loop.futures.values())).worker_name == 'test-worker'
        assert loop._runtime_report().counts.running_tasks == 1
        loop._dispatch_available([summary('proj_001'), summary('proj_002')])
        assert len(submitted) == 1
    finally:
        shutdown(loop)


def test_review_invocation_reaches_durable_usage_recorder(monkeypatch, tmp_path):
    config = make_config()
    project = make_project()
    project.project.pending_review_id = 'review-1'
    client = ReviewClient()
    records = []
    client.record_usage_attempt = lambda payload: records.append(payload) or ApiResult(204)
    monkeypatch.setenv('CAIRN_USAGE_OUTBOX', str(tmp_path / 'usage.db'))
    monkeypatch.setattr(review, 'get_driver', lambda *_a: FakeDriver())
    monkeypatch.setattr(review, 'HeartbeatLease', lambda *_a: FakeLease())
    class Process:
        def start(self):
            pass
        def communicate(self, **_kwargs):
            return ProcessResult(0, '{"decision":"rejected","feedback":"Required result missing","evidence":["Inspected output path"]}', '')
    class Backend(FakeContainerManager):
        def build_exec_process(self, *_args, **_kwargs):
            return Process()
    assert review.run_review_task(config, client, Backend(), project, config.workers[0], TaskCancellation()) == 'success'
    assert len(records) == 1
    assert records[0]['phase'] == 'review_execute'
    assert records[0]['project_id'] == project.project.id
    assert records[0]['status'] == 'success'
    assert records[0]['coverage'] == 'unreported'
    assert client.decisions[0][1]['decision'] == 'rejected'


@pytest.mark.parametrize('override,expected', [
    (None, 'http://127.0.0.1:8000'),
    ('http://worker-visible:8000', 'http://worker-visible:8000'),
])
def test_frozen_artifact_prompt_uses_worker_visible_server(monkeypatch, override, expected):
    config = make_config()
    config.server = 'http://cairn-server:8000'
    config.runtime.history_server_url = override
    project = make_project()
    project.project.pending_review_id = 'review-1'
    driver = FakeDriver()
    class ArtifactClient(ReviewClient):
        def claim_review(self, claim_id, identity):
            response = super().claim_review(claim_id, identity)
            response.data['snapshot']['artifacts'] = [{'download_url': '/reviews/review-1/artifacts/artifact-1/download', 'sha256': 'frozen-hash'}]
            return response
    monkeypatch.setattr(review, 'get_driver', lambda *_a: driver)
    monkeypatch.setattr(review, 'HeartbeatLease', lambda *_a: FakeLease())
    monkeypatch.setattr(review, 'run_worker_process', lambda *_a, **_kw: ProcessResult(0, '{"decision":"approved","feedback":"Inspected frozen artifact","evidence":["frozen-hash"]}', ''))
    assert review.run_review_task(config, ArtifactClient(), FakeContainerManager(), project, config.workers[0], TaskCancellation()) == 'success'
    prompt = driver.execute_prompts[0]
    assert f'Stored artifact download URLs resolve against the Cairn server {expected}.' in prompt
    assert 'http://cairn-server:8000' not in prompt
    assert '/reviews/review-1/artifacts/artifact-1/download' in prompt
