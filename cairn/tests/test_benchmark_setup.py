from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import yaml

from cairn.dispatcher.config import DispatchConfig
from cairn.server.models import CreateProjectRequest, CreateWorkflowRequest, GeneratedGoalDefinition

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("benchmark_fixtures", ROOT / "benchmarks/bench.py")
bench = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bench)
preflight_spec = importlib.util.spec_from_file_location("benchmark_preflight", ROOT / "benchmarks/preflight.py")
preflight = importlib.util.module_from_spec(preflight_spec)
preflight_spec.loader.exec_module(preflight)


@pytest.mark.parametrize("case", bench.CASES)
def test_generated_definitions_match_actual_project_workflow_and_expansion_models(case):
    _, _, final_goal = bench.fixture(case, "schema-test", "http://127.0.0.1:8765")
    standalone = CreateProjectRequest.model_validate({"origin": "same inputs", "goal": final_goal})
    workflow = CreateWorkflowRequest.model_validate(bench.workflow(case, "same inputs", final_goal))
    assert workflow.stages[-1].goal == standalone.goal
    assert workflow.stages[0].expansion.max_goals == 16
    assert workflow.stages[-1].depends_on == ["discover"]
    assert workflow.stages[0].artifacts[0].path != workflow.stages[-1].artifacts[0].path
    child = GeneratedGoalDefinition.model_validate({"id": "verify_item", "goal": "Verify the discovered item from source pages",
                                                   "artifacts": [{"path": "notes/checks/item.json"}]})
    assert "depends_on" not in child.model_dump()


def pi_config() -> dict:
    config = yaml.safe_load((ROOT / "benchmarks/dispatch.pi-luna.local.yaml").read_text())
    config["local"]["workspace_root"] = "/tmp/cairn-isolated-preflight-test"
    return config


def test_pi_subscription_preflight_checks_execute_and_resume_routing():
    result = preflight.verify(DispatchConfig.model_validate(pi_config()))
    assert result["runtime"] == "pi"
    assert result["routing_verified"]
    assert not result["live_model_access_verified"]


@pytest.mark.parametrize("field,value", [
    ("provider", "openai"), ("provider", None), ("model", "luna"),
    ("reasoning_effort", "medium"), ("task_types", ["explore"]),
])
def test_pi_subscription_preflight_rejects_incomplete_or_different_routing(field, value):
    config = pi_config()
    config["workers"][0][field] = value
    with pytest.raises(ValueError):
        preflight.verify(DispatchConfig.model_validate(config))
