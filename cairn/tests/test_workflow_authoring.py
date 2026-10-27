"""Keep the agent-facing examples usable as the creation schema evolves."""
from pathlib import Path
import json
import re

from click.testing import CliRunner
import pytest
import yaml

from cairn import creation_cli
from cairn.cli import main
from cairn.dispatcher.contracts import validate_reason_payload
from cairn.server.models import CreateWorkflowRequest, RetryChoiceRequest


GUIDE = Path(__file__).resolve().parents[2] / 'docs' / 'workflow-authoring.md'
TEXT = GUIDE.read_text(encoding='utf-8')
YAML_DEFINITIONS = re.findall(r'```yaml\n(.*?)\n```', TEXT, re.S)
JSON_EXAMPLES = [json.loads(block) for block in re.findall(r'```json\n(.*?)\n```', TEXT, re.S)]


@pytest.mark.parametrize('definition', YAML_DEFINITIONS, ids=['minimal', 'required_parallel_join', 'ranked_choice'])
def test_authoring_yaml_is_a_complete_offline_creation_body(definition, monkeypatch):
    def no_create(*_args, **_kwargs):
        pytest.fail('A documented dry run must not create work or contact a server')

    monkeypatch.setattr(creation_cli, 'request_json', no_create)
    expected = CreateWorkflowRequest.model_validate(yaml.safe_load(definition))
    result = CliRunner().invoke(main, ['workflow', 'create', '--file', '-', '--dry-run'], input=definition)
    assert result.exit_code == 0, result.output
    response = json.loads(result.stdout)
    assert response == {'dry_run': True, 'resource': 'workflow', 'request': expected.model_dump(mode='json')}


def test_authoring_examples_keep_creation_and_worker_contracts_separate():
    assert len(YAML_DEFINITIONS) == 3
    minimal, parallel, choice = [CreateWorkflowRequest.model_validate(yaml.safe_load(block)) for block in YAML_DEFINITIONS]
    assert minimal.stages[0].id == 'stage_1'
    assert not minimal.stages[0].verification_enabled
    assert parallel.stages[-1].depends_on == ['hosts', 'services']
    assert parallel.stages[-1].verification_enabled
    assert choice.stages[0].expansion.mode == 'one'
    assert choice.stages[0].expansion.max_goals == 2
    assert choice.stages[-1].depends_on == ['choose_source']
    creation, completion, unavailable, retry = JSON_EXAMPLES
    assert CreateWorkflowRequest.model_validate(creation) == minimal
    kind, body = validate_reason_payload(completion, open_intents_empty=True, max_intents=3, expansion_max_goals=2)
    assert kind == 'complete'
    assert body['selected_goal'] in [goal['id'] for goal in body['goals']]
    assert body['selection_reason']
    # Verify generated output paths are valid and distinct when appended to the
    # initial definition, without creating a workflow or running a worker.
    CreateWorkflowRequest(stages=[*choice.stages, *[dict(goal, depends_on=['choose_source']) for goal in body['goals']]])
    assert validate_reason_payload(unavailable, open_intents_empty=True, max_intents=3)[0] == 'unavailable'
    assert RetryChoiceRequest.model_validate(retry).goal_id == body['selected_goal']
