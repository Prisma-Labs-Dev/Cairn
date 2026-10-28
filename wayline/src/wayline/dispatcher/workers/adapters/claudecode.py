from __future__ import annotations

from wayline.dispatcher.usage_parsers import json_events

from wayline.dispatcher.config import WorkerConfig
from wayline.dispatcher.workers.base import DriverResult, SeedSessionDriver
from wayline.dispatcher.workers.health import HealthResult, http_ping, proxies_from_env


ANTHROPIC_VERSION = "2023-06-01"


class ClaudeCodeDriver(SeedSessionDriver):
    type_name = "claudecode"

    def local_binary(self) -> str | None:
        return "claude"

    def check_health(self, worker: WorkerConfig, *, timeout: float) -> HealthResult:
        env = worker.env
        return http_ping(
            f"{env['ANTHROPIC_BASE_URL']}/v1/messages",
            headers={
                "Authorization": f"Bearer {env['ANTHROPIC_AUTH_TOKEN']}",
                "anthropic-version": ANTHROPIC_VERSION,
                "content-type": "application/json",
            },
            json_body={
                "model": env["ANTHROPIC_MODEL"],
                "max_tokens": 10,
                "messages": [{"role": "user", "content": "ping"}],
            },
            timeout=timeout,
            proxies=proxies_from_env(env),
        )

    def describe_health(self, worker: WorkerConfig) -> str:
        return f"POST {worker.env['ANTHROPIC_BASE_URL']}/v1/messages (model={worker.env['ANTHROPIC_MODEL']})"

    def build_execute(self, worker: WorkerConfig, prompt: str, session: str | None) -> DriverResult:
        assert session is not None
        return DriverResult(
            argv=[
                "claude",
                "--session-id",
                session,
                "--dangerously-skip-permissions",
                "-p",
                "--output-format", "stream-json", "--verbose",
                "--",
                prompt,
            ],
            session=session,
        )

    def build_conclude(self, worker: WorkerConfig, prompt: str, session: str) -> list[str]:
        return [
            "claude",
            "-r",
            session,
            "--dangerously-skip-permissions",
            "-p",
            "--output-format", "stream-json", "--verbose",
            "--",
            prompt,
        ]

    def extract_response_text(self, stdout: str, stderr: str) -> str:
        events = json_events(stdout)
        results = [event for event in events if event.get("type") == "result"]
        if results:
            return results[-1].get("result", "") if isinstance(results[-1].get("result", ""), str) else ""
        assistants = [event.get("message") for event in events if event.get("type") == "assistant" and isinstance(event.get("message"), dict)]
        if assistants:
            return "\n".join(block["text"] for block in assistants[-1].get("content", []) if block.get("type") == "text" and isinstance(block.get("text"), str))
        return "" if events and any(event.get("type") in ("system", "result", "assistant") for event in events) else stdout
