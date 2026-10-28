from __future__ import annotations

from wayline.dispatcher.usage_parsers import json_events

from wayline.dispatcher.config import WorkerConfig
from wayline.dispatcher.workers.base import DriverResult, RegexSessionDriver
from wayline.dispatcher.workers.health import HealthResult, http_ping, proxies_from_env


class CodexDriver(RegexSessionDriver):
    type_name = "codex"

    def __init__(self, local: bool = False):
        self.local = local

    def local_binary(self) -> str | None:
        return "codex"

    def check_health(self, worker: WorkerConfig, *, timeout: float) -> HealthResult:
        env = worker.env
        return http_ping(
            f"{env['CODEX_BASE_URL']}/responses",
            headers={
                "Authorization": f"Bearer {env['OPENAI_API_KEY']}",
                "content-type": "application/json",
            },
            json_body={
                "model": worker.model or env["CODEX_MODEL"],
                "input": [{"role": "user", "content": "ping"}],
                "stream": False,
            },
            timeout=timeout,
            proxies=proxies_from_env(env),
        )

    def describe_health(self, worker: WorkerConfig) -> str:
        return f"POST {worker.env['CODEX_BASE_URL']}/responses (model={worker.model or worker.env['CODEX_MODEL']})"

    def _model_options(self, worker: WorkerConfig) -> list[str]:
        options = []
        model = worker.model if self.local else worker.model or worker.env["CODEX_MODEL"]
        effort = worker.reasoning_effort if self.local else worker.reasoning_effort or "high"
        if model:
            options.extend(["--model", model])
        if effort:
            options.extend(["-c", f'model_reasoning_effort="{effort}"'])
        return options

    def build_execute(self, worker: WorkerConfig, prompt: str, session: str | None) -> DriverResult:
        if self.local:
            return DriverResult(
                argv=[
                    "codex",
                    "exec",
                    "--json",
                    "--dangerously-bypass-approvals-and-sandbox",
                    *self._model_options(worker),
                    "--",
                    prompt,
                ]
            )
        env = worker.env
        return DriverResult(
            argv=[
                "codex",
                "exec",
                "--json",
                "--dangerously-bypass-approvals-and-sandbox",
                *self._model_options(worker),
                "-c",
                'model_provider="wayline"',
                "-c",
                'model_providers.wayline.name="wayline"',
                "-c",
                'model_providers.wayline.wire_api="responses"',
                "-c",
                f'model_providers.wayline.base_url="{env["CODEX_BASE_URL"]}"',
                "-c",
                'model_providers.wayline.env_key="OPENAI_API_KEY"',
                "--",
                prompt,
            ]
        )

    def build_conclude(self, worker: WorkerConfig, prompt: str, session: str) -> list[str]:
        if self.local:
            return [
                "codex",
                "exec",
                "resume",
                "--json",
                session,
                "--dangerously-bypass-approvals-and-sandbox",
                *self._model_options(worker),
                "--",
                prompt,
            ]
        env = worker.env
        return [
            "codex",
            "exec",
            "resume",
            "--json",
            session,
            "--dangerously-bypass-approvals-and-sandbox",
            *self._model_options(worker),
            "-c",
            'model_provider="wayline"',
            "-c",
            'model_providers.wayline.name="wayline"',
            "-c",
            'model_providers.wayline.wire_api="responses"',
            "-c",
            f'model_providers.wayline.base_url="{env["CODEX_BASE_URL"]}"',
            "-c",
            'model_providers.wayline.env_key="OPENAI_API_KEY"',
            "--",
            prompt,
        ]

    def extract_session(self, session: str | None, stdout: str, stderr: str) -> str | None:
        for event in json_events(stdout):
            if event.get("type") == "thread.started" and isinstance(event.get("thread_id"), str):
                return event["thread_id"]
        return super().extract_session(session, stdout, stderr)

    def extract_response_text(self, stdout: str, stderr: str) -> str:
        events = json_events(stdout)
        messages = [event["item"]["text"] for event in events if event.get("type") == "item.completed"
                    and isinstance(event.get("item"), dict) and event["item"].get("type") == "agent_message"
                    and isinstance(event["item"].get("text"), str)]
        if messages:
            return messages[-1]
        return "" if any(event.get("type") in ("thread.started", "turn.completed", "turn.failed", "turn.started") for event in events) else stdout
