from __future__ import annotations

from datetime import datetime, timezone
import json
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from cairn.dispatcher.config import WorkerConfig
from cairn.dispatcher.protocol.client import ApiResult
from cairn.dispatcher.usage_parsers import parse_usage
from cairn.dispatcher.usage_reporting import UsageOutbox, finish_attempt, start_attempt
from cairn.server import db
from cairn.server.app import app
from cairn.server.usage_models import UsageAttempt
from cairn.server.usage_service import usage_summary


def worker(kind='codex', model=None):
    return WorkerConfig(name='test-worker', type=kind, model=model, task_types=['reason'], max_running=1, priority=0)


def stream(*events):
    return '\n'.join(json.dumps(event) for event in events)


def event(**changes):
    return UsageAttempt.model_validate({
        'attempt_id': str(uuid4()), 'project_id': 'proj_001', 'worker_name': 'w',
        'worker_type': 'codex', 'phase': 'reason_execute', 'status': 'success',
        'started_at': '2026-10-27T10:00:00Z', 'finished_at': '2026-10-27T10:00:01Z',
        'duration_ms': 1000, 'readings': [{'model': 'test-model', 'model_source': 'reported',
            'input_tokens': 100, 'cached_input_tokens': 80, 'uncached_input_tokens': 20,
            'output_tokens': 5}], **changes,
    })


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, '_db_path', None)
    db.configure(tmp_path / 'usage.db')
    monkeypatch.setenv('CAIRN_USAGE_OUTBOX', str(tmp_path / 'outbox.db'))
    with TestClient(app) as http:
        yield http


def test_codex_counts_only_new_turn_usage_and_preserves_agent_result():
    from cairn.dispatcher.workers.adapters.codex import CodexDriver
    text = stream(
        {'type': 'thread.started', 'thread_id': 'thread-123'},
        {'type': 'turn.started'},
        {'type': 'token_count', 'usage': {'input_tokens': 999999}},
        {'type': 'turn.completed', 'usage': {'input_tokens': 100, 'cached_input_tokens': 80, 'output_tokens': 5}},
        {'type': 'turn.completed', 'usage': {'input_tokens': 100, 'cached_input_tokens': 80, 'output_tokens': 5}},
        {'type': 'turn.started'},
        {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': '{"complete":true}'}},
        {'type': 'turn.completed', 'usage': {'input_tokens': 50, 'cached_input_tokens': 20, 'output_tokens': 3}},
    )
    parsed = parse_usage(worker(model='configured'), text, resumed=True)
    row = parsed.readings[0]
    assert (row.input_tokens, row.cached_input_tokens, row.uncached_input_tokens, row.output_tokens) == (150, 100, 50, 8)
    assert row.cache_write_input_tokens is None
    assert row.model_source == 'configured'
    driver = CodexDriver()
    assert driver.extract_response_text(text, '') == '{"complete":true}'
    assert driver.extract_session(None, text, '') == 'thread-123'
    assert driver.extract_response_text(stream({'type': 'turn.completed', 'usage': {'input_tokens': 9}}), '') == ''


def test_missing_codex_cache_is_unknown_not_zero():
    parsed = parse_usage(worker(), stream({'type': 'turn.completed', 'usage': {'input_tokens': 10, 'output_tokens': 0}}))
    row = parsed.readings[0]
    assert row.model is None and row.model_source == 'unknown'
    assert row.cached_input_tokens is None and row.uncached_input_tokens is None
    assert row.output_tokens == 0
    assert event(readings=parsed.readings).coverage == 'partial'


def test_pi_final_message_deduplicates_wrappers_and_marks_error_zero_unknown():
    message = {'role': 'assistant', 'responseId': 'response-1', 'model': 'configured-alias', 'responseModel': 'actual-model',
        'usage': {'input': 10, 'cacheRead': 40, 'cacheWrite': 5, 'output': 3, 'totalTokens': 58}, 'stopReason': 'stop'}
    text = stream({'type': 'session', 'id': 'old-session'}, {'type': 'message_end', 'message': message},
                  {'type': 'message_end', 'message': message}, {'type': 'turn_end', 'message': message},
                  {'type': 'agent_end', 'messages': [message]})
    row = parse_usage(worker('pi'), text).readings[0]
    assert (row.input_tokens, row.cached_input_tokens, row.uncached_input_tokens, row.cache_write_input_tokens, row.output_tokens) == (55, 40, 15, 5, 3)
    assert row.model == 'actual-model' and row.model_source == 'reported'
    message.update(stopReason='aborted', usage={'input': 0, 'cacheRead': 0, 'cacheWrite': 0, 'output': 0})
    parsed = parse_usage(worker('pi'), stream({'type': 'message_end', 'message': message}))
    assert parsed.readings[0].input_tokens is None and 'pi_error_zero_usage' in parsed.notes


def claude_text(models, *, version='2.1.310', usage=None, session='session-1', error=False):
    return stream({'type': 'system', 'subtype': 'init', 'claude_code_version': version, 'session_id': session, 'model': 'main'},
                  {'type': 'result', 'session_id': session, 'modelUsage': models, 'usage': usage, 'is_error': error, 'result': '{"complete":true}'})


def claude_counts(fresh=10, cached=40, writes=5, output=3):
    return {'inputTokens': fresh, 'cacheReadInputTokens': cached, 'cacheCreationInputTokens': writes, 'outputTokens': output}


def test_claude_model_totals_include_subagents_and_cumulative_resume_deltas():
    text = claude_text({'main': claude_counts(), 'subagent': claude_counts(2, 3, 0, 1)})
    first = parse_usage(worker('claudecode'), text)
    assert len(first.readings) == 2
    assert first.readings[0].input_tokens == 55 and first.readings[0].uncached_input_tokens == 15
    resumed = parse_usage(worker('claudecode'), claude_text({'main': claude_counts(20, 90, 6, 9), 'subagent': claude_counts(2, 3, 0, 1)}), resumed=True, baseline=first.snapshot)
    main, sub = resumed.readings
    assert (main.input_tokens, main.cached_input_tokens, main.uncached_input_tokens, main.output_tokens) == (61, 50, 11, 6)
    assert sub.input_tokens == 0 and sub.output_tokens == 0
    assert 'session_delta_applied' in resumed.notes
    assert event(worker_type='claudecode', readings=resumed.readings, notes=resumed.notes).coverage == 'reported'


def test_claude_missing_resume_baseline_never_counts_old_session_total():
    usage = {'input_tokens': 4, 'cache_read_input_tokens': 8, 'cache_creation_input_tokens': 0, 'output_tokens': 2}
    parsed = parse_usage(worker('claudecode'), claude_text({'main': claude_counts(999, 999, 999, 999)}, usage=usage), resumed=True)
    assert parsed.readings[0].input_tokens == 12
    assert 'claude_resume_baseline_missing' in parsed.notes
    assert 'claude_main_loop_only' in parsed.notes
    assert event(readings=parsed.readings, notes=parsed.notes).coverage == 'partial'
    assert parsed.snapshot['main']['input_tokens'] == 2997
    old = parse_usage(worker('claudecode'), claude_text({'main': claude_counts()}, version='2.1.200'), resumed=True)
    assert old.readings[0].input_tokens == 55 and not old.notes
    unknown = parse_usage(worker('claudecode'), claude_text({'main': claude_counts(999)}, version='unknown', usage=usage), resumed=True)
    assert unknown.readings[0].input_tokens == 12 and unknown.snapshot is None


def test_claude_partial_stream_input_ids_once_output_placeholders_unknown():
    message = {'id': 'msg-1', 'model': 'reported', 'usage': {'input_tokens': 2, 'cache_read_input_tokens': 3, 'cache_creation_input_tokens': 0, 'output_tokens': 1}}
    parsed = parse_usage(worker('claudecode'), stream({'type': 'assistant', 'message': message}, {'type': 'assistant', 'message': message}))
    assert parsed.readings[0].input_tokens == 5 and parsed.readings[0].output_tokens is None
    assert 'partial_cli_stream' in parsed.notes


@pytest.mark.parametrize('kind', ['codex', 'pi', 'claudecode', 'mock'])
def test_agent_content_is_not_usage_and_mock_never_claims_real_usage(kind):
    text = '{"complete": true, "usage": {"input_tokens": 100}}'
    assert parse_usage(worker(kind), text).readings[0].input_tokens is None


def test_usage_api_unknown_partial_mixed_models_and_exactly_once(client):
    first = event()
    assert client.post('/usage/attempts', json=first.model_dump(mode='json')).json()['duplicate'] is False
    assert client.post('/usage/attempts', json=first.model_dump(mode='json')).json()['duplicate'] is True
    changed = first.model_dump(mode='json'); changed['readings'][0]['output_tokens'] = 6
    assert client.post('/usage/attempts', json=changed).status_code == 409
    unknown = event(project_id='proj_002', phase='explore_conclude', readings=[{'model': None, 'model_source': 'unknown'}])
    partial = event(phase='bootstrap_execute', readings=[{'model': 'second', 'model_source': 'configured', 'output_tokens': 0}])
    mixed = event(worker_type='claudecode', readings=[{'model': 'main', 'model_source': 'reported', 'input_tokens': 5, 'cached_input_tokens': 1, 'uncached_input_tokens': 4, 'output_tokens': 2}, {'model': 'sub', 'model_source': 'reported', 'input_tokens': 10, 'cached_input_tokens': 4, 'uncached_input_tokens': 6, 'output_tokens': 3}])
    for item in (unknown, partial, mixed):
        assert client.post('/usage/attempts', json=item.model_dump(mode='json')).status_code == 200
    summary = client.get('/usage/summary').json()
    assert summary['totals']['attempts'] == 4
    assert summary['totals']['reported_attempts'] == 2
    assert summary['totals']['partial_attempts'] == 1
    assert summary['totals']['unreported_attempts'] == 1
    assert summary['totals']['input_tokens'] == 115
    assert summary['totals']['output_tokens'] == 10
    assert summary['totals']['cache_write_input_tokens'] is None
    assert summary['totals']['metric_reports']['input_tokens'] == 3
    assert summary['phase_attempts'] == {'reason_execute': 2, 'explore_conclude': 1, 'bootstrap_execute': 1}
    assert next(row for row in summary['models'] if row['model'] is None)['input_tokens'] is None
    page = client.get('/usage/attempts?limit=2').json()
    assert page['total'] == 4 and page['next_offset'] == 2
    assert client.get('/usage/attempts?limit=2&offset=2').json()['next_offset'] is None
    assert client.get('/usage/summary?project_id=proj_002').json()['totals']['attempts'] == 1
    assert client.get('/usage/summary?since=2026-10-27T10:00:00Z&until=2026-10-27T10:00:01Z').json()['totals']['attempts'] == 4
    assert client.get('/usage/summary?until=2026-10-27T10:00:00Z').json()['totals']['attempts'] == 0
    assert client.get('/usage/summary?since=2026-10-27T10:00:00').status_code == 422
    assert client.get('/usage/summary?since=2026-10-28T00:00:00Z&until=2026-10-27T00:00:00Z').status_code == 422
    # Aggregation must never deserialize raw ledger payloads.
    import sqlite3
    with db.get_conn() as conn:
        conn.set_authorizer(lambda action, table, column, *_: sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_READ and column == 'payload_json' else sqlite3.SQLITE_OK)
        assert usage_summary(conn)['totals']['attempts'] == 4
    db.configure(db._db_path)
    assert client.get('/usage/summary').json()['totals']['attempts'] == 4


@pytest.mark.parametrize('change', [
    {'stdout': 'SECRET'}, {'notes': ['raw error with secret']},
    {'readings': [{'model': None, 'model_source': 'reported'}]},
    {'readings': [{'model': 'x', 'model_source': 'reported', 'input_tokens': -1}]},
    {'readings': [{'model': 'x', 'model_source': 'reported', 'input_tokens': True}]},
    {'readings': [{'model': 'x', 'model_source': 'reported', 'input_tokens': 10, 'cached_input_tokens': 9, 'uncached_input_tokens': 9}]},
])
def test_usage_api_rejects_raw_or_inconsistent_data(client, change):
    body = event().model_dump(mode='json'); body.update(change)
    assert client.post('/usage/attempts', json=body).status_code == 422


class Reporter:
    _base_url = 'test-server'
    def __init__(self, http=None):
        self.http = http; self.sent = []; self.fail = False
    def record_usage_attempt(self, body):
        self.sent.append(body)
        if self.http is not None:
            response = self.http.post('/usage/attempts', json=body)
            return ApiResult(0 if self.fail else response.status_code, response.json())
        return ApiResult(0 if self.fail else 200, {})


def test_outbox_replays_original_id_after_lost_ack_and_restart(client, tmp_path):
    box = UsageOutbox('server', tmp_path / 'pending.db')
    item = event()
    box.begin(item)
    box.finish(item, worker(), stream({'type': 'turn.completed', 'usage': {'input_tokens': 3, 'cached_input_tokens': 2, 'output_tokens': 1}}), False)
    reporter = Reporter(client); reporter.fail = True
    box.flush(reporter)
    assert client.get('/usage/summary').json()['totals']['attempts'] == 1
    reporter.fail = False
    UsageOutbox('server', box.path).flush(reporter)
    assert len(reporter.sent) == 2 and reporter.sent[0] == reporter.sent[1]
    assert client.get('/usage/summary').json()['totals']['attempts'] == 1
    with box.connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM pending_usage').fetchone()[0] == 0


def test_outbox_keeps_atomic_claude_baseline_across_restart(tmp_path):
    path = tmp_path / 'usage.db'
    first = event(worker_type='claudecode')
    box = UsageOutbox('s', path); box.begin(first)
    box.finish(first, worker('claudecode'), claude_text({'main': claude_counts()}), False)
    second = event(worker_type='claudecode'); box = UsageOutbox('s', path); box.begin(second)
    result = box.finish(second, worker('claudecode'), claude_text({'main': claude_counts(12, 45, 5, 7)}), True)
    assert result.readings[0].input_tokens == 7 and result.readings[0].output_tokens == 4
    assert result.coverage == 'reported'
    with box.connect() as conn:
        stored = conn.execute('SELECT payload FROM pending_usage WHERE attempt_id=?', (str(second.attempt_id),)).fetchone()[0]
        assert 'session-1' not in stored and 'modelUsage' not in stored


def test_interrupted_attempt_is_explicit_unreported_not_fake_zero(tmp_path, monkeypatch):
    box = UsageOutbox('s', tmp_path / 'outbox.db'); item = event(finished_at=None, duration_ms=None, readings=[{'model': None, 'model_source': 'unknown'}])
    box.begin(item)
    with box.connect() as conn:
        conn.execute("UPDATE pending_usage SET owner_instance='prior-dispatcher'")
    reporter = Reporter(); box.flush(reporter)
    assert reporter.sent[0]['finished_at'] is None
    assert reporter.sent[0]['readings'][0]['input_tokens'] is None
    assert reporter.sent[0]['notes'] == ['dispatcher_interrupted']


@pytest.mark.parametrize(('code','flags','expected'), [(0,{},'success'), (1,{},'failed'), (124,{},'timed_out'), (0,{'cancelled': True},'cancelled')])
def test_invocation_records_actual_usage_even_on_failed_or_cancelled_result(client, code, flags, expected):
    reporter = Reporter(client); w = worker()
    context = start_attempt(reporter, 'proj_001', w, 'reason_execute')
    text = stream({'type': 'turn.completed', 'usage': {'input_tokens': 3, 'cached_input_tokens': 2, 'output_tokens': 1}})
    result = SimpleNamespace(returncode=code, stdout=text, cancelled=False, timed_out=False, **({} if not flags else {}))
    for key, value in flags.items(): setattr(result, key, value)
    finish_attempt(context, reporter, w, [], result)
    stored = client.get('/usage/attempts').json()['items'][0]
    assert stored['status'] == expected and stored['readings'][0]['input_tokens'] == 3
    assert stored['coverage'] == ('reported' if expected == 'success' else 'partial')
    assert result.stdout == text


def test_real_process_all_phases_and_invalid_output_are_counted(client, tmp_path):
    import sys
    from cairn.dispatcher.config import LocalConfig
    from cairn.dispatcher.runtime.local_backend import LocalBackend
    from cairn.dispatcher.tasks.common import run_worker_process
    backend = LocalBackend(LocalConfig(workspace_root=str(tmp_path / 'runs')))
    workdir = backend.ensure_running('proj_001')
    reporter = Reporter(client)
    for phase in ('bootstrap', 'bootstrap_conclude', 'reason_execute', 'explore_execute', 'explore_conclude', 'reason_execute'):
        # Deliberately invalid agent result still has real CLI-shaped token metrics.
        text = stream({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'not-valid-agent-json'}},
                      {'type': 'turn.completed', 'usage': {'input_tokens': 7, 'cached_input_tokens': 5, 'output_tokens': 2}})
        result = run_worker_process(backend, workdir, worker(), [sys.executable, '-c', 'import sys;print(sys.argv[1])', text],
            phase=phase, timeout_seconds=2, usage_client=reporter, usage_project_id='proj_001')
        assert result.returncode == 0 and result.stdout.strip() == text
    summary = client.get('/usage/summary').json()
    assert summary['totals']['attempts'] == 6 and summary['totals']['input_tokens'] == 42
    assert summary['phase_attempts']['reason_execute'] == 2
    ids = [row['attempt_id'] for row in client.get('/usage/attempts').json()['items']]
    assert len(set(ids)) == 6
    # Prompt/output aren't persisted anywhere in the ledger/outbox.
    with db.get_conn() as conn:
        assert 'not-valid-agent-json' not in '\n'.join(row[0] for row in conn.execute('SELECT payload_json FROM usage_attempts'))


def test_start_failure_and_reporting_failure_preserve_task_behavior(client, tmp_path):
    import sys
    from cairn.dispatcher.config import LocalConfig
    from cairn.dispatcher.runtime.local_backend import LocalBackend
    from cairn.dispatcher.tasks.common import run_worker_process
    backend = LocalBackend(LocalConfig(workspace_root=str(tmp_path / 'runs')))
    workdir = backend.ensure_running('proj_001')
    reporter = Reporter(client)
    with pytest.raises(FileNotFoundError):
        run_worker_process(backend, workdir, worker(), ['/missing-cairn-test-binary'], phase='reason_execute',
            timeout_seconds=2, usage_client=reporter, usage_project_id='proj_001')
    record = client.get('/usage/attempts').json()['items'][0]
    assert record['status'] == 'start_failed' and record['coverage'] == 'unreported'
    reporter.fail = True
    result = run_worker_process(backend, workdir, worker(), [sys.executable, '-c', 'print("result unaffected")'], phase='reason_execute',
        timeout_seconds=2, usage_client=reporter, usage_project_id='proj_001')
    assert result.stdout.strip() == 'result unaffected' and result.returncode == 0
    # Lost acknowledgement is retried using the exact same invocation ID.
    reporter.fail = False
    from cairn.dispatcher.usage_reporting import flush_usage
    flush_usage(reporter)
    assert client.get('/usage/summary').json()['totals']['attempts'] == 2


def test_outbox_health_exposes_pending_rejected_and_unknown_reporters(client, tmp_path):
    class HealthReporter(Reporter):
        def record_usage_reporter(self, body):
            response = self.http.post('/usage/reporters', json=body)
            return ApiResult(response.status_code, response.json())
    assert client.get('/usage/summary').json()['reporting']['reporters'] == []
    box = UsageOutbox('s', tmp_path / 'queue.db')
    box.enqueue(event())
    running = event(); box.begin(running)
    rejected = event(); box.enqueue(rejected)
    with box.connect() as conn:
        conn.execute("UPDATE pending_usage SET state='rejected' WHERE attempt_id=?", (str(rejected.attempt_id),))
    box.publish_status(HealthReporter(client))
    row = client.get('/usage/summary?project_id=unrelated').json()['reporting']['reporters'][0]
    assert row['pending_attempts'] == row['running_attempts'] == row['rejected_attempts'] == 1
    assert len(row['reporter_id']) == 64 and row['last_seen']
    assert str(tmp_path) not in json.dumps(row)


def test_rejected_event_is_retained_without_infinite_retries(tmp_path):
    class Rejecting(Reporter):
        def record_usage_attempt(self, body):
            self.sent.append(body)
            return ApiResult(422, {})
    box = UsageOutbox('s', tmp_path / 'queue.db'); box.enqueue(event())
    reporter = Rejecting(); box.flush(reporter); box.flush(reporter)
    assert len(reporter.sent) == 1
    with box.connect() as conn:
        assert conn.execute('SELECT state FROM pending_usage').fetchone()[0] == 'rejected'


def test_claude_driver_stream_metadata_never_becomes_agent_result():
    from cairn.dispatcher.workers.adapters.claudecode import ClaudeCodeDriver
    driver = ClaudeCodeDriver()
    assert driver.extract_response_text(claude_text({'main': claude_counts()}), '') == '{"complete":true}'
    assert driver.extract_response_text(stream({'type': 'system', 'subtype': 'init', 'session_id': 's'}), '') == ''
    for argv in (driver.build_execute(worker('claudecode'), 'prompt', 's').argv, driver.build_conclude(worker('claudecode'), 'prompt', 's')):
        assert argv[argv.index('--output-format') + 1] == 'stream-json' and '--verbose' in argv


def test_local_host_default_is_not_mislabeled_as_container_env_model():
    from cairn.dispatcher.usage_reporting import invocation_worker
    for kind, key in (('codex', 'CODEX_MODEL'), ('pi', 'PI_MODEL')):
        configured = worker(kind, model='not-used-by-local-cli' if kind == 'codex' else None).model_copy(update={'env': {key: 'container-only'}})
        actual = invocation_worker(configured, [kind, 'exec', '--', 'prompt'])
        assert parse_usage(actual, '').readings[0].model is None
        selected = invocation_worker(configured, [kind, 'exec', '--model', 'selected', '--', 'prompt'])
        assert parse_usage(selected, '').readings[0].model == 'selected'


def test_outbox_restart_with_reused_pid_does_not_hide_interrupted_attempt(tmp_path):
    box = UsageOutbox('s', tmp_path / 'queue.db')
    item = event(finished_at=None, duration_ms=None, readings=[{'model': None, 'model_source': 'unknown'}]); box.begin(item)
    with box.connect() as conn:
        conn.execute("UPDATE pending_usage SET owner_instance='old-dispatcher-instance'")
        reporter_before = conn.execute('SELECT reporter_id FROM usage_outbox_meta').fetchone()[0]
    reporter = Reporter(); reopened = UsageOutbox('s', box.path); reopened.flush(reporter)
    assert reporter.sent[0]['notes'] == ['dispatcher_interrupted']
    with reopened.connect() as conn:
        assert conn.execute('SELECT reporter_id FROM usage_outbox_meta').fetchone()[0] == reporter_before


def test_effort_records_effective_invocation_selection():
    from cairn.dispatcher.usage_reporting import invocation_worker
    selected = invocation_worker(worker(), ['codex', 'exec', '-c', 'model_reasoning_effort="high"', '--', 'prompt'])
    assert selected.reasoning_effort == 'high'


def test_claude_reset_error_never_seeds_new_session_with_old_counters():
    text = claude_text({'main': claude_counts()}, session='old') + '\n' + stream(
        {'type': 'result', 'session_id': 'new', 'is_error': True, 'modelUsage': {'main': claude_counts(0, 0, 0, 0)}})
    parsed = parse_usage(worker('claudecode'), text)
    assert 'claude_session_resets_unaccounted' in parsed.notes
    assert parsed.snapshot is None
    assert event(readings=parsed.readings, notes=parsed.notes).coverage == 'partial'


OWNER_PROCESS = r'''
import json, pathlib, sys
from cairn.dispatcher import usage_reporting as reporting
from cairn.dispatcher.config import WorkerConfig
from cairn.server.usage_models import UsageAttempt
# Simulate independent containers that both observe themselves as PID1.
reporting.os.getpid = lambda: 1
box = reporting.UsageOutbox('shared-server', pathlib.Path(sys.argv[1]))
attempt = UsageAttempt.model_validate(json.loads(sys.argv[2]))
box.begin(attempt)
print('ready', flush=True)
if sys.stdin.readline().strip() == 'finish':
    worker = WorkerConfig(name='w', type='codex', task_types=['reason'], max_running=1, priority=0)
    box.finish(attempt, worker, json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 3, 'cached_input_tokens': 2, 'output_tokens': 1}}), False)
    print('finished', flush=True)
'''


def owner_process(path, item):
    import subprocess
    import sys
    process = subprocess.Popen([sys.executable, '-c', OWNER_PROCESS, str(path), item.model_dump_json()],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert process.stdout.readline().strip() == 'ready', process.stderr.read()
    return process


def test_independent_same_pid_peer_fails_before_worker_and_preserves_actual_finish(client, tmp_path):
    import os
    import sqlite3
    import subprocess
    import sys
    path = tmp_path / 'shared.db'
    item = event(finished_at=None, duration_ms=None, readings=[{'model': None, 'model_source': 'unknown'}])
    owner = owner_process(path, item)
    peer = r'''
import pathlib, sys
from cairn.dispatcher import usage_reporting as reporting
from cairn.dispatcher.config import WorkerConfig
from cairn.dispatcher.tasks.common import run_worker_process
from cairn.dispatcher.scheduler.loop import DispatcherLoop
reporting.os.getpid = lambda: 1
class Reporter:
    def record_usage_attempt(self, body):
        pathlib.Path(sys.argv[1]).write_text('poisoned telemetry')
class Backend:
    def build_exec_process(self, *args, **kwargs):
        pathlib.Path(sys.argv[1]).write_text('worker started')
worker = WorkerConfig(name='w', type='codex', task_types=['reason'], max_running=1, priority=0)
loop = DispatcherLoop.__new__(DispatcherLoop)
loop.client = Reporter()
loop.run_startup_healthchecks = lambda: pathlib.Path(sys.argv[1]).write_text('health probe started')
loop.close = lambda: None
try:
    loop.run(once=True)
except reporting.UsageOutboxOwnershipError:
    pass
else:
    sys.exit(9)
try:
    run_worker_process(Backend(), 'unused', worker, ['unused'], phase='reason_execute', timeout_seconds=1,
                       usage_client=Reporter(), usage_project_id='proj_001')
except reporting.UsageOutboxOwnershipError as error:
    print(str(error))
    sys.exit(7)
sys.exit(8)
'''
    sentinel = tmp_path / 'unexpected-worker-or-report'
    try:
        denied = subprocess.run([sys.executable, '-c', peer, str(sentinel)],
            env={**os.environ, 'CAIRN_USAGE_OUTBOX': str(path)}, capture_output=True, text=True, timeout=10)
        assert denied.returncode == 7, denied.stdout + denied.stderr
        assert 'already owned' in denied.stdout and 'CAIRN_USAGE_OUTBOX' in denied.stdout
        assert not sentinel.exists()
        with sqlite3.connect(path) as conn:
            state, payload = conn.execute('SELECT state,payload FROM pending_usage').fetchone()
        assert state == 'running' and 'dispatcher_interrupted' not in payload
        owner.stdin.write('finish\n'); owner.stdin.flush()
        assert owner.stdout.readline().strip() == 'finished'
        assert owner.wait(timeout=10) == 0
        # A new owner can now replay the real final event once. No peer wrote an
        # unreported event with this UUID, so no conflict/quarantine occurs.
        box = UsageOutbox('shared-server', path); box.flush(Reporter(client))
        record = client.get('/usage/attempts').json()['items'][0]
        assert record['attempt_id'] == str(item.attempt_id)
        assert record['coverage'] == 'reported' and record['readings'][0]['input_tokens'] == 3
        assert 'dispatcher_interrupted' not in record['notes']
        with box.connect() as conn:
            assert conn.execute('SELECT COUNT(*) FROM pending_usage').fetchone()[0] == 0
    finally:
        if owner.poll() is None:
            owner.kill(); owner.wait(timeout=10)


def test_crashed_owner_os_lock_release_allows_restart_recovery(client, tmp_path):
    path = tmp_path / 'crash.db'
    item = event(finished_at=None, duration_ms=None, readings=[{'model': None, 'model_source': 'unknown'}])
    owner = owner_process(path, item)
    owner.kill(); owner.wait(timeout=10)
    box = UsageOutbox('shared-server', path)
    box.flush(Reporter(client))
    record = client.get('/usage/attempts').json()['items'][0]
    assert record['attempt_id'] == str(item.attempt_id) and record['coverage'] == 'unreported'
    assert record['finished_at'] is None and record['notes'] == ['dispatcher_interrupted']


def test_same_process_helpers_share_ownership_across_threads_and_path_aliases(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    directory = tmp_path / 'real'; directory.mkdir()
    (tmp_path / 'alias').symlink_to(directory, target_is_directory=True)
    paths = [directory / 'queue.db', tmp_path / 'alias' / 'queue.db'] * 8
    with ThreadPoolExecutor(max_workers=8) as executor:
        boxes = list(executor.map(lambda path: UsageOutbox('s', path), paths))
    assert len({box.path for box in boxes}) == 1
    with boxes[0].connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM usage_outbox_meta').fetchone()[0] == 1


def test_eight_cold_helpers_initialize_record_and_report_through_shared_owner(client, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    directory = tmp_path/'cold-owner'; directory.mkdir()
    (tmp_path/'cold-alias').symlink_to(directory, target_is_directory=True)
    start = threading.Barrier(8)
    reporter = Reporter(client)
    reports = []
    reporter.record_usage_reporter = lambda body: reports.append(body) or ApiResult(200, {})
    attempts = [event() for _ in range(8)]
    def record(index):
        start.wait(timeout=5)
        root = directory if index % 2 == 0 else tmp_path/'cold-alias'
        box = UsageOutbox('cold-server', root/'usage.db')
        box.begin(attempts[index])
        box.enqueue(attempts[index])
        box.flush(reporter, limit=100)
        box.publish_status(reporter)
        return box.path
    with ThreadPoolExecutor(max_workers=8) as executor:
        paths = list(executor.map(record, range(8)))
    assert len(set(paths)) == 1
    box = UsageOutbox('cold-server', paths[0])
    box.flush(reporter, limit=100)
    summary = client.get('/usage/summary').json()
    assert summary['totals']['attempts'] == 8
    assert summary['totals']['input_tokens'] == 800
    assert summary['totals']['cached_input_tokens'] == 640
    assert summary['totals']['uncached_input_tokens'] == 160
    assert {item['attempt_id'] for item in reporter.sent} == {str(item.attempt_id) for item in attempts}
    assert len(reports) == 8 and len({report['reporter_id'] for report in reports}) == 1
    with box.connect() as conn:
        assert conn.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
        assert conn.execute('SELECT COUNT(*) FROM usage_outbox_meta').fetchone()[0] == 1
        assert conn.execute('SELECT COUNT(*) FROM pending_usage').fetchone()[0] == 0
