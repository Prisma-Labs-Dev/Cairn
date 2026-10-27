from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import logging
import threading

from pydantic import TypeAdapter
import requests
from requests.adapters import HTTPAdapter

from cairn.server.models import Intent, ProjectDetail, ProjectSummary, Settings
from cairn.server.runtime_models import DispatcherReport, RuntimeSettingsState

LOG = logging.getLogger(__name__)


class ProtocolError(RuntimeError):
    def __init__(self, message: str, status_code: int, response_text: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.response_text = response_text


@dataclass(slots=True)
class ApiResult:
    status_code: int
    data: Any | None = None
    text: str = ""

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300


class CairnClient:
    def __init__(self, base_url: str, timeout: float = 10.0):
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._summary_adapter = TypeAdapter(list[ProjectSummary])
        self._local = threading.local()
        self._sessions: dict[int, requests.Session] = {}
        self._sessions_lock = threading.Lock()

    def close(self) -> None:
        with self._sessions_lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            session.close()

    def list_projects(self) -> list[ProjectSummary]:
        response = self._session().get(self._url("/projects"), timeout=self._timeout)
        response.raise_for_status()
        return self._summary_adapter.validate_python(response.json())

    def get_project(self, project_id: str) -> ProjectDetail:
        response = self._session().get(self._url(f"/projects/{project_id}"), timeout=self._timeout)
        response.raise_for_status()
        return ProjectDetail.model_validate(response.json())

    def get_settings(self) -> Settings:
        response = self._session().get(self._url("/settings"), timeout=self._timeout)
        response.raise_for_status()
        return Settings.model_validate(response.json())

    def get_runtime_settings(self) -> RuntimeSettingsState:
        response = self._session().get(self._url("/runtime/settings"), timeout=self._timeout)
        response.raise_for_status()
        return RuntimeSettingsState.model_validate(response.json())

    def publish_runtime(self, dispatcher_id: str, report: DispatcherReport) -> ApiResult:
        return self._request_json("POST", f"/runtime/dispatchers/{dispatcher_id}", report.model_dump())

    def record_session_invocation(self, payload: dict) -> ApiResult:
        return self._record_usage("/agent-sessions", payload)

    def record_session_chunk(self, invocation_id: str, payload: dict, *, events=False) -> ApiResult:
        suffix = "event-chunks" if events else "chunks"
        return self._record_usage(f"/agent-sessions/{invocation_id}/{suffix}", payload)

    def record_session_result(self, conversation_id: str, payload: dict) -> ApiResult:
        return self._record_usage(f"/agent-conversations/{conversation_id}/results", payload)

    def record_usage_reporter(self, payload: dict) -> ApiResult:
        return self._record_usage("/usage/reporters", payload)

    def record_usage_attempt(self, payload: dict) -> ApiResult:
        return self._record_usage("/usage/attempts", payload)

    def _record_usage(self, path: str, payload: dict) -> ApiResult:
        try:
            response = self._session().post(self._url(path), json=payload, timeout=(1, 1))
        except requests.RequestException:
            return ApiResult(0)
        try:
            data = response.json() if response.text else None
        except ValueError:
            data = None
        return ApiResult(response.status_code, data)

    def export_project(self, project_id: str) -> str:
        response = self._session().get(
            self._url(f"/projects/{project_id}/export"),
            params={"format": "yaml"},
            timeout=self._timeout,
        )
        response.raise_for_status()
        return response.text

    def heartbeat(self, project_id: str, intent_id: str, worker: str) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/intents/{intent_id}/heartbeat",
            json={"worker": worker},
        )

    def claim_reason(self, project_id: str, worker: str, trigger: str) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/reason/claim",
            json={"worker": worker, "trigger": trigger},
        )

    def reason_heartbeat(self, project_id: str, worker: str) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/reason/heartbeat",
            json={"worker": worker},
        )

    def release_reason(self, project_id: str, worker: str) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/reason/release",
            json={"worker": worker},
        )

    def release(self, project_id: str, intent_id: str, worker: str) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/intents/{intent_id}/release",
            json={"worker": worker},
        )

    def conclude(self, project_id: str, intent_id: str, worker: str, description: str) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/intents/{intent_id}/conclude",
            json={"worker": worker, "description": description},
        )

    def complete(self, project_id: str, from_ids: list[str], description: str, worker: str, goals: list[dict] | None = None, producer_agent_id: str | None = None, producer_session_id: str | None = None, selected_goal: str | None = None, selection_reason: str | None = None, result: dict | None = None) -> ApiResult:
        payload = {"from": from_ids, "description": description, "worker": worker}
        if producer_agent_id is not None:
            payload["producer_agent_id"] = producer_agent_id
            payload["producer_session_id"] = producer_session_id
        if result is not None:
            payload["result"] = result
        if goals is not None:
            payload["goals"] = goals
        if selected_goal is not None:
            payload["selected_goal"] = selected_goal
        if selection_reason is not None:
            payload["selection_reason"] = selection_reason
        return self._request_json(
            "POST",
            f"/projects/{project_id}/complete",
            json=payload,
        )

    def request_result_recovery(self, project_id: str, description: str, worker: str) -> ApiResult:
        return self._request_json("POST", f"/projects/{project_id}/result-recovery", json={"worker": worker, "description": description})

    def unavailable(self, project_id: str, from_ids: list[str], description: str, worker: str) -> ApiResult:
        return self._request_json("POST", f"/projects/{project_id}/unavailable",
                                  {"from": from_ids, "description": description, "worker": worker})

    def claim_review(self, claim_id: str, identity: dict) -> ApiResult:
        return self._request_json("POST", f"/reviews/{claim_id}/claim", identity)

    def review_heartbeat(self, claim_id: str, identity: dict) -> ApiResult:
        return self._request_json("POST", f"/reviews/{claim_id}/heartbeat", identity)

    def release_review(self, claim_id: str, identity: dict) -> ApiResult:
        return self._request_json("POST", f"/reviews/{claim_id}/release", identity)

    def decide_review(self, claim_id: str, payload: dict) -> ApiResult:
        return self._request_json("POST", f"/reviews/{claim_id}/decision", payload)

    def request_expansion_recovery(self, project_id: str, description: str, worker: str) -> ApiResult:
        return self._request_json(
            "POST", f"/projects/{project_id}/expansion-recovery",
            json={"worker": worker, "description": description},
        )

    def create_intent(self, project_id: str, from_ids: list[str], description: str, creator: str) -> ApiResult:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/intents",
            json={"from": from_ids, "description": description, "creator": creator, "worker": None},
        )

    def create_hint(self, project_id: str, content: str, creator: str) -> ApiResult:
        return self._request_json(
            "POST", f"/projects/{project_id}/hints", json={"content": content, "creator": creator},
        )

    def upload_artifact(self, project_id: str, path: str, content: bytes) -> ApiResult:
        try:
            response = self._session().post(
                self._url(f"/projects/{project_id}/artifacts"), params={"path": path},
                data=content, headers={"Content-Type": "application/octet-stream"}, timeout=self._timeout,
            )
        except requests.RequestException as exc:
            return ApiResult(status_code=0, text=str(exc))
        data = response.json() if response.headers.get("content-type", "").startswith("application/json") else None
        return ApiResult(status_code=response.status_code, data=data, text=response.text)

    def request_artifact_recovery(self, project_id: str, description: str, worker: str) -> ApiResult:
        return self._request_json(
            "POST", f"/projects/{project_id}/artifact-recovery",
            json={"worker": worker, "description": description},
        )

    def _request_json(self, method: str, path: str, json: dict[str, Any]) -> ApiResult:
        try:
            response = self._session().request(
                method,
                self._url(path),
                json=json,
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            LOG.warning("request failed method=%s path=%s error=%s", method, path, exc)
            return ApiResult(status_code=0, text=str(exc))
        data: Any | None = None
        if response.text and response.headers.get("content-type", "").startswith("application/json"):
            data = response.json()
        return ApiResult(status_code=response.status_code, data=data, text=response.text)

    def _url(self, path: str) -> str:
        return f"{self._base_url}{path}"

    def _session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is not None:
            return session

        session = requests.Session()
        adapter = HTTPAdapter(pool_connections=64, pool_maxsize=64, pool_block=False)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        self._local.session = session
        with self._sessions_lock:
            self._sessions[threading.get_ident()] = session
        return session
