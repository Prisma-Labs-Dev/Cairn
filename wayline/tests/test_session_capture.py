from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest

from wayline.dispatcher.config import LocalConfig, WorkerConfig
from wayline.dispatcher.protocol.client import ApiResult
from wayline.dispatcher.runtime.local_backend import LocalBackend
from wayline.dispatcher.runtime.process import ProcessResult
from wayline.dispatcher.session_capture import SessionCapture, flush_sessions
from wayline.dispatcher import session_capture as module
from wayline.dispatcher.tasks.common import run_worker_process
from wayline.server import session_service
from test_agent_sessions import client, create_project, session_bytes


class SessionClient:
    _base_url = 'http://hermetic-session-fixture'
    def __init__(self, http):
        self.http, self.offline = http, False
        self.calls = []
    def _post(self, path, payload):
        self.calls.append((path, payload))
        if self.offline:
            return ApiResult(0)
        response = self.http.post(path, json=payload)
        return ApiResult(response.status_code, response.json())
    def record_session_invocation(self, payload):
        return self._post('/agent-sessions', payload)
    def record_session_chunk(self, identifier, payload, *, events=False):
        return self._post(f'/agent-sessions/{identifier}/' + ('event-chunks' if events else 'chunks'), payload)
    def record_session_result(self, identifier, payload):
        return self._post(f'/agent-conversations/{identifier}/results', payload)


def worker():
    return WorkerConfig(name='fixture-pi', type='pi', task_types=['explore'], max_running=1, priority=0)


def capture_fixture(client, tmp_path, *, backend=None, runtime=None):
    project = create_project(client)
    backend = backend or LocalBackend(LocalConfig(workspace_root=str(tmp_path / 'work'), completed_action='remove'))
    runtime = runtime or backend.ensure_running(project)
    conversation = str(uuid4())
    source = f'wayline-sessions/{conversation}.jsonl'
    reporter = SessionClient(client)
    attempt = SimpleNamespace(attempt_id=uuid4(), started_at=datetime.now(timezone.utc))
    capture = SessionCapture(reporter, backend, runtime, worker(), 'explore_execute', project,
                             ['fixture', '--session', source], (attempt, time.monotonic()),
                             source_fact_ids=['origin'])
    return capture, reporter, backend, runtime, source


def eventually(assertion, seconds=4):
    deadline = time.monotonic() + seconds
    while True:
        try:
            assertion()
            return
        except AssertionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(.02)


def assert_record(client, identifier, **expected):
    response = client.get('/agent-sessions/' + identifier)
    assert response.status_code == 200
    data = response.json()
    for key, value in expected.items():
        assert data[key] == value, (key, data)
    return data


def write_source(runtime, source, data):
    path = Path(runtime) / source
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(data)
    return path


def test_actual_local_source_and_stdout_events_are_visible_while_worker_is_alive(client, tmp_path):
    capture, reporter, backend, runtime, source = capture_fixture(client, tmp_path)
    capture.start()
    try:
        eventually(lambda: assert_record(client, capture.id, capture_status='waiting', bytes_captured=0, status='running'))
        raw = session_bytes()
        script = (
            'import json,pathlib,sys,time; '
            'p=pathlib.Path(sys.argv[1]); p.parent.mkdir(exist_ok=True); '
            'p.write_bytes(bytes.fromhex(sys.argv[2])); '
            'print(json.dumps({"type":"tool_execution_start","toolName":"fixture"}),flush=True); '
            'time.sleep(1.5); '
            'p.open("ab").write(b\'{"type":"session_info","name":"Fixture final append"}\\n\'); '
            'print(json.dumps({"type":"agent_end","messages":[]}),flush=True)'
        )
        process = backend.build_exec_process(runtime, {}, [sys.executable, '-c', script, source, raw.hex()], timeout_seconds=5)
        process.stdout_observer = capture.observe_stdout
        process.start()
        eventually(lambda: assert_record(client, capture.id, status='running', capture_status='capturing', bytes_captured=len(raw), events_captured=1))
        assert process._process.poll() is None
        result = process.communicate(5)
        capture.finish(result)
        expected_raw = raw + b'{"type":"session_info","name":"Fixture final append"}\n'
        assert session_service.conversation_file(capture.envelope['invocation']['conversation_id']).read_bytes() == expected_raw
        assert_record(client, capture.id, status='success', capture_status='complete', bytes_captured=len(expected_raw), events_captured=2)
        events = client.get(f'/agent-sessions/{capture.id}/events').json()['items']
        assert [event['event']['type'] for event in events] == ['tool_execution_start', 'agent_end']
        assert all('offset' in payload for path, payload in reporter.calls if path.endswith('/chunks'))
    finally:
        if capture.thread.is_alive():
            capture.finish(ProcessResult(1, '', '', cancelled=True, cancel_reason='fixture teardown'))


def test_common_process_final_flush_survives_immediate_runtime_cleanup(client, tmp_path):
    project = create_project(client)
    backend = LocalBackend(LocalConfig(workspace_root=str(tmp_path / 'work'), completed_action='remove'))
    runtime = backend.ensure_running(project)
    reporter = SessionClient(client)
    conversation = str(uuid4())
    source = f'wayline-sessions/{conversation}.jsonl'
    raw = session_bytes()
    script = (
        'import pathlib,json,sys; p=pathlib.Path(sys.argv[-1]); p.parent.mkdir(exist_ok=True); '
        f'p.write_bytes(bytes.fromhex({raw.hex()!r})); '
        'print(json.dumps({"type":"agent_end","messages":[]}),flush=True)'
    )
    result = run_worker_process(backend, runtime, worker(), [sys.executable, '-c', script, '--session', source],
                                phase='explore_execute', timeout_seconds=5, usage_client=reporter,
                                usage_project_id=project, session_source_fact_ids=['origin'])
    assert result.returncode == 0
    assert backend.cleanup_completed(project) is True and not Path(runtime).exists()
    page = client.get('/agent-sessions', params={'project_id': project}).json()
    assert page['total'] == 1
    metadata = page['items'][0]
    assert metadata['status'] == 'success' and metadata['capture_status'] == 'complete'
    assert session_service.conversation_file(conversation).read_bytes() == raw
    assert metadata['events_captured'] == 1


def test_offline_capture_replays_real_bytes_and_events_after_source_cleanup(client, tmp_path):
    capture, reporter, backend, runtime, source = capture_fixture(client, tmp_path)
    reporter.offline = True
    raw = session_bytes()
    write_source(runtime, source, raw)
    capture.start()
    capture.observe_stdout('{"type":"agent_start"}\n')
    capture.finish(ProcessResult(0, '', ''))
    assert client.get('/agent-sessions').json()['total'] == 0
    assert capture.session_path.read_bytes() == raw
    backend.cleanup_completed(capture.envelope['invocation']['project_id'])
    reporter.offline = False
    flush_sessions(reporter)
    assert_record(client, capture.id, status='success', capture_status='complete', bytes_captured=len(raw), events_captured=1)
    assert session_service.conversation_file(capture.envelope['invocation']['conversation_id']).read_bytes() == raw
    before = client.get('/agent-sessions/' + capture.id).json()
    flush_sessions(reporter)
    after = client.get('/agent-sessions/' + capture.id).json()
    assert after['bytes_captured'] == before['bytes_captured'] and after['events_captured'] == 1


@pytest.mark.parametrize(('result', 'status', 'reason'), [
    (ProcessResult(137, '', '', cancelled=True, cancel_reason='workflow paused'), 'cancelled', 'workflow paused'),
    (ProcessResult(137, '', '', timed_out=True), 'timed_out', None),
    (ProcessResult(3, '', ''), 'failed', None),
])
def test_invocation_terminal_status_and_reason_are_durable(client, tmp_path, result, status, reason):
    capture, reporter, backend, runtime, source = capture_fixture(client, tmp_path)
    write_source(runtime, source, session_bytes())
    capture.start()
    capture.finish(result)
    metadata = assert_record(client, capture.id, status=status, cancel_reason=reason, capture_status='complete')
    assert metadata['finished_at'] is not None
    initial = {**capture.envelope['invocation'], 'status': 'running', 'finished_at': None}
    assert reporter.record_session_invocation(initial).data['status'] == status


def test_outbox_marks_prior_dispatcher_running_invocation_interrupted(client, tmp_path):
    capture, reporter, backend, runtime, source = capture_fixture(client, tmp_path)
    raw = session_bytes()
    write_source(runtime, source, raw)
    capture._copy_source()
    capture.envelope['owner'] = 'previous-dispatcher-instance'
    capture._save()
    flush_sessions(reporter)
    metadata = assert_record(client, capture.id, status='interrupted', capture_status='unavailable', bytes_captured=len(raw))
    assert 'Dispatcher ended' in metadata['source_error']
    assert metadata['finished_at'] is not None


def test_missing_source_waits_while_running_then_finishes_unavailable(client, tmp_path):
    capture, reporter, backend, runtime, source = capture_fixture(client, tmp_path)
    capture.start()
    eventually(lambda: assert_record(client, capture.id, status='running', capture_status='waiting', bytes_captured=0))
    capture.finish(ProcessResult(0, '', ''))
    metadata = assert_record(client, capture.id, status='success', capture_status='unavailable', bytes_captured=0)
    assert metadata['viewer_url'] is None and metadata['source_error']


def test_unsafe_first_source_is_unavailable_immediately_not_waiting(client, tmp_path):
    capture, reporter, backend, runtime, source = capture_fixture(client, tmp_path)
    path = Path(runtime) / source
    path.parent.mkdir()
    private = tmp_path / 'unrelated-private-session'
    private.write_bytes(session_bytes())
    path.symlink_to(private)
    capture.start()
    try:
        eventually(lambda: assert_record(client, capture.id, status='running', capture_status='unavailable', bytes_captured=0))
        assert not capture.session_path.exists()
    finally:
        capture.finish(ProcessResult(1, '', ''))


def test_source_and_event_limits_report_truncation_without_synthesizing_session(client, tmp_path, monkeypatch):
    capture, reporter, backend, runtime, source = capture_fixture(client, tmp_path)
    raw = session_bytes()
    cap = len(raw) - 10
    monkeypatch.setattr(module, 'SESSION_BYTES', cap)
    monkeypatch.setattr(module, 'EVENT_BYTES', 30)
    write_source(runtime, source, raw)
    capture.start()
    capture.observe_stdout('{"type":"agent_start"}\n')
    capture.observe_stdout('{"type":"message_update","text":"too much fixture text"}\n')
    capture.finish(ProcessResult(0, '', ''))
    metadata = assert_record(client, capture.id, status='success', capture_status='truncated', bytes_captured=cap, events_truncated=True)
    assert metadata['source_error'] and capture.session_path.read_bytes() == raw[:cap]
    assert session_service.conversation_file(capture.envelope['invocation']['conversation_id']).read_bytes() == raw[:cap]
    events = client.get(f'/agent-sessions/{capture.id}/events').json()
    assert events['truncated'] is True
    assert [item['event']['type'] for item in events['items']] == ['agent_start']


def test_partial_header_stays_private_until_original_line_is_complete(client, tmp_path):
    capture, reporter, backend, runtime, source = capture_fixture(client, tmp_path)
    raw = session_bytes()
    newline = raw.index(b'\n') + 1
    path = write_source(runtime, source, raw[:20])
    capture._copy_source()
    assert not capture.box.flush(capture.path)
    assert_record(client, capture.id, bytes_captured=0, viewer_url=None)
    with path.open('ab') as output:
        output.write(raw[20:newline])
    capture._copy_source()
    assert capture.box.flush(capture.path)
    metadata = assert_record(client, capture.id, bytes_captured=newline)
    assert metadata['pi_session_id'] == 'actual-pi-header-id'
    assert session_service.conversation_file(capture.envelope['invocation']['conversation_id']).read_bytes() == raw[:newline]


def test_scheduler_replay_does_not_starve_later_records_behind_delivered_history(client, tmp_path, monkeypatch):
    capture, reporter, backend, runtime, source = capture_fixture(client, tmp_path)
    root = capture.box.root
    capture.path.unlink()
    project = capture.envelope['invocation']['project_id']
    now = [0.0]
    monkeypatch.setattr(module.time, 'monotonic', lambda: now[0])
    seen = set()
    original = reporter.record_session_invocation
    def delayed_record(payload):
        now[0] += .8
        seen.add(payload['id'])
        return original(payload)
    reporter.record_session_invocation = delayed_record
    identifiers = []
    for index in range(5):
        identifier = f'00000000-0000-4000-8000-{index:012d}'
        identifiers.append(identifier)
        envelope = {'owner': module.PROCESS_INSTANCE, 'invocation': {
            **capture.envelope['invocation'], 'id': identifier, 'conversation_id': str(uuid4()),
            'status': 'success', 'finished_at': '2026-10-27T10:01:00Z', 'capture_status': 'unavailable'}}
        module._write_json(root / (identifier + '.json'), envelope)
    for _ in range(4):
        flush_sessions(reporter)
    assert seen == set(identifiers), 'Repeated bounded flushes must advance past already delivered historical records'


def test_partial_stdout_tail_is_marked_incomplete_instead_of_silently_lost(client, tmp_path):
    capture, reporter, backend, runtime, source = capture_fixture(client, tmp_path)
    write_source(runtime, source, session_bytes())
    capture.start()
    capture.observe_stdout('{"type":"message_update","text":"partial')
    capture.finish(ProcessResult(137, '', '', cancelled=True, cancel_reason='fixture stop'))
    metadata = assert_record(client, capture.id, status='cancelled')
    assert metadata['events_truncated'] is True
    assert client.get(f'/agent-sessions/{capture.id}/events').json()['items'] == []


def test_source_identity_replacement_preserves_original_bytes_and_fails_closed(client, tmp_path):
    capture, reporter, backend, runtime, source = capture_fixture(client, tmp_path)
    raw = session_bytes()
    path = write_source(runtime, source, raw)
    capture._copy_source()
    assert capture.session_path.read_bytes() == raw
    replacement = raw.replace(b'actual-pi-header-id', b'other--pi-header-id') + b'{"type":"session_info","name":"Replacement"}\n'
    path.write_bytes(replacement)
    capture._copy_source()
    assert capture.envelope['invocation']['capture_status'] == 'unavailable'
    assert 'identity changed' in capture.envelope['invocation']['source_error']
    assert capture.session_path.read_bytes() == raw
    # A later restoration must not turn this invocation into a mixed transcript.
    path.write_bytes(raw + b'{"type":"session_info","name":"Later original"}\n')
    capture._copy_source()
    assert capture.session_path.read_bytes() == raw
    capture.start()
    capture.finish(ProcessResult(0, '', ''))
    metadata = assert_record(client, capture.id, capture_status='unavailable', bytes_captured=len(raw))
    assert metadata['pi_session_id'] == 'actual-pi-header-id'


def test_slow_source_reader_does_not_block_live_stdout_drain(client, tmp_path):
    import threading
    entered, release, observed = threading.Event(), threading.Event(), threading.Event()
    class SlowBackend:
        def read_session_chunk(self, *args):
            entered.set()
            assert release.wait(3)
            return b''
    capture, reporter, backend, runtime, source = capture_fixture(client, tmp_path, backend=SlowBackend(), runtime='fixture-runtime')
    capture.start()
    assert entered.wait(1)
    observer = threading.Thread(target=lambda: (capture.observe_stdout('{"type":"agent_start"}\n'), observed.set()), daemon=True)
    observer.start()
    try:
        assert observed.wait(.25), 'Session source I/O must not hold the stdout observer lock'
    finally:
        release.set()
        observer.join(2)
        capture.finish(ProcessResult(0, '', ''))


def test_quiet_capture_refreshes_heartbeat_without_new_source_or_stdout(client, tmp_path):
    capture, reporter, backend, runtime, source = capture_fixture(client, tmp_path)
    write_source(runtime, source, session_bytes())
    capture._copy_source()
    assert capture.box.flush(capture.path)
    before = assert_record(client, capture.id)
    with capture.lock:
        capture.envelope['invocation']['heartbeat_at'] = '2000-01-01T00:00:00Z'
        capture.next_heartbeat = 0
    capture._copy_source()
    assert capture.box.flush(capture.path)
    after = assert_record(client, capture.id, runtime_status='running', status='running')
    assert after['heartbeat_at'] != '2000-01-01T00:00:00Z'
    assert after['bytes_captured'] == before['bytes_captured'] and after['events_captured'] == 0
    # No restart is needed to distinguish stale metadata from a confirmed run.
    capture.envelope['owner'] = 'prior-dispatcher-instance'
    capture._save()
    flush_sessions(reporter)
    assert_record(client, capture.id, runtime_status='interrupted', status='interrupted')


def test_capture_limit_does_not_stop_heartbeats_for_a_still_running_worker(client, tmp_path):
    capture, reporter, backend, runtime, source = capture_fixture(client, tmp_path)
    capture.envelope['invocation'].update(capture_status='truncated', source_error='Fixture capture limit', heartbeat_at='2000-01-01T00:00:00Z')
    capture.next_heartbeat = 0
    module._write_json(capture.path, capture.envelope)
    assert capture.box.flush(capture.path)
    capture.start()
    try:
        eventually(lambda: assert_record(client, capture.id, status='running', runtime_status='running', capture_status='truncated'))
    finally:
        capture.finish(ProcessResult(0, '', ''))


def test_terminal_capture_delivery_remains_pending_until_final_bytes_are_acked(client, tmp_path):
    capture, reporter, backend, runtime, source = capture_fixture(client, tmp_path)
    target = Path(runtime)/source
    target.parent.mkdir(parents=True)
    raw = session_bytes()
    target.write_bytes(raw)
    capture._copy_source()
    for index in range(75):
        capture.observe_stdout(json.dumps({'type': 'agent_end' if index == 74 else 'message_update', 'index': index})+'\n')
    capture.envelope['invocation'].update(status='success', finished_at=datetime.now(timezone.utc).isoformat(), capture_status='complete')
    capture._save()
    original = reporter.record_session_chunk
    def delay_events(identifier, payload, *, events=False):
        return ApiResult(0) if events else original(identifier, payload, events=events)
    reporter.record_session_chunk = delay_events
    assert capture.box.flush(capture.path) is False
    pending = assert_record(client, capture.id, status='success', capture_status='complete', delivery_status='pending')
    assert pending['bytes_captured'] == len(raw)
    assert pending['events_captured'] == 0
    assert pending['delivery_event_bytes'] == capture.events_path.stat().st_size > 0
    reporter.record_session_chunk = original
    assert capture.box.flush(capture.path) is True
    delivered = assert_record(client, capture.id, delivery_status='delivered', events_captured=75)
    assert delivered['events_bytes'] == delivered['delivery_event_bytes']
    assert delivered['bytes_captured'] == delivered['delivery_session_bytes'] == len(raw)
    first = client.get(f'/agent-sessions/{capture.id}/events?after=0&limit=50').json()
    final = client.get(f'/agent-sessions/{capture.id}/events?after={first["next_after"]}&limit=50').json()
    assert len(first['items']) == 50 and len(final['items']) == 25
    assert final['items'][-1]['event']['type'] == 'agent_end'
    assert final['has_more'] is False
    assert reporter.calls[-1][0] == '/agent-sessions'
    assert reporter.calls[-1][1]['delivery_status'] == 'delivered'
