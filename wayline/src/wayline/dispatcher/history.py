from __future__ import annotations

from importlib import resources
import json
from urllib.parse import urlsplit, urlunsplit
import uuid

from wayline.dispatcher.config import DispatchConfig
from wayline.dispatcher.runtime.backend import ExecutionBackend
from wayline.server.models import ProjectDetail

HISTORY_HELPER_ROOT = "/tmp/wayline-history"


def worker_history_server_url(config: DispatchConfig) -> str:
    """Resolve one configured endpoint, without guessing or retrying other hosts."""
    if config.runtime.history_server_url is not None:
        return config.runtime.history_server_url
    parts = urlsplit(config.server)
    if (config.runtime.execution == "container" and config.container is not None
            and config.container.network_mode == "host" and parts.hostname == "wayline-server"):
        # The shipped compose server name is visible to the dispatcher container,
        # but workers use host networking and reach compose's published server port.
        port = f":{parts.port}" if parts.port is not None else ""
        return urlunsplit((parts.scheme, f"127.0.0.1{port}", parts.path, "", ""))
    return config.server.rstrip("/")


def write_optional_history_reference(
    config: DispatchConfig, backend: ExecutionBackend, runtime: str, project: ProjectDetail,
) -> str:
    """Install a scoped reader, without fetching or embedding any historical records."""
    if not project.project.workflow_id:
        return ""
    directory = f"{HISTORY_HELPER_ROOT}/{uuid.uuid4().hex}"
    reader_path = f"{directory}/history.py"
    reader = resources.files("wayline.dispatcher").joinpath("history_reader.py").read_text(encoding="utf-8")
    backend.write_text_file(runtime, reader_path, reader)
    backend.write_text_file(runtime, f"{directory}/config.json", json.dumps({
        "server": worker_history_server_url(config), "project_id": project.project.id,
    }))
    return (
        "Prior-stage history is optional reference material. Focus on this stage's current goal. "
        "Do not read or list history unless it helps the current task. If needed, inspect only "
        "the relevant stage, finding, intent, completion, or artifact metadata with this scoped reader: "
        f"`python3 {reader_path} --help`. "
        "Its JSON output is paged and selected text is bounded by default; use --full only when "
        "you deliberately need a complete selected record. Prior findings are inherited claims, "
        "not independent verification. Working files remain in the shared workflow directory."
    )


def add_optional_history(
    prompt: str, config: DispatchConfig, backend: ExecutionBackend, runtime: str,
    project: ProjectDetail,
) -> str:
    reference = write_optional_history_reference(config, backend, runtime, project)
    if not reference:
        return prompt
    # Mock and user-defined JSON prompts must remain valid JSON.
    try:
        payload = json.loads(prompt)
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        payload["optional_history"] = reference
        return json.dumps(payload, ensure_ascii=False)
    return prompt + "\n\n## Optional earlier-stage context\n" + reference
