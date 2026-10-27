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
from cairn.dispatcher.workers.adapters.pi import LOCAL_MODEL_CHECK_SCRIPT, PiDriver


def verify(config: DispatchConfig) -> dict:
    kinds = {w.type for w in config.workers}
    if not config.workers or len(kinds) != 1 or not kinds <= {"codex", "pi"} or any(w.model != "gpt-6-luna" or w.reasoning_effort != "low" for w in config.workers):
        raise ValueError("every worker must explicitly select the same codex or pi runtime, gpt-6-luna, and low effort")
    kind = config.workers[0].type
    if kind == "pi" and (config.runtime.execution != "local" or any(w.provider != "openai-codex" for w in config.workers)):
        raise ValueError("Pi subscription workers require local execution and exact openai-codex provider")
    if {task for worker in config.workers for task in worker.task_types} != {"bootstrap", "reason", "explore"}:
        raise ValueError("Luna workers must cover every phase")
    if config.runtime.max_workers != 2 or config.runtime.max_project_workers != 2 or config.runtime.max_running_projects != 2:
        raise ValueError("pilot uses identical limits of 2 workers, 2 workers/project, and 2 running projects")
    if config.runtime.execution == "local" and (not config.local or not config.local.workspace_root or "REPLACE_" in config.local.workspace_root):
        raise ValueError("set an isolated runtime workspace_root before using this config")
    driver = (PiDriver if kind == "pi" else CodexDriver)(local=config.runtime.execution == "local")
    for worker in config.workers:
        for command in [driver.build_execute(worker, "preflight only", None).argv,
                        driver.build_conclude(worker, "preflight only", "no-real-session")]:
            effort_matches = (command[command.index("--thinking") + 1] == "low" if kind == "pi" else 'model_reasoning_effort="low"' in command)
            provider_matches = kind != "pi" or command[command.index("--provider") + 1] == "openai-codex"
            if command[command.index("--model") + 1] != "gpt-6-luna" or not effort_matches or not provider_matches:
                raise ValueError("adapter failed explicit model routing; do not launch")
    return {"routing_verified": True, "runtime": kind, "model": "gpt-6-luna", "effort": "low", "live_model_access_verified": False}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--model-cache", type=Path, default=Path.home() / ".codex/models_cache.json")
    args = parser.parse_args()
    try:
        result = verify(DispatchConfig.model_validate(yaml.safe_load(args.config.read_text())))
        executable = shutil.which(result["runtime"])
        if not executable:
            raise ValueError(f"{result['runtime']} CLI unavailable")
        commands = [[executable, "--help"]] if result["runtime"] == "pi" else [[executable, "exec", "--help"], [executable, "exec", "resume", "--help"]]
        for command in commands:
            help_result = subprocess.run(command, capture_output=True, text=True, check=True, timeout=15)
            help_text = help_result.stdout + help_result.stderr
            if "--model" not in help_text:
                raise ValueError("installed CLI does not support explicit model routing")
        if result["runtime"] == "pi":
            subprocess.run(["python3", "-c", LOCAL_MODEL_CHECK_SCRIPT, "openai-codex", "gpt-6-luna"], check=True, capture_output=True, text=True, timeout=20)
            result.update({"provider": "openai-codex", "catalog_source": "pi_offline_configured_catalog"})
        else:
            cache = json.loads(args.model_cache.read_text())
            model = next((m for m in cache.get("models", []) if m.get("slug") == "gpt-6-luna"), None)
            if not model or model.get("visibility") == "hide":
                raise ValueError("exact Luna model absent from local CLI catalog; never substitute another model")
            result["catalog_fetched_at"] = cache.get("fetched_at")
        result.update({"cli_model_catalog_match": True,
                       "status": "routing_ready_live_access_unverified", "model_calls_made": 0})
    except (ValueError, OSError, KeyError, subprocess.SubprocessError) as exc:
        parser.exit(2, str(exc) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
