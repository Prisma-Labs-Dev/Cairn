import pytest
from cairn.dispatcher.scheduler import loop as scheduler
from cairn.dispatcher.tasks import common
from cairn.dispatcher.usage_reporting import UsageOutboxOwnershipError
from conftest import make_config


def test_dispatcher_rejects_conflicting_outbox_before_startup_probes(monkeypatch):
    loop = scheduler.DispatcherLoop.__new__(scheduler.DispatcherLoop)
    loop.client = object()
    probes = []
    closed = []
    loop.run_startup_healthchecks = lambda: probes.append(True)
    loop.close = lambda: closed.append(True)
    def reject(_client):
        raise UsageOutboxOwnershipError('Another process owns the usage outbox')
    monkeypatch.setattr(scheduler, 'ensure_usage_owner', reject)
    with pytest.raises(UsageOutboxOwnershipError):
        loop.run(once=True)
    assert probes == []
    assert closed == [True]


def test_worker_never_starts_after_usage_ownership_conflict(monkeypatch):
    calls = []
    backend = type('Backend', (), {'build_exec_process': lambda *_a, **_k: calls.append(True)})()
    def reject(*_args):
        raise UsageOutboxOwnershipError('Another process owns the usage outbox')
    monkeypatch.setattr(common, 'start_attempt', reject)
    with pytest.raises(UsageOutboxOwnershipError):
        common.run_worker_process(backend, 'runtime', make_config().workers[0], ['must-not-run'], phase='review_execute', timeout_seconds=10, usage_client=object(), usage_project_id='project')
    assert calls == []
