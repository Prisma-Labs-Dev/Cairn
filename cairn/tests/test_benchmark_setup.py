from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from cairn.server.models import CreateProjectRequest, CreateWorkflowRequest, GeneratedGoalDefinition

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("benchmark_fixtures", ROOT / "benchmarks/bench.py")
bench = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bench)


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
