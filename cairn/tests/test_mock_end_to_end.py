from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
import pathlib
import re
import subprocess
import sys
import threading
from typing import Any

from fastapi.testclient import TestClient
from pydantic import TypeAdapter
import pytest

from cairn.dispatcher.config import DispatchConfig
from cairn.dispatcher.models import ReasonCheckpoint
from cairn.dispatcher.protocol.client import ApiResult
from cairn.dispatcher.runtime.process import ProcessResult
from cairn.dispatcher.runtime.local_backend import LocalBackend
from cairn.dispatcher.config import LocalConfig
from cairn.dispatcher.workers.base import DriverResult
from cairn.dispatcher.workers.adapters.mock import MockDriver
from cairn.dispatcher.scheduler.loop import DispatcherLoop
from cairn.server import db
from cairn.server.app import app
from cairn.server.models import ProjectDetail, ProjectSummary, Settings


class InProcessClient:
    def __init__(self, http: TestClient):
        self.http = http
        self._summaries = TypeAdapter(list[ProjectSummary])

    def close(self) -> None:
        return None

    def list_projects(self) -> list[ProjectSummary]:
        response = self.http.get("/projects")
        response.raise_for_status()
        return self._summaries.validate_python(response.json())

    def get_project(self, project_id: str) -> ProjectDetail:
        response = self.http.get(f"/projects/{project_id}")
        response.raise_for_status()
        return ProjectDetail.model_validate(response.json())

    def get_settings(self) -> Settings:
        response = self.http.get("/settings")
        response.raise_for_status()
        return Settings.model_validate(response.json())

    def export_project(self, project_id: str) -> str:
        response = self.http.get(f"/projects/{project_id}/export?format=yaml")
        response.raise_for_status()
        return response.text

    def heartbeat(self, project_id: str, intent_id: str, worker: str) -> ApiResult:
        return self._post(f"/projects/{project_id}/intents/{intent_id}/heartbeat", {"worker": worker})

    def claim_reason(self, project_id: str, worker: str, trigger: str) -> ApiResult:
        return self._post(f"/projects/{project_id}/reason/claim", {"worker": worker, "trigger": trigger})

    def reason_heartbeat(self, project_id: str, worker: str) -> ApiResult:
        return self._post(f"/projects/{project_id}/reason/heartbeat", {"worker": worker})

    def release_reason(self, project_id: str, worker: str) -> ApiResult:
        return self._post(f"/projects/{project_id}/reason/release", {"worker": worker})

    def release(self, project_id: str, intent_id: str, worker: str) -> ApiResult:
        return self._post(f"/projects/{project_id}/intents/{intent_id}/release", {"worker": worker})

    def conclude(self, project_id: str, intent_id: str, worker: str, description: str) -> ApiResult:
        return self._post(
            f"/projects/{project_id}/intents/{intent_id}/conclude",
            {"worker": worker, "description": description},
        )

    def complete(self, project_id: str, from_ids: list[str], description: str, worker: str, goals=None) -> ApiResult:
        payload = {"from": from_ids, "description": description, "worker": worker}
        if goals is not None:
            payload["goals"] = goals
        return self._post(
            f"/projects/{project_id}/complete",
            payload,
        )

    def request_expansion_recovery(self, project_id: str, description: str, worker: str) -> ApiResult:
        return self._post(f"/projects/{project_id}/expansion-recovery", {"description": description, "worker": worker})

    def create_intent(self, project_id: str, from_ids: list[str], description: str, creator: str) -> ApiResult:
        return self._post(
            f"/projects/{project_id}/intents",
            {"from": from_ids, "description": description, "creator": creator, "worker": None},
        )

    def create_hint(self, project_id: str, content: str, creator: str) -> ApiResult:
        return self._post(f"/projects/{project_id}/hints", {"content": content, "creator": creator})

    def upload_artifact(self, project_id: str, path: str, content: bytes) -> ApiResult:
        response = self.http.post(f"/projects/{project_id}/artifacts", params={"path": path}, content=content)
        return ApiResult(response.status_code, response.json(), response.text)

    def request_artifact_recovery(self, project_id: str, description: str, worker: str) -> ApiResult:
        return self._post(f"/projects/{project_id}/artifact-recovery", {"description": description, "worker": worker})

    def _post(self, path: str, payload: dict[str, Any]) -> ApiResult:
        response = self.http.post(path, json=payload)
        data = response.json() if response.headers.get("content-type", "").startswith("application/json") else None
        return ApiResult(response.status_code, data, response.text)


class LocalProcess:
    def __init__(self, command: list[str], env: dict[str, str]):
        self.command = command
        self.env = env
        self._process: subprocess.Popen[str] | None = None
        self._cancel_reason: str | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            self._process = subprocess.Popen(
                self.command,
                env={**os.environ, **self.env},
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

    def communicate(self, timeout: float | None) -> ProcessResult:
        assert self._process is not None
        timed_out = False
        try:
            stdout, stderr = self._process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            self.kill()
            stdout, stderr = self._process.communicate()
        return ProcessResult(
            returncode=self._process.returncode,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
            cancelled=self._cancel_reason is not None,
            cancel_reason=self._cancel_reason,
        )

    def kill(self) -> None:
        with self._lock:
            process = self._process
        if process is not None and process.poll() is None:
            process.kill()

    def cancel(self, reason: str) -> None:
        if self._cancel_reason is None:
            self._cancel_reason = reason
        self.kill()


class LocalContainerManager:
    def __init__(self) -> None:
        self.writes: list[tuple[str, str, str]] = []

    def close(self) -> None:
        return None

    def container_name(self, project_id: str) -> str:
        return f"local-{project_id}"

    def ensure_running(self, project_id: str) -> str:
        return self.container_name(project_id)

    def build_exec_process(
        self,
        _container_name: str,
        env: dict[str, str],
        command: list[str],
        timeout_seconds: int | None = None,
        kill_after_seconds: int = 5,
    ) -> LocalProcess:
        assert timeout_seconds is not None
        assert kill_after_seconds == 5
        return LocalProcess(command, env)

    def write_text_file(self, container_name: str, path: str, content: str) -> None:
        self.writes.append((container_name, path, content))

    def needs_completed_cleanup(self, _project_id: str) -> bool:
        return False

    def needs_stopped_cleanup(self, _project_id: str) -> bool:
        return False

    def managed_container_names(self) -> list[str]:
        return []


@pytest.fixture
def http_client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "cairn.db")
    with TestClient(app) as client:
        yield client


def _phase(
    outcome: str,
    *,
    rules: list[dict[str, Any]] | None = None,
    zero_outcomes: list[str] | None = None,
) -> str:
    outcomes = {name: 0 for name in zero_outcomes or []}
    outcomes[outcome] = 1
    payload: dict[str, Any] = {"delay": [0, 0], "outcomes": outcomes}
    if rules is not None:
        payload["rules"] = rules
    return json.dumps(payload)


def _config(
    *,
    bootstrap: str,
    reason: str,
    explore: str,
    task_types: list[str] | None = None,
    worker_healthcheck: str = "startup_only",
    healthcheck: str | None = None,
) -> DispatchConfig:
    return DispatchConfig.model_validate(
        {
            "server": "in-process",
            "runtime": {
                "interval": 1,
                "max_workers": 1,
                "max_running_projects": 1,
                "max_project_workers": 1,
                "healthcheck_timeout": 2,
                "worker_healthcheck": worker_healthcheck,
                "prompt_group": "mock",
            },
            "tasks": {
                "bootstrap": {"timeout": 2, "conclude_timeout": 2},
                "reason": {"timeout": 2, "max_intents": 1},
                "explore": {"timeout": 2, "conclude_timeout": 2},
            },
            "container": {
                "image": "unused",
                "network_mode": "host",
                "completed_action": "stop",
            },
            "workers": [
                {
                    "name": "mock-worker",
                    "type": "mock",
                    "task_types": task_types or ["bootstrap", "reason", "explore"],
                    "max_running": 1,
                    "priority": 0,
                    "env": {
                        "MOCK_HEALTHCHECK": healthcheck or _phase("ok"),
                        "MOCK_BOOTSTRAP": bootstrap,
                        "MOCK_REASON": reason,
                        "MOCK_EXPLORE_EXECUTE": explore,
                    },
                }
            ],
        }
    )


def _loop(config: DispatchConfig, client: InProcessClient, containers: LocalContainerManager) -> DispatcherLoop:
    loop = DispatcherLoop.__new__(DispatcherLoop)
    loop.config = config
    loop.client = client
    loop.container_manager = containers
    loop.executor = ThreadPoolExecutor(max_workers=config.runtime.max_workers)
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
    return loop


def _dispatch_and_wait(loop: DispatcherLoop) -> None:
    loop._reap_futures()
    summaries = loop.client.list_projects()
    loop._initialize_reason_checkpoints(summaries)
    loop._refresh_runtime_projects(summaries)
    loop._cancel_inactive_tasks(summaries)
    loop._queue_container_cleanups(summaries)
    loop._dispatch_available(summaries)
    assert loop.futures
    for future in list(loop.futures):
        future.result(timeout=5)
    loop._reap_futures()


def _create_project(http: TestClient) -> str:
    response = http.post(
        "/projects",
        json={"title": "integration", "origin": "start", "goal": "finish"},
    )
    assert response.status_code == 201
    return response.json()["project"]["id"]


def test_workflow_runs_stages_and_hands_off_real_files_with_remove_cleanup(
    http_client: TestClient, tmp_path, monkeypatch,
) -> None:
    from cairn.dispatcher.tasks import bootstrap as bootstrap_module

    class ResearchDriver(MockDriver):
        def build_execute(self, worker, prompt, session):
            # Exercise actual subprocesses in the real local backend. No model calls.
            context = json.loads(prompt)
            source_path = ""
            if context["goal"] != "Collect evidence":
                assert "artifacts/research/sources.md" not in prompt
                assert "Source A corroborates source B" not in prompt
                reader_path = re.search(r"python3 ([^` ]+) --help", context["optional_history"]).group(1)
                reader_config = json.loads(pathlib.Path(reader_path).with_name("config.json").read_text())
                # The simulated writer deliberately asks for one source stage's artifact.
                # History is retrieved here because it is needed, not supplied in its prompt.
                history = http_client.get(f"/projects/{reader_config['project_id']}/history").json()
                source = history["items"][0]
                artifacts = http_client.get(source["context_url"] + "/artifacts").json()
                source_path = artifacts[0]["path"]
            script = """
import json, pathlib, sys
context = json.loads(sys.argv[1])
if context['goal'] == 'Collect evidence':
    path = pathlib.Path('artifacts/research/sources.md')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('Source A corroborates source B', encoding='utf-8')
    description = 'Evidence is in artifacts/research/sources.md'
else:
    assert 'artifacts/research/sources.md' not in sys.argv[1]
    evidence = pathlib.Path(sys.argv[2]).read_text(encoding='utf-8')
    assert evidence == 'Source A corroborates source B'
    path = pathlib.Path('artifacts/write/book.md')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('Investigation: ' + evidence, encoding='utf-8')
    description = 'Manuscript is in artifacts/write/book.md'
print(json.dumps({'accepted': True, 'data': {'fact': {'description': description},
    'complete': {'description': description}}}))
"""
            return DriverResult(argv=[sys.executable, "-c", script, prompt, source_path], session=session)

    monkeypatch.setattr(bootstrap_module, "get_driver", lambda *_: ResearchDriver())
    config = _config(bootstrap=_phase("complete"), reason=_phase("complete", zero_outcomes=["intent"]), explore=_phase("fact"))
    config.runtime.execution = "local"
    backend = LocalBackend(LocalConfig(workspace_root=str(tmp_path / "runs"), completed_action="remove"))
    client = InProcessClient(http_client)
    loop = _loop(config, client, backend)
    response = http_client.post("/workflows", json={
        "title": "Investigation", "origin": "Starting question", "stages": [
            {"id": "research", "title": "Research", "goal": "Collect evidence", "kind": "research",
             "artifacts": [{"path": "artifacts/research/sources.md"}]},
            {"id": "write", "title": "Write", "goal": "Write manuscript", "kind": "synthesis", "depends_on": ["research"],
             "artifacts": [{"path": "artifacts/write/book.md"}]},
        ],
    })
    assert response.status_code == 201
    workflow_id = response.json()["workflow"]["id"]
    try:
        _dispatch_and_wait(loop)
        halfway = http_client.get(f"/workflows/{workflow_id}").json()
        assert [stage["status"] for stage in halfway["stages"]] == ["completed", "active"]
        _dispatch_and_wait(loop)
        loop._queue_container_cleanups(client.list_projects())
        detail = http_client.get(f"/workflows/{workflow_id}").json()
    finally:
        loop.close()
    assert detail["workflow"]["status"] == "completed"
    workspace = tmp_path / "runs" / workflow_id
    assert (workspace / "artifacts/research/sources.md").exists()
    assert (workspace / "artifacts/write/book.md").read_text() == "Investigation: Source A corroborates source B"
    assert len(list((tmp_path / "runs").iterdir())) == 1
    research_artifact = detail["stages"][0]["collected_artifacts"][0]
    handoff = detail["stages"][1]["handoffs"][0]
    assert handoff["artifact_count"] == 1
    assert http_client.get(handoff["context_url"] + "/artifacts").json() == [research_artifact]
    assert http_client.get(research_artifact["download_url"]).content == b"Source A corroborates source B"
    output = detail["stages"][1]["collected_artifacts"][0]
    assert http_client.get(output["download_url"]).content == b"Investigation: Source A corroborates source B"


def test_missing_output_schedules_recovery_without_reason_starving_exploration(
    http_client: TestClient, tmp_path, monkeypatch,
) -> None:
    from cairn.dispatcher.tasks import bootstrap, explore, reason

    class PrematureCompletionDriver(MockDriver):
        def build_execute(self, worker, prompt, session):
            script = """
import json, pathlib, sys
context = json.loads(sys.argv[1])
if context['phase'] == 'bootstrap':
    result = {'fact': {'description': 'Agent prematurely claims output is ready'},
              'complete': {'description': 'Claimed done'}}
elif context['phase'] == 'reason':
    result = {'complete': {'from': [context['fact_ids'][-1]], 'description': 'Output delivered'}}
else:
    pathlib.Path('result.bin').write_bytes(bytes([0, 255, 3, 4]))
    result = {'description': 'Created result.bin with requested binary output'}
print(json.dumps({'accepted': True, 'data': result}))
"""
            return DriverResult(argv=[sys.executable, "-c", script, prompt], session=session)

    for module in (bootstrap, explore, reason):
        monkeypatch.setattr(module, "get_driver", lambda *_: PrematureCompletionDriver())
    config = _config(bootstrap=_phase("complete"), reason=_phase("complete", zero_outcomes=["intent"]), explore=_phase("fact"))
    config.runtime.execution = "local"
    backend = LocalBackend(LocalConfig(workspace_root=str(tmp_path / "runs")))
    client = InProcessClient(http_client)
    loop = _loop(config, client, backend)
    response = http_client.post("/workflows", json={
        "title": "Binary output", "origin": "Produce a file", "stages": [
            {"id": "produce", "title": "Produce", "artifacts": [{"path": "result.bin"}]},
        ],
    })
    assert response.status_code == 201
    workflow_id = response.json()["workflow"]["id"]
    project_id = response.json()["stages"][0]["project_id"]
    try:
        _dispatch_and_wait(loop)
        blocked = client.get_project(project_id)
        assert blocked.project.status == "active"
        assert len([intent for intent in blocked.intents if intent.to is None]) == 1
        hints_before = len(blocked.hints)
        loop.reason_checkpoints[project_id] = ReasonCheckpoint(2, 0, 1)
        _dispatch_and_wait(loop)  # reasoning repeats completion, recovery already queued
        assert len(client.get_project(project_id).hints) == hints_before
        _dispatch_and_wait(loop)  # exploration now gets a turn and writes the missing output
        assert (tmp_path / "runs" / workflow_id / "result.bin").read_bytes() == bytes([0, 255, 3, 4])
        _dispatch_and_wait(loop)
        detail = http_client.get(f"/workflows/{workflow_id}").json()
    finally:
        loop.close()
    assert detail["workflow"]["status"] == "completed"
    artifact = detail["stages"][0]["collected_artifacts"][0]
    assert http_client.get(artifact["download_url"]).content == bytes([0, 255, 3, 4])


def test_mock_scheduler_bootstrap_completes_project_end_to_end(http_client: TestClient) -> None:
    client = InProcessClient(http_client)
    containers = LocalContainerManager()
    loop = _loop(
        _config(
            bootstrap=_phase("complete"),
            reason=_phase("complete", zero_outcomes=["intent"]),
            explore=_phase("fact"),
        ),
        client,
        containers,
    )
    project_id = _create_project(http_client)

    try:
        _dispatch_and_wait(loop)
        project = client.get_project(project_id)
    finally:
        loop.close()

    assert project.project.status == "completed"
    assert [fact.id for fact in project.facts] == ["origin", "goal", "f001"]
    assert [(intent.id, intent.to) for intent in project.intents] == [("i001", "f001"), ("i002", "goal")]


def test_mock_scheduler_runs_reason_explore_reason_complete_chain(http_client: TestClient) -> None:
    client = InProcessClient(http_client)
    containers = LocalContainerManager()
    loop = _loop(
        _config(
            bootstrap=_phase("complete"),
            reason=_phase("intent", rules=[{"fact_ids_gte": 3, "force": "complete"}]),
            explore=_phase("fact"),
        ),
        client,
        containers,
    )
    project_id = _create_project(http_client)
    seed = client.create_intent(project_id, ["origin"], "seed", "seed-worker")
    assert seed.ok
    assert client.heartbeat(project_id, "i001", "seed-worker").ok
    assert client.conclude(project_id, "i001", "seed-worker", "seed fact").ok

    try:
        _dispatch_and_wait(loop)
        assert loop.reason_checkpoints[project_id] == ReasonCheckpoint(3, 0, 0)
        _dispatch_and_wait(loop)
        _dispatch_and_wait(loop)
        project = client.get_project(project_id)
    finally:
        loop.close()

    assert project.project.status == "completed"
    assert [fact.id for fact in project.facts] == ["origin", "goal", "f001", "f002"]
    assert [(intent.id, intent.to) for intent in project.intents] == [
        ("i001", "f001"),
        ("i002", "f002"),
        ("i003", "goal"),
    ]
    assert any("/reason_execute-" in path and "f002" in content for _, path, content in containers.writes)
    assert any("/explore_execute-" in path and "f001" in content for _, path, content in containers.writes)


def test_mock_scheduler_enabled_project_skips_bootstrap_when_worker_does_not_support_it(
    http_client: TestClient,
) -> None:
    client = InProcessClient(http_client)
    containers = LocalContainerManager()
    loop = _loop(
        _config(
            bootstrap=_phase("complete"),
            reason=_phase("complete", zero_outcomes=["intent"]),
            explore=_phase("fact"),
            task_types=["reason", "explore"],
        ),
        client,
        containers,
    )
    project_id = _create_project(http_client)

    try:
        _dispatch_and_wait(loop)
        project = client.get_project(project_id)
    finally:
        loop.close()

    assert project.project.status == "completed"
    assert [(intent.description, intent.to) for intent in project.intents] == [
        ("mock complete from origin", "goal")
    ]


def test_task_healthcheck_healthy_worker_completes_end_to_end(http_client: TestClient) -> None:
    client = InProcessClient(http_client)
    containers = LocalContainerManager()
    loop = _loop(
        _config(
            bootstrap=_phase("complete"),
            reason=_phase("complete", zero_outcomes=["intent"]),
            explore=_phase("fact"),
            worker_healthcheck="startup_and_task",
        ),
        client,
        containers,
    )
    project_id = _create_project(http_client)

    try:
        _dispatch_and_wait(loop)
        project = client.get_project(project_id)
    finally:
        loop.close()

    # code-based check_health runs before the task, passes, and the bootstrap completes
    assert project.project.status == "completed"


def test_task_healthcheck_failure_aborts_task_and_cools_down_worker(http_client: TestClient) -> None:
    client = InProcessClient(http_client)
    containers = LocalContainerManager()
    loop = _loop(
        _config(
            bootstrap=_phase("complete"),
            reason=_phase("complete", zero_outcomes=["intent"]),
            explore=_phase("fact"),
            worker_healthcheck="startup_and_task",
            healthcheck=_phase("fail", zero_outcomes=["ok"]),
        ),
        client,
        containers,
    )
    project_id = _create_project(http_client)

    try:
        _dispatch_and_wait(loop)
        project = client.get_project(project_id)
    finally:
        loop.close()

    # unhealthy worker -> task aborted before execution, no facts written, worker put on cooldown
    assert project.project.status == "active"
    assert [fact.id for fact in project.facts] == ["origin", "goal"]
    assert "mock-worker" in loop.worker_unhealthy_until


def _failover_config() -> DispatchConfig:
    def worker(name: str, priority: int, healthcheck: str) -> dict:
        return {
            "name": name,
            "type": "mock",
            "task_types": ["bootstrap", "reason", "explore"],
            "max_running": 1,
            "priority": priority,
            "env": {
                "MOCK_HEALTHCHECK": healthcheck,
                "MOCK_BOOTSTRAP": _phase("complete"),
                "MOCK_REASON": _phase("complete", zero_outcomes=["intent"]),
                "MOCK_EXPLORE_EXECUTE": _phase("fact"),
            },
        }

    return DispatchConfig.model_validate(
        {
            "server": "in-process",
            "runtime": {
                "interval": 1,
                "max_workers": 1,
                "max_running_projects": 1,
                "max_project_workers": 1,
                "healthcheck_timeout": 2,
                "worker_healthcheck": "startup_and_task",
                "prompt_group": "mock",
            },
            "tasks": {
                "bootstrap": {"timeout": 2, "conclude_timeout": 2},
                "reason": {"timeout": 2, "max_intents": 1},
                "explore": {"timeout": 2, "conclude_timeout": 2},
            },
            "container": {"image": "unused", "network_mode": "host", "completed_action": "stop"},
            "workers": [
                worker("bad", 0, _phase("fail", zero_outcomes=["ok"])),
                worker("good", 1, _phase("ok")),
            ],
        }
    )


def test_unhealthy_worker_fails_over_to_healthy_worker(http_client: TestClient) -> None:
    client = InProcessClient(http_client)
    containers = LocalContainerManager()
    loop = _loop(_failover_config(), client, containers)
    project_id = _create_project(http_client)

    try:
        # round 1: 'bad' (priority 0) is chosen first, its health check fails -> cooldown
        _dispatch_and_wait(loop)
        assert "bad" in loop.worker_unhealthy_until
        assert client.get_project(project_id).project.status == "active"

        # round 2: 'bad' still cooling down -> 'good' takes over and completes the project
        _dispatch_and_wait(loop)
        project = client.get_project(project_id)
    finally:
        loop.close()

    assert project.project.status == "completed"
    assert any(intent.worker == "good" for intent in project.intents)
