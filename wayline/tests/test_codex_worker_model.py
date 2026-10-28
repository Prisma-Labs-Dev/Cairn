from __future__ import annotations

import pytest

from wayline.dispatcher.config import WorkerConfig
from wayline.dispatcher.workers.adapters.codex import CodexDriver


def worker(**overrides):
    return WorkerConfig.model_validate({"name": "luna", "type": "codex", "task_types": ["bootstrap", "reason", "explore"],
                                       "max_running": 2, "priority": 0, **overrides})


@pytest.mark.parametrize("local", [True, False])
def test_explicit_model_and_effort_are_used_for_execute_resume_and_health(local):
    configured = worker(model="gpt-6-luna", reasoning_effort="low", env={
        "CODEX_MODEL": "must-not-be-selected", "CODEX_BASE_URL": "http://api", "OPENAI_API_KEY": "unused"})
    driver = CodexDriver(local=local)
    commands = [driver.build_execute(configured, "prompt", None).argv,
                driver.build_conclude(configured, "prompt", "session-id")]
    for argv in commands:
        assert argv[argv.index("--model") + 1] == "gpt-6-luna"
        assert 'model_reasoning_effort="low"' in argv
        assert "must-not-be-selected" not in argv
        assert argv[-2:] == ["--", "prompt"]
    assert "model=gpt-6-luna" in driver.describe_health(configured)


def test_local_optional_selection_preserves_existing_host_default_behavior():
    driver = CodexDriver(local=True)
    for argv in [driver.build_execute(worker(), "prompt", None).argv,
                 driver.build_conclude(worker(), "prompt", "session-id")]:
        assert "--model" not in argv
        assert not any("model_reasoning_effort" in option for option in argv)


def test_container_model_field_does_not_require_legacy_model_env_in_driver():
    configured = worker(model="gpt-6-luna", env={"CODEX_BASE_URL": "http://api", "OPENAI_API_KEY": "unused"})
    argv = CodexDriver().build_execute(configured, "prompt", None).argv
    assert argv[argv.index("--model") + 1] == "gpt-6-luna"
    assert 'model_reasoning_effort="high"' in argv
