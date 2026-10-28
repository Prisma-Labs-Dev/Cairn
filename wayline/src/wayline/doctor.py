"""Offline readiness and small per-instance setup; Pi owns all authentication."""
from __future__ import annotations

from importlib import metadata, resources
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tempfile

import yaml

from wayline import __version__
from wayline.dispatcher.config import DispatchConfig
from wayline.server.pi_viewer import binary_path

PI_INSTALL = 'npm install -g --ignore-scripts @earendil-works/pi-coding-agent@0.78.0'
PI_AUTH = 'Run pi, use /login to choose a subscription or API-key provider, then /model to select a model. Native provider environment variables also work. Wayline does not copy or store credentials.'


def _native_output(argv, extra_env=None):
    # Native configuration may print warnings. Never include raw output in diagnostics.
    with tempfile.TemporaryFile() as output:
        result = subprocess.run(argv, stdout=output, stderr=subprocess.STDOUT, timeout=20,
                                env={**os.environ, **(extra_env or {}), 'PI_OFFLINE': '1', 'NO_COLOR': '1', 'FORCE_COLOR': '0'})
        output.seek(0)
        value = output.read(1024 * 1024 + 1)
    if result.returncode or len(value) > 1024 * 1024:
        raise ValueError('native Pi check failed or exceeded its output bound')
    return value.decode('utf-8', errors='replace')


def readiness(config_path: Path | None = None):
    checks, blocked = [], False
    try:
        version = metadata.version('wayline')
        checks.append({'name': 'package', 'status': 'ok', 'version': version})
    except metadata.PackageNotFoundError:
        blocked = True
        checks.append({'name': 'package', 'status': 'missing', 'action': 'Install the Wayline package before running it outside the checkout.'})
    root = resources.files('wayline')
    required = ['LICENSE', 'server/static/index.html', 'dispatcher/prompts/default/bootstrap.md',
                'dispatcher/prompts/default/bootstrap_conclude.md', 'dispatcher/prompts/default/reason.md',
                'dispatcher/prompts/default/explore.md', 'dispatcher/prompts/default/explore_conclude.md',
                'dispatcher/prompts/mock/bootstrap.md']
    missing = [name for name in required if not root.joinpath(name).is_file()]
    checks.append({'name': 'resources', 'status': 'missing' if missing else 'ok', 'missing': missing})
    blocked |= bool(missing)
    selected = {'pi'}
    config = None
    if config_path is not None:
        try:
            config = DispatchConfig.load(config_path)
            selected = {worker.type for worker in config.workers} if config.runtime.execution == 'local' else set()
            checks.append({'name': 'configuration', 'status': 'ok', 'execution': config.runtime.execution,
                           'workers': len(config.workers)})
        except Exception:
            blocked = True
            checks.append({'name': 'configuration', 'status': 'invalid',
                           'action': 'Check the configuration schema and file path. No configuration values or secrets are printed.'})
    else:
        checks.append({'name': 'configuration', 'status': 'not_checked',
                       'action': 'Run wayline init --runtime-dir PATH, then wayline doctor --config PATH/dispatch.yaml.'})
    for kind in sorted(selected):
        executable = {'pi': 'pi', 'mock': 'python3', 'codex': 'codex', 'claudecode': 'claude'}[kind]
        configured = [w for w in config.workers if w.type == kind] if config else [None]
        paths = [w.env.get('PATH') if w else None for w in configured]
        found = next((binary for path in paths if (binary := shutil.which(executable, path=path))), None)
        if not found:
            blocked = True
            checks.append({'name': kind + '_cli', 'status': 'missing', 'action': PI_INSTALL if kind == 'pi' else f'Install {executable} and make it available on PATH.'})
            continue
        checks.append({'name': kind + '_cli', 'status': 'ok'})
        if kind != 'pi':
            continue
        try:
            workers = [w for w in config.workers if w.type == 'pi'] if config else [None]
            for worker in workers:
                selected_pi = shutil.which('pi', path=worker.env.get('PATH') if worker else None)
                if selected_pi is None:
                    blocked = True
                    checks.append({'name': 'pi_offline_catalog', 'status': 'unavailable', 'worker': worker.name if worker else None,
                                   'action': 'Make pi available on the configured worker PATH. ' + PI_INSTALL})
                    continue
                output = _native_output([selected_pi, '--offline', '--version'], extra_env=worker.env if worker else None)
                match = re.search(r'(?m)^([0-9]+\.[0-9]+\.[0-9]+)\s*$', output)
                version = match.group(1) if match else None
                catalog = _native_output([selected_pi, '--offline', '--no-extensions', '--no-skills',
                                          '--no-prompt-templates', '--no-context-files', '--list-models'],
                                         extra_env=worker.env if worker else None)
                models = set()
                for line in catalog.splitlines():
                    parts = line.split()
                    if len(parts) == 6 and parts[4] in ('yes', 'no') and parts[5] in ('yes', 'no') and re.fullmatch(r'[0-9]+(?:\.[0-9]+)?[KM]?', parts[2]):
                        models.add((parts[0], parts[1]))
                available = {(provider, model) for provider, model in models
                             if worker is None or not worker.provider or provider == worker.provider}
                matches = {(provider, model) for provider, model in available
                           if worker is not None and worker.model in (model, provider + '/' + model)}
                ready = len(matches) == 1 if worker is not None and worker.model else bool(available)
                blocked |= not ready
                checks.append({'name': 'pi_offline_catalog', 'status': 'ok' if ready else 'needs_native_setup',
                               'worker': worker.name if worker else None, 'environment_scope': 'configured_worker' if worker else 'host',
                               'version': version, 'available_models': len(available),
                               'explicit_selection_matches': len(matches) if worker and worker.model else None,
                               'unavailable_explicit_selections': int(bool(worker and worker.model) and len(matches) != 1),
                               'action': PI_AUTH, 'live_auth_verified': False})
        except (OSError, ValueError, subprocess.SubprocessError):
            blocked = True
            checks.append({'name': 'pi_offline_catalog', 'status': 'unavailable', 'action': PI_AUTH + ' Install Node 24 and the supported Pi CLI if needed.'})
    binary = binary_path()
    present = binary.is_file() and os.access(binary, os.X_OK)
    checks.append({'name': 'viewer', 'status': 'available' if present else 'optional_missing',
                   'required': False, 'action': 'From the source checkout run python3 scripts/build-pi-web.py. Core workflow execution does not require the transcript viewer.'})
    return {'status': 'needs_setup' if blocked else 'offline_checks_passed', 'version': __version__,
            'model_calls_made': 0, 'live_auth_verified': False,
            'authentication': PI_AUTH, 'checks': checks,
            'note': 'Offline catalog presence is not proof of a valid credential, live model access, subscription quota or billing. Use native Pi to check your chosen provider before dispatching.'}


def initialize(runtime_dir: Path):
    root = runtime_dir.expanduser().resolve()
    dispatch, environment = root / 'dispatch.yaml', root / 'runtime.env'
    if dispatch.exists() or environment.exists():
        raise FileExistsError('dispatch.yaml or runtime.env already exists; choose another runtime directory or keep the existing configuration')
    root.mkdir(parents=True, exist_ok=True)
    (root / 'workspaces').mkdir(exist_ok=True)
    config = {'server': 'http://127.0.0.1:8000',
              'runtime': {'execution': 'local', 'max_workers': 2, 'max_running_projects': 2, 'max_project_workers': 2,
                          'interval': 1, 'healthcheck_timeout': 15, 'worker_healthcheck': 'disabled', 'prompt_group': 'default'},
              'tasks': {'bootstrap': {'timeout': 120, 'conclude_timeout': 30}, 'reason': {'timeout': 120, 'max_intents': 2},
                        'explore': {'timeout': 300, 'conclude_timeout': 60}},
              'local': {'workspace_root': str(root / 'workspaces'), 'completed_action': 'keep'},
              'workers': [{'name': 'native-pi', 'type': 'pi', 'task_types': ['bootstrap', 'reason', 'explore'],
                           'max_running': 2, 'priority': 0}]}
    DispatchConfig.model_validate(config)
    values = {'WAYLINE_INSTANCE': root, 'WAYLINE_DB_PATH': root / 'wayline.sqlite',
              'WAYLINE_USAGE_OUTBOX': root / 'usage-outbox.sqlite', 'WAYLINE_SESSION_OUTBOX': root / 'session-outbox',
              'WAYLINE_PI_VIEWER_DIR': root / 'pi-viewer'}
    with dispatch.open('x') as target:
        target.write(yaml.safe_dump(config, sort_keys=False))
    with environment.open('x') as target:
        target.write('# Per-instance paths only. Pi retains its own credentials and model settings.\n')
        target.write(''.join(f'export {name}={shlex.quote(str(value))}\n' for name, value in values.items()))
    environment.chmod(0o600)
    return {'status': 'created', 'runtime_dir': str(root), 'config': str(dispatch), 'environment': str(environment),
            'model_calls_made': 0, 'credentials_managed': False,
            'commands': {'activate_each_shell': '. ' + shlex.quote(str(environment)), 'check': 'wayline doctor --config ' + shlex.quote(str(dispatch)),
                         'server': 'wayline serve', 'dispatcher': 'wayline dispatch --config ' + shlex.quote(str(dispatch))}}
