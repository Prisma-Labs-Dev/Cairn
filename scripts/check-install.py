#!/usr/bin/env python3
"""Build a fresh source snapshot and verify a wheel install with no model calls."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent


def request(url, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    with urlopen(Request(url, data=data, headers={'content-type': 'application/json'}), timeout=3) as response:
        return json.load(response)


def terminate(process):
    if process and process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=3)


def verify(output: Path, python: str):
    output.mkdir(parents=True, exist_ok=False)
    home, outside, snapshot = output / 'home', output / 'outside-checkout', output / 'source'
    for path in (home, outside, snapshot):
        path.mkdir()
    shutil.copytree(ROOT / 'wayline', snapshot / 'wayline', ignore=shutil.ignore_patterns('.venv', '__pycache__', '.pytest_cache', 'dist', '*.egg-info'))
    env = {'HOME': str(home), 'PATH': os.defpath, 'PYTHONNOUSERSITE': '1', 'UV_CACHE_DIR': str(output / 'uv-cache'),
           'XDG_CONFIG_HOME': str(home / '.config'), 'XDG_DATA_HOME': str(home / '.local/share'), 'TZ': 'UTC'}
    uv = shutil.which('uv')
    if not uv:
        raise RuntimeError('Install uv before running the installation check')
    commands = []
    log_path = output / 'install.log'
    def run(argv, *, capture=False):
        commands.append(argv)
        result = subprocess.run(argv, cwd=outside, env=env, capture_output=True, text=True)
        with log_path.open('a') as log:
            log.write(result.stdout + result.stderr)
        if result.returncode:
            raise RuntimeError('Installation check command failed: ' + argv[0] + '\n' + (result.stdout + result.stderr)[-5000:])
        return result.stdout if capture else None
    project = snapshot / 'wayline';dist = output / 'dist';venv = output / 'venv'
    run([uv, 'build', '--wheel', '--python', python, '--project', str(project), '--out-dir', str(dist)])
    wheel = next(dist.glob('wayline-*.whl'))
    requirements = output / 'requirements.txt'
    run([uv, 'export', '--frozen', '--project', str(project), '--no-dev', '--no-emit-project', '--format', 'requirements-txt', '--output-file', str(requirements)])
    run([uv, 'venv', '--python', python, str(venv)])
    executable = venv / 'bin/wayline';installed_python = venv / 'bin/python'
    # Exercise the installed entrypoint without activating its virtual environment.
    run([uv, 'pip', 'install', '--python', str(installed_python), '--require-hashes', '-r', str(requirements)])
    run([uv, 'pip', 'install', '--python', str(installed_python), '--no-index', '--no-deps', str(wheel)])
    location = Path(run([str(installed_python), '-c', 'import wayline; print(wayline.__file__)'], capture=True).strip())
    if not location.is_relative_to(venv) or location.is_relative_to(snapshot):
        raise RuntimeError('Installed import did not come from the isolated wheel environment')
    run([str(executable), '--help'])
    initialized = json.loads(run([str(executable), 'init', '--runtime-dir', str(output / 'instance')], capture=True))
    config_path = Path(initialized['config']);config = json.loads(run([str(installed_python), '-c', 'import json,yaml,sys;print(json.dumps(yaml.safe_load(open(sys.argv[1]))))', str(config_path)], capture=True))
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0));port = sock.getsockname()[1]
    base = 'http://127.0.0.1:' + str(port)
    config['server'] = base;config['runtime']['prompt_group'] = 'mock'
    config['workers'] = [{'name': 'install-fixture', 'type': 'mock', 'task_types': ['bootstrap', 'reason', 'explore'], 'max_running': 2, 'priority': 0,
                          'env': {key: json.dumps({'delay': [0, 0], 'outcomes': {name: int(name == chosen) for name in outcomes}}) for key, chosen, outcomes in
                                  (('MOCK_HEALTHCHECK', 'ok', ('ok', 'fail')),
                                   ('MOCK_BOOTSTRAP', 'complete', ('complete', 'fact', 'rejected', 'invalid_json', 'invalid_payload', 'command_fail')),
                                   ('MOCK_REASON', 'complete', ('complete', 'intent', 'noop', 'rejected', 'invalid_json', 'invalid_payload', 'command_fail')),
                                   ('MOCK_EXPLORE_EXECUTE', 'fact', ('fact', 'rejected', 'invalid_json', 'invalid_payload', 'command_fail')))}}]
    config_path.write_text(json.dumps(config, indent=2) + '\n')
    # Do not source arbitrary shell or inherit caller credentials. These are own paths only.
    instance = output / 'instance'
    env.update(WAYLINE_DB_PATH=str(instance / 'wayline.sqlite'), WAYLINE_USAGE_OUTBOX=str(instance / 'usage-outbox.sqlite'),
               WAYLINE_SESSION_OUTBOX=str(instance / 'session-outbox'), WAYLINE_PI_VIEWER_DIR=str(instance / 'pi-viewer'))
    readiness = json.loads(run([str(executable), 'doctor', '--config', str(config_path)], capture=True))
    if readiness['status'] != 'offline_checks_passed' or readiness['model_calls_made'] != 0:
        raise RuntimeError('Installed offline doctor did not pass the mock configuration')
    server = dispatcher = None
    try:
        with (output / 'server.log').open('w') as server_log, (output / 'dispatch.log').open('w') as dispatch_log:
            server = subprocess.Popen([str(executable), 'serve', '--host', '127.0.0.1', '--port', str(port), '--no-access-log'], cwd=outside, env=env, stdout=server_log, stderr=subprocess.STDOUT, start_new_session=True)
            deadline = time.monotonic() + 15
            while True:
                try:
                    request(base + '/projects');break
                except OSError:
                    if server.poll() is not None or time.monotonic() >= deadline:
                        raise RuntimeError('Installed API failed readiness')
                    time.sleep(.05)
            with urlopen(base + '/', timeout=3) as response:
                html = response.read().decode()
            if 'Wayline' not in html:
                raise RuntimeError('Packaged frontend was not served')
            static_paths = sorted(set(re.findall(r'(?:src|href)="(/static/[^\"]+)"', html)))
            for path in static_paths:
                with urlopen(base + path, timeout=3) as response:
                    if not response.read():raise RuntimeError('Empty packaged asset: ' + path)
            workflow = request(base + '/workflows', {'title': 'Installed package smoke', 'origin': 'Deterministic mock installation fixture. Artifact bytes are preseeded by the harness, not model output.',
                'stages': [{'id': 'prepare', 'goal': 'Complete the deterministic installation fixture'},
                           {'id': 'finish', 'depends_on': ['prepare'], 'goal': 'Collect the prepared installation artifact', 'artifacts': [{'path': 'proof.txt'}]}]})
            identifier = workflow['workflow']['id'];workspace = Path(config['local']['workspace_root']) / identifier
            workspace.mkdir(parents=True);content = b'Wayline installed-wheel mock fixture\n';(workspace / 'proof.txt').write_bytes(content)
            dispatcher = subprocess.Popen([str(executable), 'dispatch', '--config', str(config_path)], cwd=outside, env=env, stdout=dispatch_log, stderr=subprocess.STDOUT, start_new_session=True)
            deadline = time.monotonic() + 30
            while True:
                workflow = request(base + '/workflows/' + identifier)
                if workflow['workflow']['status'] == 'completed':break
                if dispatcher.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError('Installed deterministic workflow did not complete')
                time.sleep(.1)
            artifacts = workflow['stages'][-1]['collected_artifacts']
            if len(artifacts) != 1:raise RuntimeError('Installed workflow did not collect one fixture artifact')
            with urlopen(base + artifacts[0]['download_url'], timeout=3) as response:download = response.read()
            if download != content or artifacts[0]['sha256'] != hashlib.sha256(content).hexdigest():
                raise RuntimeError('Installed artifact bytes/hash differ from the prepared fixture')
            usage = request(base + '/usage/attempts?limit=100')
            if not usage['items'] or any(row['worker_type'] != 'mock' for row in usage['items']):
                raise RuntimeError('Installation smoke did not use mock-only invocations')
    finally:
        terminate(dispatcher);terminate(server)
    result = {'status': 'passed', 'model_calls_made': 0, 'fixture': 'deterministic mock workflow with preseeded artifact',
              'requested_python': python, 'python_version': run([str(installed_python), '-c', 'import sys; print(sys.version)'], capture=True).strip(),
              'wheel': wheel.name, 'wheel_sha256': hashlib.sha256(wheel.read_bytes()).hexdigest(),
              'installed_module': str(location), 'isolated_home': str(home), 'outside_checkout': str(outside),
              'static_assets_served': len(static_paths), 'workflow_status': workflow['workflow']['status'],
              'artifact_sha256': hashlib.sha256(content).hexdigest(), 'mock_invocations': len(usage['items']),
              'viewer_required': False, 'live_provider_auth_verified': False, 'commands': commands}
    (output / 'receipt.json').write_text(json.dumps(result, indent=2) + '\n')
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, help='New directory to retain logs and receipt; temporary by default')
    parser.add_argument('--python', default=sys.executable if (3, 12) <= sys.version_info[:2] < (3, 15) else '3.13',
                        help='Python 3.12–3.14 interpreter; defaults to the active supported Python, otherwise uv-managed 3.13')
    args = parser.parse_args()
    try:
        if args.output:
            result = verify(args.output.expanduser().resolve(), args.python)
        else:
            with tempfile.TemporaryDirectory(prefix='wayline-install-check-') as temporary:
                result = verify(Path(temporary) / 'check', args.python)
    except (OSError, ValueError, RuntimeError, StopIteration) as exc:
        parser.exit(1, str(exc) + '\n')
    print(json.dumps(result, indent=2))
