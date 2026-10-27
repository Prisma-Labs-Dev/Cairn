from __future__ import annotations

import json
import uuid
from pathlib import PurePosixPath
from typing import Any

from cairn.dispatcher.config import WorkerConfig
from cairn.dispatcher.workers.base import DriverResult, WorkerDriver
from cairn.dispatcher.workers.health import HealthResult, http_ping, proxies_from_env


# Pi intentionally accepts fuzzy patterns and can construct unknown provider
# model IDs. An explicit Cairn selection must instead exist exactly once in
# Pi's available offline catalog before the model-bearing command starts.
LOCAL_MODEL_CHECK_SCRIPT = """\
import os, re, subprocess, sys, tempfile
provider, requested = sys.argv[1:3]
try:
    with tempfile.TemporaryFile() as catalog:
        subprocess.run(['pi', '--offline', '--no-extensions', '--no-skills',
                        '--no-prompt-templates', '--no-context-files', '--list-models'],
                       stdout=catalog, stderr=subprocess.DEVNULL, check=True, timeout=15,
                       env={**os.environ, 'NO_COLOR': '1', 'FORCE_COLOR': '0'})
        catalog.seek(0)
        raw = catalog.read(1048577)
    if len(raw) > 1048576:
        raise ValueError('catalog exceeds the bounded reader')
    matches = set()
    for line in raw.decode('utf-8', errors='replace').splitlines():
        parts = line.split()
        if len(parts) != 6 or parts[4] not in ('yes', 'no') or parts[5] not in ('yes', 'no'):
            continue
        if not re.fullmatch(r'[0-9]+(?:\\.[0-9]+)?[KM]?', parts[2]):
            continue
        actual_provider, actual_model = parts[:2]
        if provider and actual_provider != provider:
            continue
        if requested in (actual_model, actual_provider + '/' + actual_model):
            matches.add((actual_provider, actual_model))
    if len(matches) != 1:
        raise ValueError('explicit model is absent or ambiguous')
    actual_provider, actual_model = next(iter(matches))
    if len(sys.argv) > 3:
        if sys.argv[3] != '--execute':
            raise ValueError('invalid execution arguments')
        forwarded = sys.argv[4:]
        configured = (['--provider', provider] if provider else []) + ['--model', requested]
        if forwarded[:len(configured)] != configured:
            raise ValueError('configured selectors do not match the checked request')
        forwarded = forwarded[len(configured):]
        # Pi strips one explicit provider prefix, then prefers canonical
        # references over raw IDs. Preserve a canonical pair after that strip
        # when the real ID itself begins with its provider's name.
        model_reference = actual_provider + '/' + actual_model
        if actual_model.lower().startswith((actual_provider + '/').lower()):
            model_reference = actual_provider + '/' + model_reference
        os.execvp('pi', ['pi', '--provider', actual_provider, '--model',
                        model_reference, *forwarded])
except Exception:
    sys.stderr.write('Cairn: explicit Pi model is unavailable or ambiguous in the offline catalog. '
                     'Configure an exact provider/model from pi --offline --list-models.\\n')
    sys.exit(2)
"""


class PiDriver(WorkerDriver):
    type_name = "pi"

    def __init__(self, local: bool = False):
        self.local = local

    def local_binary(self) -> str | None:
        return "pi"

    def prepare_session(self) -> str:
        return f"cairn-sessions/{uuid.uuid4()}.jsonl"

    def check_health(self, worker: WorkerConfig, *, timeout: float) -> HealthResult:
        env = worker.env
        base = env["PI_BASE_URL"].rstrip("/")
        model = worker.model or env["PI_MODEL"]
        api = env["PI_PROVIDER_API"]
        proxies = proxies_from_env(env)
        headers = {"Authorization": f"Bearer {env['PI_API_KEY']}", "content-type": "application/json"}
        if "anthropic" in api:
            return http_ping(
                f"{base}/v1/messages",
                headers={**headers, "anthropic-version": "2023-06-01"},
                json_body={"model": model, "max_tokens": 10, "messages": [{"role": "user", "content": "ping"}]},
                timeout=timeout,
                proxies=proxies,
            )
        if "responses" in api:
            return http_ping(
                f"{base}/responses",
                headers=headers,
                json_body={"model": model, "input": [{"role": "user", "content": "ping"}], "stream": False},
                timeout=timeout,
                proxies=proxies,
            )
        # openai-completions and anything else: OpenAI-compatible chat/completions
        return http_ping(
            f"{base}/chat/completions",
            headers=headers,
            json_body={"model": model, "max_tokens": 10, "messages": [{"role": "user", "content": "ping"}]},
            timeout=timeout,
            proxies=proxies,
        )

    def describe_health(self, worker: WorkerConfig) -> str:
        env = worker.env
        return f"POST {env['PI_BASE_URL']} (api={env['PI_PROVIDER_API']}, model={worker.model or env['PI_MODEL']})"

    def build_execute(self, worker: WorkerConfig, prompt: str, session: str | None) -> DriverResult:
        session = session or self.prepare_session()
        if self.local:
            return DriverResult(argv=self._local_argv(worker, prompt, session), session=session)
        env = worker.env
        argv = [
            "--provider",
            "cairn",
            "--model",
            worker.model or env["PI_MODEL"],
            "--mode",
            "json",
            "--session-dir",
            self._session_dir(worker),
        ]
        argv.extend(["--session", session])
        if worker.reasoning_effort:
            argv.extend(["--thinking", worker.reasoning_effort])
        argv.extend(["-p", prompt])
        return DriverResult(argv=self._wrap_with_models(worker, argv), session=session)

    def build_conclude(self, worker: WorkerConfig, prompt: str, session: str) -> list[str]:
        if self.local:
            return self._local_argv(worker, prompt, session)
        env = worker.env
        argv = [
            "--provider",
            "cairn",
            "--model",
            worker.model or env["PI_MODEL"],
            "--mode",
            "json",
            "--session-dir",
            self._session_dir(worker),
            "--session",
            session,
        ]
        if worker.reasoning_effort:
            argv.extend(["--thinking", worker.reasoning_effort])
        argv.extend(["-p", prompt])
        return self._wrap_with_models(worker, argv)

    def _local_argv(self, worker: WorkerConfig, prompt: str, session: str | None) -> list[str]:
        # Native Pi keeps host credentials and configuration. Only explicit worker
        # selectors override its provider/model/thinking defaults.
        session_dir = self._session_dir(worker)
        selectors: list[str] = []
        if worker.provider:
            selectors.extend(["--provider", worker.provider])
        if worker.model:
            selectors.extend(["--model", worker.model])
        pi_argv = [
            *selectors,
            "--mode",
            "json",
            "--session-dir",
            session_dir,
            "--no-extensions",
            "--no-skills",
            "--no-prompt-templates",
            "--no-themes",
            "--no-context-files",
            "--tools",
            "read,write,edit,bash,grep,find,ls",
        ]
        if session:
            pi_argv.extend(["--session", session])
        if worker.reasoning_effort:
            pi_argv.extend(["--thinking", worker.reasoning_effort])
        pi_argv.extend(["-p", prompt])
        script = (
            'sdir="$1"\nprovider="$2"\nmodel="$3"\nmodel_check="$4"\nshift 4\n'
            'mkdir -p "$sdir" cairn-sessions\n'
            'if [ -n "$model" ]; then\n'
            '  exec python3 -c "$model_check" "$provider" "$model" --execute "$@"\n'
            'fi\nexec pi "$@"\n'
        )
        return ["/bin/sh", "-lc", script, "--", session_dir, worker.provider or "", worker.model or "",
                LOCAL_MODEL_CHECK_SCRIPT, *pi_argv]

    def extract_session(self, session: str | None, stdout: str, stderr: str) -> str | None:
        if session:
            return session
        for event in self._iter_events(stdout):
            if event.get("type") != "session":
                continue
            session_id = event.get("id")
            if isinstance(session_id, str) and session_id:
                return session_id
        return None

    def extract_response_text(self, stdout: str, stderr: str) -> str:
        assistant_message: dict[str, Any] | None = None
        for event in self._iter_events(stdout):
            event_type = event.get("type")
            if event_type == "turn_end":
                message = event.get("message")
                if isinstance(message, dict) and message.get("role") == "assistant":
                    assistant_message = message
            elif event_type == "agent_end":
                messages = event.get("messages")
                if isinstance(messages, list):
                    for message in reversed(messages):
                        if isinstance(message, dict) and message.get("role") == "assistant":
                            assistant_message = message
                            break
        if assistant_message is None:
            return stdout
        content = assistant_message.get("content")
        if not isinstance(content, list):
            return stdout
        parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") != "text":
                continue
            text = item.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
        return "\n".join(parts).strip() or stdout

    def _wrap_with_models(self, worker: WorkerConfig, pi_argv: list[str], *, enable_tools: bool = True) -> list[str]:
        script = (
            'agent_dir="$1"\n'
            'models_json="$2"\n'
            "shift 2\n"
            'mkdir -p "$agent_dir"\n'
            'mkdir -p "$agent_dir/sessions"\n'
            'mkdir -p cairn-sessions\n'
            'printf "%s" "$models_json" > "$agent_dir/models.json"\n'
            'exec env PI_CODING_AGENT_DIR="$agent_dir" pi "$@"\n'
        )
        argv = [
            "--no-extensions",
            "--no-skills",
            "--no-prompt-templates",
            "--no-themes",
            "--no-context-files",
        ]
        if enable_tools:
            argv.extend(["--tools", "read,write,edit,bash,grep,find,ls"])
        return [
            "/bin/sh",
            "-lc",
            script,
            "--",
            self._agent_dir(worker),
            self._models_json(worker),
            *argv,
            *pi_argv,
        ]

    @staticmethod
    def _agent_dir(worker: WorkerConfig) -> str:
        return str(PurePosixPath("/tmp/cairn-pi") / worker.name)

    @staticmethod
    def _session_dir(worker: WorkerConfig) -> str:
        return str(PurePosixPath(PiDriver._agent_dir(worker)) / "sessions")

    @staticmethod
    def _iter_events(stdout: str) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                events.append(payload)
        return events

    @staticmethod
    def _models_json(worker: WorkerConfig) -> str:
        env = worker.env
        model: dict[str, Any] = {
            "id": worker.model or env["PI_MODEL"],
            "name": worker.model or env["PI_MODEL"],
        }
        context_window = env.get("PI_MODEL_CONTEXT_WINDOW")
        if context_window:
            model["contextWindow"] = int(context_window)

        provider: dict[str, Any] = {
            "baseUrl": env["PI_BASE_URL"],
            "api": env["PI_PROVIDER_API"],
            "apiKey": env["PI_API_KEY"],
            "models": [model],
        }
        payload = {"providers": {"cairn": provider}}
        return json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
