from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from cairn.server import db
from cairn.server.app import app
from test_verification import reviewer, decide


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, '_db_path', None)
    db.configure(tmp_path / 'choices.db')
    with TestClient(app) as client:
        yield client


def create(client, *, verify=False):
    response = client.post('/workflows', json={'origin': 'Make breakfast. Pancakes preferred; eggs acceptable. Never use peanuts.', 'stages': [
        {'id': 'research', 'goal': 'Research and rank suitable breakfast options.', 'expansion': {'mode': 'one', 'max_goals': 3}, 'verification_enabled': verify},
        {'id': 'serve', 'goal': 'Serve the successful selected breakfast.', 'depends_on': ['research']},
    ]})
    assert response.status_code == 201, response.text
    return response.json()


def stages(client, workflow):
    return {s['id']: s for s in client.get('/workflows/' + workflow['workflow']['id']).json()['stages']}


def complete(client, project, **extra):
    return client.post(f'/projects/{project}/complete', json={'from': ['origin'], 'description': 'Evidence confirms goal achieved', 'worker': 'test', **extra})


def discover(client, workflow):
    return complete(client, workflow['stages'][0]['project_id'], goals=[
        {'id': 'pancakes', 'goal': 'Make peanut-free pancakes with an available sweet topping. Maple syrup preferred; honey is acceptable.'},
        {'id': 'eggs', 'goal': 'Make peanut-free eggs.'},
        {'id': 'benedict', 'goal': 'Make peanut-free eggs benedict.'}],
        selected_goal='pancakes', selection_reason='Pancakes meet the hard constraints and preference; honey can replace unavailable maple syrup.')


def unavailable(client, project, reason='The required ingredients are unavailable, so this breakfast goal cannot be achieved.'):
    return client.post(f'/projects/{project}/unavailable', json={'from': ['origin'], 'description': reason, 'worker': 'test'})


def test_one_option_only_and_unchosen_never_cook(client):
    workflow = create(client)
    assert discover(client, workflow).status_code == 200
    assert discover(client, workflow).status_code == 200
    current = stages(client, workflow)
    assert len(current) == 5
    assert current['pancakes']['status'] == 'active'
    assert current['research']['choice_satisfied'] is False
    assert current['serve']['status'] == 'blocked'
    assert all(current[item]['project_id'] is None for item in ('eggs', 'benedict'))
    assert client.get(f"/projects/{current['pancakes']['project_id']}").json()['choice_option'] is True
    assert complete(client, current['pancakes']['project_id']).status_code == 200
    current = stages(client, workflow)
    assert current['research']['choice_satisfied'] is True
    assert current['eggs']['option_state'] == current['benedict']['option_state'] == 'skipped'
    assert current['serve']['status'] == 'active'
    assert complete(client, current['serve']['project_id']).status_code == 200
    summary = client.get('/workflows').json()[0]
    assert summary['status'] == 'completed' and summary['stage_count'] == summary['completed_stage_count'] == 3


def test_ranked_fallback_idempotency_pause_restart_and_trash(client, monkeypatch):
    workflow = create(client)
    discover(client, workflow)
    first = stages(client, workflow)['pancakes']['project_id']
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: unavailable(client, first), range(2)))
    assert [r.status_code for r in responses] == [200, 200]
    current = stages(client, workflow)
    assert current['pancakes']['option_state'] == 'unavailable' and current['pancakes']['completion'] is None
    assert current['eggs']['status'] == 'active' and current['benedict']['project_id'] is None
    assert complete(client, first).status_code == 403
    wid = workflow['workflow']['id']
    client.put(f'/workflows/{wid}/status', json={'status': 'stopped'})
    client.put(f'/workflows/{wid}/status', json={'status': 'active'})
    assert client.get(f'/projects/{first}').json()['project']['status'] == 'stopped'
    database = db._db_path
    monkeypatch.setattr(db, '_db_path', None)
    db.configure(database)
    assert stages(client, workflow)['eggs']['status'] == 'active'
    client.delete(f'/workflows/{wid}')
    assert unavailable(client, current['eggs']['project_id']).status_code == 409
    assert client.post(f'/workflows/{wid}/restore').status_code == 200
    # Trash route is verified separately below; restored choices still need Resume.
    assert client.get(f'/projects/{first}').json()['project']['status'] == 'stopped'


def test_exhaustion_requires_explicit_retry_and_preserves_old_attempt(client):
    workflow = create(client)
    discover(client, workflow)
    old = {}
    for option in ('pancakes', 'eggs', 'benedict'):
        old[option] = stages(client, workflow)[option]['project_id']
        assert unavailable(client, old[option]).status_code == 200
    wid = workflow['workflow']['id']
    current = stages(client, workflow)
    assert current['research']['choice_error'] and current['serve']['status'] == 'blocked'
    assert client.put(f'/workflows/{wid}/status', json={'status': 'active'}).status_code == 409
    url = f'/workflows/{wid}/choices/research/retry'
    assert client.post(url, json={'goal_id': 'eggs', 'reason': ' '}).status_code == 422
    response = client.post(url, json={'goal_id': 'eggs', 'reason': 'A new shop has confirmed eggs are in stock.'})
    assert response.status_code == 200, response.text
    assert response.json()['workflow']['status'] == 'stopped'
    assert stages(client, workflow)['eggs']['project_id'] is None
    assert client.put(f'/workflows/{wid}/status', json={'status': 'active'}).status_code == 200
    current = stages(client, workflow)
    assert current['eggs']['project_id'] != old['eggs']
    assert client.get(f"/projects/{old['eggs']}/export").status_code == 200
    assert client.get(f"/projects/{old['eggs']}/history").status_code == 200
    history = client.get(f"/projects/{current['eggs']['project_id']}/choice-history").json()['items']
    assert len(history) == 3
    assert client.get(history[0]['facts'][0]['url']).status_code == 200
    assert current['eggs']['option_history'][-1]['project_id'] == old['eggs']
    assert unavailable(client, old['eggs']).status_code == 409
    assert complete(client, old['eggs']).status_code == 403
    assert all(client.get(f'/projects/{pid}').json()['project']['status'] == 'stopped' for pid in old.values())
    assert complete(client, current['eggs']['project_id']).status_code == 200
    assert stages(client, workflow)['serve']['status'] == 'active'


def test_empty_choice_pauses_without_correction_loop(client):
    workflow = create(client)
    response = complete(client, workflow['stages'][0]['project_id'], goals=[], selection_reason='No safe ingredients are currently available.')
    assert response.status_code == 200
    wid = workflow['workflow']['id']
    detail = client.get(f'/workflows/{wid}').json()
    assert detail['workflow']['status'] == 'stopped'
    assert 'Create a revised workflow' in detail['stages'][0]['choice_error']
    assert detail['stages'][0]['expansion_recovery_attempts'] == 0
    summary = client.get('/workflows').json()[0]
    assert summary['completed_stage_count'] < summary['stage_count']
    assert detail['stages'][1]['project_id'] is None
    assert client.put(f'/workflows/{wid}/status', json={'status': 'active'}).status_code == 409


def test_selection_validation_is_atomic_and_constraints_immutable(client):
    workflow = create(client)
    pid = workflow['stages'][0]['project_id']
    for extra in ({}, {'selected_goal': 'absent', 'selection_reason': 'Choice'}, {'selected_goal': 'eggs'}):
        response = complete(client, pid, goals=[{'id': 'eggs', 'goal': 'Make peanut-free eggs.'}], **extra)
        assert response.status_code == 409
        assert len(stages(client, workflow)) == 2
    assert unavailable(client, pid).status_code == 409
    assert discover(client, workflow).status_code == 200
    current = stages(client, workflow)
    response = client.post(f"/projects/{current['pancakes']['project_id']}/unavailable", json={'from': [], 'description': 'No ingredients', 'worker': 'test'})
    assert response.status_code == 422
    assert current['pancakes']['goal'].startswith('Make peanut-free pancakes')
    assert current['research']['completion']['selected_goal'] == 'pancakes'


def test_verification_approval_gates_choice_and_switch_races(client):
    workflow = create(client, verify=True)
    pid = workflow['stages'][0]['project_id']
    proposals = {'goals': [{'id': 'pancakes', 'goal': 'Make peanut-free pancakes.'}, {'id': 'eggs', 'goal': 'Make peanut-free eggs.'}],
                 'selected_goal': 'pancakes', 'selection_reason': 'Preferred available option.', 'producer_agent_id': 'producer', 'producer_session_id': 'session'}
    claim = complete(client, pid, **proposals).json()['id']
    assert len(stages(client, workflow)) == 2
    assert decide(client, claim, reviewer(client, claim)).status_code == 200
    current = stages(client, workflow)
    chosen = current['pancakes']['project_id']
    claim = complete(client, chosen, producer_agent_id='producer', producer_session_id='new-session').json()['id']
    assert stages(client, workflow)['serve']['status'] == 'blocked'
    assert unavailable(client, chosen).status_code == 409
    identity = reviewer(client, claim)
    assert decide(client, claim, identity, 'rejected').status_code == 200
    assert stages(client, workflow)['pancakes']['option_state'] == 'selected'
    assert unavailable(client, chosen).status_code == 200
    assert decide(client, claim, identity).status_code in (403, 409)


@pytest.mark.parametrize('bootstrap', [False, True])
def test_dispatcher_selects_then_falls_back_through_both_worker_contracts(client, tmp_path, monkeypatch, bootstrap):
    import sys
    from cairn.dispatcher.config import LocalConfig
    from cairn.dispatcher.runtime.local_backend import LocalBackend
    from cairn.dispatcher.workers.adapters.mock import MockDriver
    from cairn.dispatcher.workers.base import DriverResult
    from cairn.dispatcher.tasks import bootstrap as bootstrap_task, reason
    from test_mock_end_to_end import InProcessClient, _config, _dispatch_and_wait, _loop, _phase

    class ChoiceClient(InProcessClient):
        def complete(self, project_id, from_ids, description, worker, **extra):
            return self._post(f'/projects/{project_id}/complete', {'from': from_ids, 'description': description, 'worker': worker, **extra})
        def unavailable(self, project_id, from_ids, description, worker):
            return self._post(f'/projects/{project_id}/unavailable', {'from': from_ids, 'description': description, 'worker': worker})

    class Driver(MockDriver):
        def build_execute(self, worker, prompt, session):
            script = '''
import json,sys
context=json.loads(sys.argv[1])
phase=context['phase']
if context.get('choice_option'):
    result={'description':'Required ingredients for this selected option are confirmed unavailable.'}
    key='unavailable'
else:
    result={'description':'Confirmed current goal achieved.'}
    key='complete'
    if context.get('expansion'):
        result.update(goals=[{'id':'pancakes','goal':'Make peanut-free pancakes.','bootstrap_enabled':phase=='bootstrap'},{'id':'eggs','goal':'Make peanut-free eggs.','bootstrap_enabled':phase=='bootstrap'}],selected_goal='pancakes',selection_reason='Pancakes preferred, eggs are the next alternative.')
if phase=='bootstrap':
    data={'fact':{'description':'Observed local fixture evidence'},key:result}
else:
    result['from']=[context['fact_ids'][-1]]
    data={key:result}
print(json.dumps({'accepted':True,'data':data}))
'''
            return DriverResult(argv=[sys.executable, '-c', script, prompt], session=session)

    for module in (bootstrap_task, reason):
        monkeypatch.setattr(module, 'get_driver', lambda *_: Driver())
    workflow = client.post('/workflows', json={'stages': [
        {'id': 'research', 'goal': 'Research breakfast options', 'bootstrap_enabled': bootstrap, 'expansion': {'mode': 'one', 'max_goals': 2}},
        {'id': 'serve', 'goal': 'Serve breakfast', 'depends_on': ['research']},
    ]}).json()
    config = _config(bootstrap=_phase('complete'), reason=_phase('complete', zero_outcomes=['intent']), explore=_phase('fact'))
    config.runtime.execution = 'local'
    loop = _loop(config, ChoiceClient(client), LocalBackend(LocalConfig(workspace_root=str(tmp_path / 'runs'))))
    try:
        _dispatch_and_wait(loop)
        current = stages(client, workflow)
        assert current['pancakes']['status'] == 'active' and current['eggs']['project_id'] is None
        for _ in range(8):
            _dispatch_and_wait(loop)
            current = stages(client, workflow)
            if current['research']['choice_error']:
                break
        assert current['pancakes']['option_state'] == current['eggs']['option_state'] == 'unavailable'
        assert current['serve']['project_id'] is None
        assert current['pancakes']['completion'] is current['eggs']['completion'] is None
        assert 'unavailable' in current['research']['choice_error']
    finally:
        loop.close()


def test_milk_and_honey_are_required_parallel_work_not_alternatives(client):
    workflow = client.post('/workflows', json={'stages': [
        {'id': 'ingredients', 'goal': 'Identify required available ingredients for pancakes; honey may replace preferred maple syrup.', 'expansion': {'mode': 'all'}},
        {'id': 'cook', 'goal': 'Make pancakes with milk and honey, without peanuts.', 'depends_on': ['ingredients']},
    ]}).json()
    pid = workflow['stages'][0]['project_id']
    assert complete(client, pid, goals=[{'id': 'milk', 'goal': 'Fetch milk from shop A.'}, {'id': 'honey', 'goal': 'Fetch honey from shop B.'}]).status_code == 200
    current = stages(client, workflow)
    assert current['milk']['status'] == current['honey']['status'] == 'active'
    assert complete(client, current['milk']['project_id']).status_code == 200
    assert stages(client, workflow)['cook']['status'] == 'blocked'
    assert complete(client, current['honey']['project_id']).status_code == 200
    assert stages(client, workflow)['cook']['status'] == 'active'


def test_selected_option_approval_alone_unlocks_successor(client):
    workflow = create(client, verify=True)
    pid = workflow['stages'][0]['project_id']
    claim = complete(client, pid, goals=[{'id': 'eggs', 'goal': 'Make peanut-free eggs.'}, {'id': 'benedict', 'goal': 'Make suitable benedict.'}],
                     selected_goal='eggs', selection_reason='Eggs are feasible.', producer_agent_id='producer', producer_session_id='research-session').json()['id']
    assert decide(client, claim, reviewer(client, claim)).status_code == 200
    chosen = stages(client, workflow)['eggs']['project_id']
    claim = complete(client, chosen, producer_agent_id='producer', producer_session_id='cook-session').json()['id']
    current = stages(client, workflow)
    assert current['serve']['project_id'] is None and current['research']['choice_satisfied'] is False
    assert decide(client, claim, reviewer(client, claim)).status_code == 200
    current = stages(client, workflow)
    assert current['serve']['status'] == 'active'
    assert current['benedict']['option_state'] == 'skipped' and current['benedict']['project_id'] is None


def test_optional_failed_evidence_is_scoped_and_paged(client):
    first = create(client)
    second = create(client)
    discover(client, first)
    discover(client, second)
    source = stages(client, first)['pancakes']['project_id']
    assert unavailable(client, source).status_code == 200
    selected = stages(client, first)['eggs']['project_id']
    other = stages(client, second)['pancakes']['project_id']
    page = client.get(f'/projects/{selected}/choice-history?limit=1').json()
    assert page['total'] == 1 and page['items'][0]['project_id'] == source
    assert client.get(page['items'][0]['facts'][0]['url']).status_code == 200
    assert client.get(f'/projects/{other}/choice-history/{source}/facts/origin').status_code == 404
    assert client.get(f'/projects/{selected}/choice-history?limit=101').status_code == 422


def test_revised_all_expansion_definition_removes_generated_dependencies(client):
    import json
    from pathlib import Path
    import shutil
    import subprocess
    from cairn.server.models import CreateWorkflowRequest

    if not shutil.which('node'):
        pytest.skip('Node is required to execute UI definition-prefill regression')
    workflow = client.post('/workflows', json={'origin': 'Original editable context', 'stages': [
        {'id': 'discover', 'goal': 'Discover required work', 'expansion': {'mode': 'all', 'max_goals': 2}},
        {'id': 'join', 'goal': 'Combine required results', 'depends_on': ['discover'], 'verification_enabled': True, 'artifacts': [{'path': 'final.txt'}]},
    ]}).json()
    complete(client, workflow['stages'][0]['project_id'], goals=[{'id': 'alpha', 'goal': 'Investigate A'}, {'id': 'beta', 'goal': 'Investigate B'}])
    detail = client.get('/workflows/' + workflow['workflow']['id']).json()
    html = (Path(__file__).parents[1] / 'src/cairn/server/static/index.html').read_text()
    script = html[html.index('function cairnApp()'):].split('</script>')[0]
    node = "const p=JSON.parse(require('fs').readFileSync(0,'utf8')); eval(p.script); const a=cairnApp(); a.workflow=p.workflow; a.reviseWorkflowDefinition(); console.log(JSON.stringify(a.newWorkflow));"
    result = subprocess.run(['node', '-e', node], input=json.dumps({'script': script, 'workflow': detail}), text=True, capture_output=True, check=True)
    form = json.loads(result.stdout)
    assert len(form['stages']) == 2 and form['stages'][1]['depends_on'] == ['discover']
    for stage in form['stages']:
        enabled = stage.pop('expansion_enabled')
        mode, maximum = stage.pop('expansion_mode'), stage.pop('max_goals')
        if enabled:
            stage['expansion'] = {'mode': mode, 'max_goals': maximum}
        if not stage['layer']:
            del stage['layer']
    request = CreateWorkflowRequest.model_validate(form)
    assert request.origin == 'Original editable context'
    assert request.stages[0].expansion.mode == 'all'
    assert request.stages[1].verification_enabled is True and request.stages[1].artifacts[0].path == 'final.txt'
    assert client.post('/workflows', json=request.model_dump()).status_code == 201


def test_history_reader_choice_commands_fetch_only_requested_evidence(monkeypatch):
    from cairn.dispatcher import history_reader
    requested = []
    def fetch(url):
        requested.append(url)
        return {'id': 'f001', 'description': 'x' * 5000}
    monkeypatch.setattr(history_reader, 'fetch_json', fetch)
    config = {'server': 'http://fixture', 'project_id': 'current'}
    args = history_reader.parser().parse_args(['choice-list', '--limit', '2'])
    history_reader.execute(args, config)
    assert requested == ['http://fixture/projects/current/choice-history?offset=0&limit=2']
    args = history_reader.parser().parse_args(['choice-fact', 'old_project', 'f001', '--max-chars', '10'])
    record = history_reader.execute(args, config)
    assert requested[-1] == 'http://fixture/projects/current/choice-history/old_project/facts/f001'
    assert len(record['description']) == 10 and record['text_window']['total_chars'] == 5000
