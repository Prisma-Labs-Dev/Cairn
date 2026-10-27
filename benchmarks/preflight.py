#!/usr/bin/env python3
"""Verify benchmark model routing without starting any model invocation."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess

import yaml

from cairn.dispatcher.config import DispatchConfig
from cairn.dispatcher.workers.adapters.codex import CodexDriver


def verify(config: DispatchConfig) -> dict:
    if not config.workers or any(w.type != "codex" or w.model != "gpt-6-luna" or w.reasoning_effort != "low" for w in config.workers):
        raise ValueError("every worker must explicitly select codex, gpt-6-luna, and low effort")
    if {task for worker in config.workers for task in worker.task_types} != {"bootstrap", "reason", "explore"}:
        raise ValueError("Luna workers must cover every phase")
    if config.runtime.max_workers != 2 or config.runtime.max_project_workers != 2 or config.runtime.max_running_projects != 2:
        raise ValueError("pilot uses identical limits of 2 workers, 2 workers/project, and 2 running projects")
    if config.runtime.execution == "local" and (not config.local or not config.local.workspace_root or "REPLACE_" in config.local.workspace_root):
        raise ValueError("set an isolated runtime workspace_root before using this config")
    driver = CodexDriver(local=config.runtime.execution == "local")
    for worker in config.workers:
        for command in [driver.build_execute(worker, "preflight only", None).argv,
                        driver.build_conclude(worker, "preflight only", "no-real-session")]:
            if command[command.index("--model") + 1] != "gpt-6-luna" or 'model_reasoning_effort="low"' not in command:
                raise ValueError("adapter failed explicit model routing; do not launch")
    return {"routing_verified": True, "model": "gpt-6-luna", "effort": "low", "live_model_access_verified": False}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--model-cache", type=Path, default=Path.home() / ".codex/models_cache.json")
    args = parser.parse_args()
    try:
        result = verify(DispatchConfig.model_validate(yaml.safe_load(args.config.read_text())))
        executable = shutil.which("codex")
        if not executable:
            raise ValueError("codex CLI unavailable")
        for command in ([executable, "exec", "--help"], [executable, "exec", "resume", "--help"]):
            help_text = subprocess.run(command, capture_output=True, text=True, check=True).stdout
            if "--model" not in help_text:
                raise ValueError("installed CLI does not support explicit model routing")
        cache = json.loads(args.model_cache.read_text())
        model = next((m for m in cache.get("models", []) if m.get("slug") == "gpt-6-luna"), None)
        if not model or model.get("visibility") == "hide":
            raise ValueError("exact Luna model absent from local CLI catalog; never substitute another model")
        result.update({"cli_model_catalog_match": True, "catalog_fetched_at": cache.get("fetched_at"),
                       "status": "routing_ready_live_access_unverified", "model_calls_made": 0})
    except (ValueError, OSError, KeyError, subprocess.SubprocessError) as exc:
        parser.exit(2, str(exc) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
