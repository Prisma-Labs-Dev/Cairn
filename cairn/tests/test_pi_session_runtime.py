from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace
import uuid

import pytest
from pydantic import ValidationError

from cairn.dispatcher.config import DispatchConfig, LocalConfig, WorkerConfig
from cairn.dispatcher.runtime.artifact_files import MAX_SESSION_CHUNK_BYTES, read_session_chunk
from cairn.dispatcher.runtime.containers import ContainerManager
from cairn.dispatcher.runtime.local_backend import LocalBackend
from cairn.dispatcher.runtime.local_process import LocalProcess
from cairn.dispatcher.runtime.process import ManagedProcess
from cairn.dispatcher.usage_reporting import invocation_worker
from cairn.dispatcher.workers.adapters.pi import PiDriver
from conftest import make_config


def pi_worker(**changes):
    return WorkerConfig.model_validate({
        'name': 'pi-worker', 'type': 'pi', 'task_types': ['explore'], 'priority': 0, 'max_running': 1,
        **changes,
    })


def pi_env():
    return {'PI_MODEL': 'configured', 'PI_BASE_URL': 'https://example.invalid/v1',
            'PI_PROVIDER_API': 'openai-responses', 'PI_API_KEY': 'fixture-only'}


@pytest.mark.parametrize('local', [True, False])
def test_managed_pi_session_is_explicit_and_resume_keeps_same_source(local):
    driver = PiDriver(local=local)
    worker = pi_worker(env=pi_env())
    result = driver.build_execute(worker, 'inspect fixture', None)
    path = result.session
    assert path and path.startswith('cairn-sessions/') and path.endswith('.jsonl')
    assert str(uuid.UUID(Path(path).stem)) == Path(path).stem
    assert result.argv[result.argv.index('--session') + 1] == path
    resumed = driver.build_conclude(worker, 'summarize fixture', path)
    assert resumed[resumed.index('--session') + 1] == path
    assert driver.extract_session(path, '{"type":"session","id":"pi-header-id"}', '') == path
    assert driver.prepare_session() != path


def test_pi_explicit_local_selectors_and_usage_keep_requested_model():
    worker = pi_worker(provider='fixture-provider', model='missing-luna-id', reasoning_effort='high')
    argv = PiDriver(local=True).build_execute(worker, 'fixture', None).argv
    assert argv[argv.index('--provider') + 1] == 'fixture-provider'
    assert argv[argv.index('--model') + 1] == 'missing-luna-id'
    assert argv[argv.index('--thinking') + 1] == 'high'
    effective = invocation_worker(worker, argv)
    assert effective.model == 'missing-luna-id' and effective.reasoning_effort == 'high'
    # Host defaults remain defaults; container env never selects a local model.
    default = PiDriver(local=True).build_execute(pi_worker(env=pi_env()), 'fixture', None).argv
    assert '--provider' not in default and '--model' not in default and '--thinking' not in default


def test_pi_container_explicit_model_wins_in_argv_models_json_and_health(monkeypatch):
    worker = pi_worker(env=pi_env(), model='explicit-id', reasoning_effort='medium')
    driver = PiDriver()
    argv = driver.build_execute(worker, 'fixture', None).argv
    assert argv[argv.index('--model') + 1] == 'explicit-id'
    assert argv[argv.index('--thinking') + 1] == 'medium'
    models = json.loads(argv[5])
    provider = models['providers']['cairn']
    assert provider['models'][0]['id'] == 'explicit-id'
    assert provider['baseUrl'] == pi_env()['PI_BASE_URL']
    calls = []
    monkeypatch.setattr('cairn.dispatcher.workers.adapters.pi.http_ping', lambda *a, **kw: calls.append(kw))
    driver.check_health(worker, timeout=1)
    assert calls[0]['json_body']['model'] == 'explicit-id'


def test_pi_container_configuration_accepts_model_field_but_rejects_ignored_provider():
    payload = make_config().model_dump()
    payload['workers'] = [pi_worker(env={k: v for k, v in pi_env().items() if k != 'PI_MODEL'}, model='exact-id').model_dump()]
    assert DispatchConfig.model_validate(payload).workers[0].model == 'exact-id'
    payload['workers'][0]['provider'] = 'host-provider'
    with pytest.raises(ValidationError, match='provider overrides require local'):
        DispatchConfig.model_validate(payload)
    with pytest.raises(ValidationError, match='provider requires a pi'):
        WorkerConfig.model_validate({**payload['workers'][0], 'type': 'codex'})


def test_pi_local_wrapper_creates_managed_directory_before_harmless_shim(tmp_path):
    # A shell function avoids executing any installed Pi or model provider.
    worker = pi_worker()
    driver = PiDriver(local=True)
    command = driver.build_execute(worker, 'fixture', None)
    script = command.argv[2].replace('exec pi "$@"', 'test -d cairn-sessions && printf "%s" "$@"')
    result = subprocess.run(['/bin/sh', '-c', script, *command.argv[3:]], cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0 and (tmp_path / 'cairn-sessions').is_dir()
    assert command.session in result.stdout


def test_local_session_chunk_reads_appends_without_rereading_prefix(tmp_path):
    backend = LocalBackend(LocalConfig(workspace_root=str(tmp_path)))
    runtime = backend.ensure_running('proj_001')
    path = f'cairn-sessions/{uuid.uuid4()}.jsonl'
    source = Path(runtime) / path
    source.parent.mkdir()
    source.write_bytes(b'header\nfirst\n')
    assert backend.read_session_chunk(runtime, path, 0, 7) == b'header\n'
    assert backend.read_session_chunk(runtime, path, 7, 100) == b'first\n'
    with source.open('ab') as output:
        output.write(b'next\n')
    assert backend.read_session_chunk(runtime, path, 13, 100) == b'next\n'
    assert backend.read_session_chunk(runtime, path, 18, 100) == b''
    source.write_bytes(b'shrank')
    with pytest.raises(ValueError, match='shrank'):
        backend.read_session_chunk(runtime, path, 18, 100)


@pytest.mark.parametrize('path', ['../private.jsonl', '/tmp/private.jsonl', 'cairn-sessions/not-a-uuid.jsonl',
                                  'cairn-sessions/../private.jsonl'])
def test_session_chunk_rejects_unmanaged_paths(tmp_path, path):
    with pytest.raises(ValueError, match='managed'):
        read_session_chunk(tmp_path, path, 0, 100)


@pytest.mark.parametrize(('offset', 'limit'), [(-1, 10), (True, 10), (0, 0), (0, MAX_SESSION_CHUNK_BYTES + 1), (0, True)])
def test_session_chunk_rejects_invalid_bounds_before_open(tmp_path, offset, limit):
    with pytest.raises(ValueError):
        read_session_chunk(tmp_path, f'cairn-sessions/{uuid.uuid4()}.jsonl', offset, limit)


@pytest.mark.parametrize('parent_link', [True, False])
def test_session_chunk_rejects_symlinked_parent_or_file(tmp_path, parent_link):
    target = tmp_path / 'private'
    target.mkdir()
    identifier = str(uuid.uuid4())
    (target / f'{identifier}.jsonl').write_text('private')
    if parent_link:
        (tmp_path / 'cairn-sessions').symlink_to(target, target_is_directory=True)
    else:
        (tmp_path / 'cairn-sessions').mkdir()
        (tmp_path / 'cairn-sessions' / f'{identifier}.jsonl').symlink_to(target / f'{identifier}.jsonl')
    with pytest.raises(OSError):
        read_session_chunk(tmp_path, f'cairn-sessions/{identifier}.jsonl', 0, 100)


def test_docker_session_reader_executes_same_safe_slice_protocol(tmp_path):
    path = f'cairn-sessions/{uuid.uuid4()}.jsonl'
    source = tmp_path / path
    source.parent.mkdir()
    source.write_bytes(b'header\nfirst\n')
    calls = []
    class Container:
        def exec_run(self, argv, demux):
            calls.append((argv, demux))
            actual = subprocess.run([sys.executable, *argv[1:]], cwd=tmp_path, capture_output=True)
            return SimpleNamespace(exit_code=actual.returncode, output=(actual.stdout, actual.stderr))
    manager = ContainerManager.__new__(ContainerManager)
    manager._require_container = lambda _: Container()
    assert manager.read_session_chunk('runtime', path, 7, 5) == b'first'
    assert calls[0][1] is True and calls[0][0][-3:] == [path, '7', '5']
    source.unlink()
    with pytest.raises(FileNotFoundError, match='not been created'):
        manager.read_session_chunk('runtime', path, 0, 20)
    source.symlink_to('/etc/passwd')
    with pytest.raises(ValueError, match='unsafe'):
        manager.read_session_chunk('runtime', path, 0, 20)


def test_local_stdout_observer_receives_small_chunk_before_process_exits(tmp_path):
    observed = threading.Event()
    process = LocalProcess([sys.executable, '-c', "import sys,time; print('first', flush=True); time.sleep(1); print('last')"],
                           str(tmp_path), dict(os.environ), timeout_seconds=5)
    chunks = []
    def observer(chunk):
        chunks.append(chunk)
        observed.set()
    process.stdout_observer = observer
    process.start()
    assert observed.wait(.8)
    assert process._process.poll() is None
    result = process.communicate(5)
    assert ''.join(chunks) == result.stdout == 'first\nlast\n'


def test_local_stdout_observer_exception_keeps_unicode_and_exit(tmp_path):
    process = LocalProcess([sys.executable, '-c', "import os,time; os.write(1,b'\\xe2'); time.sleep(.1); os.write(1,b'\\x82\\xac\\n')"],
                           str(tmp_path), dict(os.environ), timeout_seconds=5)
    process.stdout_observer = lambda _: (_ for _ in ()).throw(ValueError('observer'))
    process.start()
    result = process.communicate(5)
    assert result.stdout == '€\n' and result.returncode == 0


def test_container_stdout_observer_preserves_split_unicode_and_result():
    class API:
        def exec_create(self, *args, **kwargs): return {'Id': 'exec-id'}
        def exec_start(self, *args, **kwargs): return iter([(b'first\n\xe2', b''), (b'\x82\xac\n', b'err')])
        def exec_inspect(self, *args): return {'ExitCode': 0}
    container = SimpleNamespace(id='fixture', client=SimpleNamespace(api=API()))
    process = ManagedProcess(container, ['fixture'], {})
    chunks = []
    process.stdout_observer = chunks.append
    process.start()
    result = process.communicate(5)
    assert ''.join(chunks) == result.stdout == 'first\n€\n'
    assert result.stderr == 'err' and result.returncode == 0


@pytest.mark.parametrize(('provider', 'model', 'catalog', 'expected'), [
    ('fixture', 'exact-id', 'fixture exact-id 100K 10K yes no\n', 0),
    ('fixture', 'missing-luna', 'fixture other-model 100K 10K yes no\n', 2),
    ('', 'ambiguous', 'one ambiguous 100K 10K yes no\ntwo ambiguous 100K 10K yes no\n', 2),
    ('fixture', 'fuzzy', 'fixture fuzzy-substitute 100K 10K yes no\n', 2),
    ('', 'fixture/exact-id', 'fixture exact-id 100K 10K yes no\n', 0),
])
@pytest.mark.parametrize('catalog_stream', ['stdout', 'stderr'])
def test_explicit_local_catalog_preflight_is_exact_and_never_calls_a_model(tmp_path, provider, model, catalog, expected, catalog_stream):
    from cairn.dispatcher.workers.adapters.pi import LOCAL_MODEL_CHECK_SCRIPT
    executable = tmp_path / 'pi'
    calls = tmp_path / 'calls'
    redirect = ' >&2' if catalog_stream == 'stderr' else ''
    executable.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$FIXTURE_CALLS"\nprintf "%s" "$FIXTURE_CATALOG"' + redirect + '\n')
    executable.chmod(0o755)
    env = {**os.environ, 'PATH': f'{tmp_path}{os.pathsep}{os.environ["PATH"]}',
           'FIXTURE_CALLS': str(calls), 'FIXTURE_CATALOG': catalog}
    result = subprocess.run([sys.executable, '-c', LOCAL_MODEL_CHECK_SCRIPT, provider, model],
                            env=env, capture_output=True, text=True)
    assert result.returncode == expected
    assert '--offline' in calls.read_text() and '--list-models' in calls.read_text()
    assert '--print' not in calls.read_text() and '--mode' not in calls.read_text()
    assert result.stdout == ''
    if expected:
        assert 'unavailable or ambiguous' in result.stderr


# This pure shim follows Pi 0.78 resolveCliModel's relevant precedence: all
# registered models participate, inferred provider prefixes precede raw IDs,
# and an explicit provider causes exactly one matching prefix to be stripped.
# Its only execution is catalog printing or pure resolution against fixture rows.
PURE_PI_RESOLVER_SHIM = r'''
import json, os, sys
args = sys.argv[1:]
with open(os.environ['FIXTURE_CALLS'], 'a') as target:
    target.write(json.dumps(args) + '\n')
if '--list-models' in args:
    print(os.environ['FIXTURE_CATALOG'], end='')
    sys.exit(0)
models = json.loads(os.environ['FIXTURE_REGISTRY'])
provider = args[args.index('--provider')+1] if '--provider' in args else None
model = args[args.index('--model')+1] if '--model' in args else None
if model is None:
    selected = next((row for row in models if provider is None or row['provider'] == provider), None)
else:
    providers = {row['provider'].lower(): row['provider'] for row in models}
    actual_provider = providers.get(provider.lower()) if provider else None
    pattern = model
    if not actual_provider and '/' in model:
        prefix, remainder = model.split('/', 1)
        if prefix.lower() in providers:
            actual_provider = providers[prefix.lower()]
            pattern = remainder
    if provider and actual_provider and model.lower().startswith((actual_provider+'/').lower()):
        pattern = model[len(actual_provider)+1:]
    selected = next((row for row in models
        if (actual_provider is None or row['provider'] == actual_provider)
        and pattern.lower() in (row['id'].lower(), (row['provider']+'/'+row['id']).lower())), None)
print(json.dumps({'selected': selected, 'argv': args}))
'''


MODEL_RESOLVER_CASES = [
    (None, 'anthropic/claude-sonnet-4-5',
     [('openrouter', 'anthropic/claude-sonnet-4-5')],
     [('anthropic', 'claude-sonnet-4-5'), ('openrouter', 'anthropic/claude-sonnet-4-5')],
     ('openrouter', 'anthropic/claude-sonnet-4-5'), 'openrouter/anthropic/claude-sonnet-4-5'),
    (None, 'openrouter/anthropic/claude-sonnet-4-5',
     [('openrouter', 'anthropic/claude-sonnet-4-5')],
     [('anthropic', 'claude-sonnet-4-5'), ('openrouter', 'anthropic/claude-sonnet-4-5')],
     ('openrouter', 'anthropic/claude-sonnet-4-5'), 'openrouter/anthropic/claude-sonnet-4-5'),
    ('openrouter', 'anthropic/claude-sonnet-4-5',
     [('openrouter', 'anthropic/claude-sonnet-4-5')],
     [('anthropic', 'claude-sonnet-4-5'), ('openrouter', 'anthropic/claude-sonnet-4-5')],
     ('openrouter', 'anthropic/claude-sonnet-4-5'), 'openrouter/anthropic/claude-sonnet-4-5'),
    (None, 'shared-model-id', [('fixture-codex', 'shared-model-id')],
     [('fixture-openai', 'shared-model-id'), ('fixture-codex', 'shared-model-id')],
     ('fixture-codex', 'shared-model-id'), 'fixture-codex/shared-model-id'),
    ('fixture-codex', 'shared-model-id',
     [('fixture-openai', 'shared-model-id'), ('fixture-codex', 'shared-model-id')],
     [('fixture-openai', 'shared-model-id'), ('fixture-codex', 'shared-model-id')],
     ('fixture-codex', 'shared-model-id'), 'fixture-codex/shared-model-id'),
    (None, 'openrouter/model-x', [('openrouter', 'openrouter/model-x')],
     [('openrouter', 'model-x'), ('openrouter', 'openrouter/model-x')],
     ('openrouter', 'openrouter/model-x'), 'openrouter/openrouter/openrouter/model-x'),
]


def run_model_wrapper(tmp_path, *, provider, model, available, registry, conclude=False):
    executable = tmp_path / 'pi'
    executable.write_text('#!' + sys.executable + '\n' + PURE_PI_RESOLVER_SHIM)
    executable.chmod(0o755)
    calls = tmp_path / 'resolver-calls.jsonl'
    catalog = ''.join(f'{p} {m} 200K 64K yes yes\n' for p, m in available)
    rows = [{'provider': p, 'id': m} for p, m in registry]
    env = {**os.environ, 'PATH': f'{tmp_path}{os.pathsep}{os.environ["PATH"]}',
           'FIXTURE_CALLS': str(calls), 'FIXTURE_CATALOG': catalog, 'FIXTURE_REGISTRY': json.dumps(rows)}
    selected = pi_worker(provider=provider, model=model, reasoning_effort='high')
    driver = PiDriver(local=True)
    session = driver.prepare_session()
    prompt = 'Fixture prompt with "quotes", $(never-run), --model text, and\na second line.'
    argv = driver.build_conclude(selected, prompt, session) if conclude else driver.build_execute(selected, prompt, session).argv
    # Execute the generated production shell body with a controlled non-login
    # PATH, so a host profile cannot resolve the installed real Pi instead.
    result = subprocess.run([argv[0], '-c', *argv[2:]], cwd=tmp_path, env=env, capture_output=True, text=True)
    recorded = [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []
    return result, recorded, session, prompt


@pytest.mark.parametrize(('provider', 'model', 'available', 'registry', 'expected', 'reference'), MODEL_RESOLVER_CASES)
@pytest.mark.parametrize('conclude', [False, True])
def test_local_model_execution_uses_the_exact_catalog_pair_and_preserves_arguments(tmp_path, provider, model, available, registry, expected, reference, conclude):
    result, calls, session, prompt = run_model_wrapper(tmp_path, provider=provider, model=model,
                                                      available=available, registry=registry, conclude=conclude)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert (payload['selected']['provider'], payload['selected']['id']) == expected
    argv = payload['argv']
    assert argv[:4] == ['--provider', expected[0], '--model', reference]
    assert argv.count('--provider') == argv.count('--model') == 1
    assert argv[argv.index('--session')+1] == session
    assert argv[argv.index('--thinking')+1] == 'high'
    assert argv[-2:] == ['-p', prompt]
    assert len(calls) == 2 and '--list-models' in calls[0] and '--mode' in calls[1]


@pytest.mark.parametrize(('model', 'available', 'registry'), [
    ('shared-model-id', [('one', 'shared-model-id'), ('two', 'shared-model-id')],
     [('one', 'shared-model-id'), ('two', 'shared-model-id')]),
    ('missing-luna', [('fixture', 'available-model')], [('fixture', 'available-model')]),
    ('fuzzy', [('fixture', 'fuzzy-substitute')], [('fixture', 'fuzzy-substitute')]),
])
def test_unavailable_ambiguous_or_fuzzy_model_never_reaches_execution(tmp_path, model, available, registry):
    result, calls, _, _ = run_model_wrapper(tmp_path, provider=None, model=model, available=available, registry=registry)
    assert result.returncode == 2 and 'unavailable or ambiguous' in result.stderr
    assert result.stdout == '' and len(calls) == 1 and '--list-models' in calls[0]


def test_host_default_model_does_not_run_catalog_or_add_model_selectors(tmp_path):
    result, calls, _, _ = run_model_wrapper(tmp_path, provider=None, model=None, available=[], registry=[('host-default', 'default-model')])
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload['selected'] == {'provider': 'host-default', 'id': 'default-model'}
    assert '--provider' not in payload['argv'] and '--model' not in payload['argv']
    assert len(calls) == 1 and '--list-models' not in calls[0]


def test_explicit_pairs_match_installed_pure_pi_resolver_when_available():
    import shutil
    pi_binary, node_binary = shutil.which('pi'), shutil.which('node')
    if not pi_binary or not node_binary:
        pytest.skip('Optional installed pure Pi resolver is unavailable')
    resolver = Path(pi_binary).resolve().parent / 'core/model-resolver.js'
    if not resolver.is_file():
        pytest.skip('Installed Pi does not expose its pure resolver module')
    cases = [{'provider': expected[0], 'reference': reference,
              'models': [{'provider': p, 'id': m} for p, m in registry], 'expected': list(expected)}
             for _, _, _, registry, expected, reference in MODEL_RESOLVER_CASES]
    script = '''
const { resolveCliModel } = await import(process.argv[1]);
const cases = JSON.parse(process.argv[2]);
const results = cases.map(c => {
  const actual = resolveCliModel({ cliProvider: c.provider, cliModel: c.reference,
    modelRegistry: { getAll: () => c.models } });
  return actual.model ? [actual.model.provider, actual.model.id] : actual.error;
});
process.stdout.write(JSON.stringify(results));
'''
    result = subprocess.run([node_binary, '--input-type=module', '-e', script, resolver.as_uri(), json.dumps(cases)],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [case['expected'] for case in cases]
