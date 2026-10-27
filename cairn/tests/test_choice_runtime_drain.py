from __future__ import annotations

from concurrent.futures import Future
from types import SimpleNamespace

import pytest

from cairn.dispatcher.models import RunningTask
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.scheduler.loop import DispatcherLoop
from cairn.server.models import ProjectSummary

from conftest import make_config, make_project


def summary(project_id: str, workflow_id: str, status: str = "active") -> ProjectSummary:
    return ProjectSummary(
        id=project_id, title=project_id, status=status, workflow_id=workflow_id,
        bootstrap_enabled=True, created_at="2026-01-01T00:00:00Z",
        fact_count=2, intent_count=0, working_intent_count=0,
        unclaimed_intent_count=0, hint_count=0,
    )


def loop_for_test(monkeypatch):
    loop = DispatcherLoop.__new__(DispatcherLoop)
    loop.config = make_config()
    loop.config.runtime.max_running_projects = 4
    loop.config.runtime.max_project_workers = 4
    loop.futures = {}
    loop.runtime_project_ids = set()
    loop._cleanup_pending = set()
    loop._log_state = {}
    loop.worker_unhealthy_until = {}
    loop.worker_rejected_until = {}
    loop.container_manager = SimpleNamespace(container_name=lambda runtime_id: runtime_id)
    dispatched = []

    def get_project(project_id):
        project = make_project()
        project.project.id = project_id
        project.project.workflow_id = "shared" if project_id != "unrelated" else "separate"
        return project

    loop.client = SimpleNamespace(get_project=get_project)
    monkeypatch.setattr(loop, "_dispatch_initial_project", lambda project: dispatched.append(project.project.id) or True)
    return loop, dispatched


@pytest.mark.parametrize("old_status", ["stopped", "completed", "deleted"])
def test_choice_fallback_waits_for_canceled_future_to_be_reaped(monkeypatch, old_status):
    loop, dispatched = loop_for_test(monkeypatch)
    old = summary("old_option", "shared")
    fallback = summary("fallback", "shared")
    unrelated = summary("unrelated", "separate")
    future: Future[str] = Future()
    cancellation = TaskCancellation()
    loop.futures[future] = RunningTask(old.id, "explore", "worker", cancellation)
    # Capture the runtime while the old project still exists. Deletion must not
    # lose its grouping before its cancellation has finished.
    loop._cancel_inactive_tasks([old, fallback, unrelated])
    old_snapshot = [] if old_status == "deleted" else [old.model_copy(update={"status": old_status})]
    loop._cancel_inactive_tasks([*old_snapshot, fallback, unrelated])
    assert cancellation.is_cancelled
    assert loop._try_dispatch_project(fallback) is False
    assert loop._try_dispatch_project(unrelated) is True
    assert dispatched == ["unrelated"]

    future.set_result("cancelled")
    # A completed future still occupies the scheduler until its result is reaped.
    assert loop._try_dispatch_project(fallback) is False
    loop._reap_futures()
    assert loop._try_dispatch_project(fallback) is True
    assert dispatched == ["unrelated", "fallback"]
    loop._cancel_inactive_tasks([fallback, unrelated])
    assert "old_option" not in loop._task_runtime_ids


def test_healthy_required_branches_share_a_runtime_in_parallel(monkeypatch):
    loop, dispatched = loop_for_test(monkeypatch)
    milk = summary("milk", "shared")
    honey = summary("honey", "shared")
    future: Future[str] = Future()
    cancellation = TaskCancellation()
    loop.futures[future] = RunningTask(milk.id, "explore", "worker", cancellation)
    loop._cancel_inactive_tasks([milk, honey])
    assert not cancellation.is_cancelled
    assert loop._try_dispatch_project(honey) is True
    assert dispatched == ["honey"]


def test_explicit_resume_still_waits_for_canceled_attempt(monkeypatch):
    loop, dispatched = loop_for_test(monkeypatch)
    selected = summary("selected", "shared")
    future: Future[str] = Future()
    cancellation = TaskCancellation()
    loop.futures[future] = RunningTask(selected.id, "bootstrap", "worker", cancellation)
    loop._cancel_inactive_tasks([selected.model_copy(update={"status": "stopped"})])
    loop._cancel_inactive_tasks([selected])
    assert loop._try_dispatch_project(selected) is False
    future.set_result("cancelled")
    loop._reap_futures()
    assert loop._try_dispatch_project(selected) is True
    assert dispatched == ["selected"]
