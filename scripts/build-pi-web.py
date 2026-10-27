#!/usr/bin/env python3
"""Build the pinned read-only viewer locally. Never runs package lifecycle hooks."""
from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import subprocess
import tempfile

PIN = '4a77ea5d662b2ef4fa20563b90dac823581614b7'
REPOSITORY = 'https://github.com/ygncode/pi-web.git'
ROOT = Path(__file__).resolve().parent.parent


def run(*arguments, cwd):
    subprocess.run(arguments, cwd=cwd, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path.home()/'.cache/cairn/pi-web'/PIN/'pi-web')
    args = parser.parse_args()
    for binary in ('git', 'npm', 'go'):
        if shutil.which(binary) is None:
            parser.error(f'{binary} must be installed before building the viewer')
    with tempfile.TemporaryDirectory(prefix='cairn-pi-web-build-') as temporary:
        source = Path(temporary)
        run('git', 'init', '-q', cwd=source)
        run('git', 'remote', 'add', 'origin', REPOSITORY, cwd=source)
        run('git', 'fetch', '--depth', '1', 'origin', PIN, cwd=source)
        run('git', 'checkout', '--detach', 'FETCH_HEAD', cwd=source)
        found = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=source, text=True).strip()
        if found != PIN:
            raise SystemExit('Upstream commit did not match the pinned viewer source')
        run('git', 'apply', '--check', str(ROOT/'vendor/pi-web/readonly.patch'), cwd=source)
        run('git', 'apply', str(ROOT/'vendor/pi-web/readonly.patch'), cwd=source)
        # Install only the frontend lockfile. Root postinstall would configure
        # autostart and must never run in Cairn's viewer build.
        run('npm', '--prefix', 'web', 'ci', '--ignore-scripts', cwd=source)
        run('npm', '--prefix', 'web', 'run', 'build', cwd=source)
        run('go', 'test', './internal/app', './internal/server', '-run', 'TestCairn', '-count=1', cwd=source)
        args.output = args.output.expanduser().resolve()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary_binary = args.output.with_suffix('.building')
        run('go', 'build', '-trimpath', '-ldflags', f'-X main.version=cairn-readonly-{PIN}', '-o', str(temporary_binary), './cmd/pi-web', cwd=source)
        temporary_binary.replace(args.output)
        print(f'Read-only viewer built: {args.output}')
        print(f'Pinned source: {REPOSITORY} at {PIN}')
        print('Cairn starts this child on demand; no services or Tailscale configuration were changed.')


if __name__ == '__main__':
    main()
