from __future__ import annotations

import json
from pathlib import Path
import shlex
import subprocess

from click.testing import CliRunner
import pytest
import yaml

from wayline.cli import main
from wayline import doctor
from wayline.dispatcher.config import DispatchConfig


def mock_config(path):
    value = doctor.initialize(path)
    config = yaml.safe_load(Path(value['config']).read_text())
    config['workers'][0]['type'] = 'mock'
    Path(value['config']).write_text(yaml.safe_dump(config))
    return Path(value['config'])


def test_init_creates_valid_native_pi_config_and_quoted_instance_paths_only(tmp_path, monkeypatch):
    native = tmp_path / 'native'; native.mkdir(); auth = native / 'auth.json'; auth.write_text('native-credential-sentinel')
    monkeypatch.setenv('PI_CODING_AGENT_DIR', str(native))
    root = tmp_path / "instance with space and ' quote"
    result = doctor.initialize(root)
    config = DispatchConfig.load(Path(result['config']))
    assert config.runtime.execution == 'local'
    assert config.workers[0].type == 'pi' and config.workers[0].model is None
    assert config.local.workspace_root == str(root / 'workspaces')
    script = '. ' + shlex.quote(result['environment']) + '; printf "%s" "$WAYLINE_DB_PATH"'
    output = subprocess.check_output(['/bin/sh', '-c', script], text=True)
    assert output == str(root / 'wayline.sqlite')
    assert auth.read_text() == 'native-credential-sentinel'
    assert 'credential-sentinel' not in Path(result['environment']).read_text()
    assert result['credentials_managed'] is False and result['model_calls_made'] == 0
    with pytest.raises(FileExistsError):
        doctor.initialize(root)


def test_mock_readiness_does_not_require_pi_or_optional_viewer(tmp_path, monkeypatch):
    config = mock_config(tmp_path / 'runtime')
    monkeypatch.setattr(doctor.shutil, 'which', lambda name, **kwargs: '/fixture/python3' if name == 'python3' else None)
    monkeypatch.setattr(doctor, 'binary_path', lambda: tmp_path / 'viewer-not-built')
    report = doctor.readiness(config)
    assert report['status'] == 'offline_checks_passed'
    assert report['live_auth_verified'] is False
    assert next(row for row in report['checks'] if row['name'] == 'viewer')['status'] == 'optional_missing'


def test_missing_pi_reports_native_setup_not_custom_auth(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor.shutil, 'which', lambda _, **kwargs: None)
    monkeypatch.setattr(doctor, 'binary_path', lambda: tmp_path / 'missing')
    result = CliRunner().invoke(main, ['doctor'])
    assert result.exit_code == 1
    report = json.loads(result.output)
    assert report['model_calls_made'] == 0 and report['live_auth_verified'] is False
    assert '/login' in report['authentication'] and '/model' in report['authentication']
    assert next(row for row in report['checks'] if row['name'] == 'pi_cli')['action'] == doctor.PI_INSTALL


def test_catalog_checks_are_offline_and_never_auth_proof(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor.shutil, 'which', lambda _, **kwargs: '/fixture/pi')
    monkeypatch.setattr(doctor, 'binary_path', lambda: tmp_path / 'missing')
    commands = []
    def native(argv, **kwargs):
        commands.append(argv)
        return '0.78.0\n' if '--version' in argv else 'native-provider model-name 128K 16K yes yes\n'
    monkeypatch.setattr(doctor, '_native_output', native)
    report = doctor.readiness()
    assert report['status'] == 'offline_checks_passed'
    assert report['live_auth_verified'] is False and report['model_calls_made'] == 0
    assert all('--offline' in command and '-p' not in command and '--print' not in command for command in commands)
    assert next(row for row in report['checks'] if row['name'] == 'pi_offline_catalog')['available_models'] == 1


def test_explicit_unavailable_model_never_substituted(tmp_path, monkeypatch):
    created = doctor.initialize(tmp_path / 'runtime');path = Path(created['config'])
    config = yaml.safe_load(path.read_text());config['workers'][0].update(provider='chosen', model='absent');path.write_text(yaml.safe_dump(config))
    monkeypatch.setattr(doctor.shutil, 'which', lambda _, **kwargs: '/fixture/pi')
    monkeypatch.setattr(doctor, '_native_output', lambda argv, **kwargs: '0.78.0\n' if '--version' in argv else 'chosen present 128K 16K yes yes\n')
    report = doctor.readiness(path)
    assert report['status'] == 'needs_setup'
    assert next(row for row in report['checks'] if row['name'] == 'pi_offline_catalog')['unavailable_explicit_selections'] == 1


def test_native_failure_and_invalid_secret_config_never_echo_raw_values(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor.shutil, 'which', lambda _, **kwargs: '/fixture/pi')
    def fail(argv, **kwargs):
        raise ValueError('secret-token-DO-NOT-PRINT')
    monkeypatch.setattr(doctor, '_native_output', fail)
    assert 'secret-token' not in json.dumps(doctor.readiness())
    config = tmp_path / 'invalid.yaml';config.write_text('unrecognized: secret-token-DO-NOT-PRINT\n')
    report = doctor.readiness(config)
    assert report['status'] == 'needs_setup'
    assert 'secret-token' not in json.dumps(report)


def test_ambiguous_unqualified_model_is_not_ready(tmp_path, monkeypatch):
    created = doctor.initialize(tmp_path / 'runtime');path = Path(created['config'])
    config = yaml.safe_load(path.read_text());config['workers'][0]['model'] = 'shared';path.write_text(yaml.safe_dump(config))
    monkeypatch.setattr(doctor.shutil, 'which', lambda _, **kwargs: '/fixture/pi')
    monkeypatch.setattr(doctor, '_native_output', lambda argv, **kwargs: '0.78.0\n' if '--version' in argv else 'first shared 128K 16K yes yes\nsecond shared 128K 16K yes yes\n')
    report = doctor.readiness(path)
    assert report['status'] == 'needs_setup'
    assert next(row for row in report['checks'] if row['name'] == 'pi_offline_catalog')['explicit_selection_matches'] == 2


def test_configured_worker_native_environment_used_without_printing_secrets(tmp_path, monkeypatch):
    created = doctor.initialize(tmp_path / 'runtime');path = Path(created['config'])
    config = yaml.safe_load(path.read_text());config['common_env'] = {'PI_CODING_AGENT_DIR': '/fixture/native-settings'}
    config['workers'][0].update(model='configured', env={'OPENAI_API_KEY': 'secret-sentinel-value'})
    path.write_text(yaml.safe_dump(config));seen=[]
    monkeypatch.setattr(doctor.shutil, 'which', lambda _, **kwargs: '/fixture/pi')
    def native(argv, extra_env=None):
        if '--version' in argv:return '0.78.0\n'
        seen.append(extra_env)
        return 'native configured 128K 16K yes yes\n' if extra_env.get('OPENAI_API_KEY') else ''
    monkeypatch.setattr(doctor, '_native_output', native)
    report = doctor.readiness(path)
    assert report['status'] == 'offline_checks_passed'
    assert seen[0]['PI_CODING_AGENT_DIR'] == '/fixture/native-settings'
    assert 'secret-sentinel-value' not in json.dumps(report)
    assert next(row for row in report['checks'] if row['name'] == 'pi_offline_catalog')['environment_scope'] == 'configured_worker'


def test_pi_available_only_on_configured_worker_path_is_checked(tmp_path, monkeypatch):
    created = doctor.initialize(tmp_path / 'runtime');path = Path(created['config'])
    config = yaml.safe_load(path.read_text());config['workers'][0]['env'] = {'PATH': '/configured/bin'};path.write_text(yaml.safe_dump(config))
    monkeypatch.setattr(doctor.shutil, 'which', lambda name, path=None: '/configured/bin/pi' if path == '/configured/bin' else None)
    commands=[]
    def native(argv, extra_env=None):
        commands.append((argv,extra_env))
        return '0.78.0\n' if '--version' in argv else 'native model 128K 16K yes yes\n'
    monkeypatch.setattr(doctor, '_native_output', native)
    report=doctor.readiness(path)
    assert report['status']=='offline_checks_passed'
    assert all(command[0][0]=='/configured/bin/pi' and command[1]['PATH']=='/configured/bin' for command in commands)
