from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import threading
import time

from fastapi.testclient import TestClient
import pytest
import requests

from wayline.dispatcher.models import RunningTask
from wayline.dispatcher.protocol.client import ApiResult, WaylineClient
from wayline.dispatcher.runtime.cancellation import TaskCancellation
from wayline.dispatcher.scheduler.loop import DispatcherLoop
from wayline.server import db
from wayline.server.app import app
from wayline.server.models import ProjectSummary
from wayline.server.runtime_models import DispatcherReport, RuntimeLimits, RuntimeOverrides, RuntimeSettingsState

from conftest import make_config


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "runtime.db")
    with TestClient(app) as client:
        yield client


def report(**changes):
    result = {
        "configured": {"max_workers": 2, "max_running_projects": 2, "max_project_workers": 2},
        "effective": {"max_workers": 2, "max_running_projects": 2, "max_project_workers": 2},
        "applied_settings_revision": 0,
        "stale_after_seconds": 60,
        "counts": {"running_tasks": 1, "running_projects": 1, "admitted_projects": 2},
        "workers": [{"name": "worker", "type": "mock", "task_types": ["bootstrap", "reason", "explore"],
                     "max_running": 1, "running": 1, "cooldown": False}],
    }
    result.update(changes)
    return result


def test_runtime_settings_persist_merge_and_reset_without_changing_leases(client, monkeypatch):
    initial = client.get("/runtime").json()
    assert initial == {"scope": "per_dispatcher", "settings": {"max_workers": None, "max_running_projects": None, "max_project_workers": None},
                       "settings_revision": 0, "dispatchers": []}
    leases = client.get("/settings").json()
    first = client.put("/runtime/settings", json={"max_workers": 4}).json()
    assert first["settings"]["max_workers"] == 4 and first["settings_revision"] == 1
    assert client.put("/runtime/settings", json={"max_workers": 4}).json()["settings_revision"] == 1
    client.put("/runtime/settings", json={"max_running_projects": 3})
    reset = client.put("/runtime/settings", json={"max_workers": None}).json()
    assert reset["settings"] == {"max_workers": None, "max_running_projects": 3, "max_project_workers": None}
    assert reset["settings_revision"] == 3
    path = db._db_path
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(path)
    assert client.get("/runtime/settings").json() == {"settings": reset["settings"], "settings_revision": 3}
    assert client.get("/settings").json() == leases


@pytest.mark.parametrize("payload", [
    {}, {"max_workers": 0}, {"max_workers": -1}, {"max_workers": 257},
    {"max_workers": True}, {"max_workers": 1.5}, {"max_workers": "3"}, {"unknown": 3},
])
def test_runtime_override_validation_is_strict_and_bounded(client, payload):
    assert client.put("/runtime/settings", json=payload).status_code == 422
    assert client.get("/runtime/settings").json()["settings_revision"] == 0


def test_runtime_snapshots_show_applied_pending_stale_and_worker_capacity(client):
    assert client.post("/runtime/dispatchers/dispatcher_one", json=report()).status_code == 204
    view = client.get("/runtime").json()["dispatchers"][0]
    assert view["online"] is True and view["pending"] is False
    assert view["counts"] == {"running_tasks": 1, "running_projects": 1, "admitted_projects": 2}
    assert view["worker_capacity"] == 1 and view["effective_worker_capacity"] == 1
    assert view["available_worker_slots"] == 0
    assert view["capacity_by_task_type"] == {"bootstrap": 1, "reason": 1, "explore": 1}
    changed = client.put("/runtime/settings", json={"max_workers": 4}).json()["dispatchers"][0]
    assert changed["pending"] is True
    assert changed["effective"]["max_workers"] == 2 and changed["desired"]["max_workers"] == 4
    applied = report(effective={"max_workers": 4, "max_running_projects": 2, "max_project_workers": 2}, applied_settings_revision=1)
    client.post("/runtime/dispatchers/dispatcher_one", json=applied)
    assert client.get("/runtime").json()["dispatchers"][0]["pending"] is False
    with db.get_conn() as conn:
        conn.execute("UPDATE runtime_dispatchers SET last_seen = ?", ((datetime.now(timezone.utc) - timedelta(seconds=45)).isoformat(),))
    assert client.get("/runtime").json()["dispatchers"][0]["online"] is True
    with db.get_conn() as conn:
        conn.execute("UPDATE runtime_dispatchers SET last_seen = ?", ((datetime.now(timezone.utc) - timedelta(seconds=90)).isoformat(),))
    assert client.get("/runtime").json()["dispatchers"][0]["online"] is False


def test_multiple_dispatchers_apply_caps_independently_and_never_publish_environments(client):
    client.put("/runtime/settings", json={"max_workers": 4})
    for name in ("dispatcher_a", "dispatcher_b"):
        assert client.post(f"/runtime/dispatchers/{name}", json=report()).status_code == 204
    result = client.get("/runtime").json()
    assert result["scope"] == "per_dispatcher" and len(result["dispatchers"]) == 2
    assert all(item["desired"]["max_workers"] == 4 for item in result["dispatchers"])
    unsafe = report()
    unsafe["workers"][0]["env"] = {"API_KEY": "private-secret"}
    assert client.post("/runtime/dispatchers/unsafe", json=unsafe).status_code == 422
    assert "private-secret" not in client.get("/runtime").text


def summary(pid):
    return ProjectSummary(id=pid, title=pid, status="active", bootstrap_enabled=True,
                          created_at="2026-01-01T00:00:00Z", fact_count=2, intent_count=0,
                          working_intent_count=0, unclaimed_intent_count=0, hint_count=0)


def loop_for_test():
    loop = DispatcherLoop.__new__(DispatcherLoop)
    loop.config = make_config()
    loop.executor = ThreadPoolExecutor(max_workers=loop.config.runtime.max_workers)
    loop.cleanup_executor = ThreadPoolExecutor(max_workers=1)
    loop.futures = {}
    loop.cleanup_futures = {}
    loop.reason_checkpoints = {}
    loop.runtime_project_ids = set()
    loop.worker_unhealthy_until = {}
    loop.worker_rejected_until = {}
    loop._log_state = {}
    loop._cleanup_pending = set()
    loop._inactive_cleanup_done = {}
    loop._workflow_cleanup_done = set()
    loop.project_cursor = 0
    loop.dispatcher_id = "fixture_dispatcher"
    return loop


def settings(revision=1, **overrides):
    return RuntimeSettingsState(settings=RuntimeOverrides(**overrides), settings_revision=revision)


def shutdown(loop):
    loop.executor.shutdown(wait=True)
    for executor in getattr(loop, "_retired_executors", []):
        executor.shutdown(wait=True)
    loop.cleanup_executor.shutdown(wait=True)


def test_increase_replaces_executor_and_runs_additional_jobs_without_exceeding_cap(monkeypatch):
    loop = loop_for_test()
    loop.config.runtime.max_running_projects = 4
    loop.config.workers[0].max_running = 4
    release = threading.Event()
    started = {pid: threading.Event() for pid in ("a", "b", "c", "d")}

    def work(pid):
        started[pid].set()
        assert release.wait(5)
        return "success"

    def dispatch(item):
        if any(task.project_id == item.id for task in loop.futures.values()):
            return False
        future = loop.executor.submit(work, item.id)
        loop.futures[future] = RunningTask(item.id, "explore", "test-worker", TaskCancellation())
        loop.runtime_project_ids.add(item.id)
        return True

    monkeypatch.setattr(loop, "_try_dispatch_project", dispatch)
    items = [summary(pid) for pid in started]
    try:
        loop._dispatch_available(items)
        assert len(loop.futures) == 2
        previous = loop.executor
        loop._apply_runtime_settings(settings(max_workers=4))
        assert loop.executor is not previous
        loop._dispatch_available(items)
        assert len(loop.futures) == 4
        assert all(event.wait(2) for event in started.values())
        loop._apply_runtime_settings(settings(2, max_workers=1))
        loop._dispatch_available(items)
        assert len(loop.futures) == 4
        assert all(not task.cancellation.is_cancelled for task in loop.futures.values())
        assert loop.config.runtime.max_workers == 2
    finally:
        release.set()
        shutdown(loop)


def test_lower_project_admission_limit_drains_then_only_dispatches_one_project(monkeypatch):
    loop = loop_for_test()
    loop.config.runtime.max_workers = 10
    loop.config.runtime.max_running_projects = 3
    loop.runtime_project_ids = {"a", "b", "c"}
    for pid in ("a", "b", "c"):
        loop.futures[Future()] = RunningTask(pid, "explore", "test-worker", TaskCancellation())
    dispatched = []
    monkeypatch.setattr(loop, "_try_dispatch_project", lambda item: dispatched.append(item.id) or False)
    try:
        loop._apply_runtime_settings(settings(max_running_projects=1))
        items = [summary(pid) for pid in ("a", "b", "c")]
        loop._dispatch_available(items)
        assert dispatched == [] and len(loop.futures) == 3
        assert all(not task.cancellation.is_cancelled for task in loop.futures.values())
        loop.futures.clear()
        loop._dispatch_available(items)
        assert len(loop.runtime_project_ids) == 1
        assert set(dispatched) == loop.runtime_project_ids
    finally:
        shutdown(loop)


def test_live_global_limit_does_not_override_worker_or_project_limits():
    loop = loop_for_test()
    try:
        loop._apply_runtime_settings(settings(max_workers=4, max_project_workers=1))
        loop.futures[Future()] = RunningTask("a", "explore", "test-worker", TaskCancellation())
        assert loop._select_worker("b", "explore").worker is None
        assert loop._try_dispatch_project(summary("a")) is False
        payload = loop._runtime_report().model_dump()
        assert payload["effective"]["max_workers"] == 4
        assert payload["workers"][0]["max_running"] == 1
        assert payload["counts"]["running_projects"] == 1
        loop.config.workers[0].env["API_KEY"] = "never-publish"
        assert "never-publish" not in loop._runtime_report().model_dump_json()
    finally:
        shutdown(loop)


def test_run_once_polls_controls_and_publishes_safe_effective_status(monkeypatch):
    loop = loop_for_test()

    class Client:
        published = []
        def get_runtime_settings(self):
            return settings(7, max_workers=4, max_running_projects=3)
        def list_projects(self):
            return []
        def publish_runtime(self, dispatcher_id, report):
            self.published.append((dispatcher_id, report))
            return ApiResult(204)
        def close(self):
            pass

    class Backend:
        def close(self):
            pass

    loop.client = Client()
    loop.container_manager = Backend()
    loop._settings_checked = True
    monkeypatch.setattr(loop, "run_startup_healthchecks", lambda: None)
    loop.run(once=True)
    dispatcher_id, published = loop.client.published[0]
    assert dispatcher_id == "fixture_dispatcher"
    assert published.applied_settings_revision == 7
    assert published.configured.max_workers == 2 and published.effective.max_workers == 4
    assert published.counts.running_tasks == 0 and published.counts.running_projects == 0


def test_client_completion_preserves_optional_goals_and_runtime_payloads():
    captured = []

    class Session:
        def request(self, method, url, json, timeout):
            captured.append((method, url, json))
            response = requests.Response()
            response.status_code = 204
            response._content = b""
            response.headers["Content-Type"] = "application/json"
            return response

    client = WaylineClient("http://server")
    client._local.session = Session()
    assert client.complete("proj_1", ["origin"], "Done", "worker").ok
    assert "goals" not in captured[-1][2]
    assert client.complete("proj_1", ["origin"], "Done", "worker", goals=[]).ok
    assert captured[-1][2]["goals"] == []
    assert client.request_expansion_recovery("proj_1", "Missing goals", "worker").ok
    assert captured[-1][1].endswith("/projects/proj_1/expansion-recovery")
