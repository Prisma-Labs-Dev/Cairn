"""Same-origin, read-only gateway to a pinned and patched pi-web child process."""
from __future__ import annotations

import atexit
import os
from pathlib import Path
import socket
import subprocess
import threading
import time
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
import requests
import anyio

from wayline.server.db import get_conn
from wayline.server.session_service import agent_dir

UPSTREAM_COMMIT = '4a77ea5d662b2ef4fa20563b90dac823581614b7'
ALLOWED = frozenset(('/session', '/events', '/api/session', '/api/sessions', '/api/settings',
    '/api/worker-status', '/api/annotations', '/api/chat/queue', '/api/projects', '/api/scratchpad', '/custom-themes.css'))
router = APIRouter(include_in_schema=False)
_lock = threading.Lock()
_process = None
_origin = None
_process_root = None


def binary_path():
    return Path(os.environ.get('WAYLINE_PI_WEB_BINARY', str(Path.home() / '.cache/wayline/pi-web' / UPSTREAM_COMMIT / 'pi-web'))).expanduser()


def stop_viewer():
    global _process, _origin, _process_root
    with _lock:
        if _process is not None and _process.poll() is None:
            _process.terminate()
            try:
                _process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                _process.kill(); _process.wait(timeout=2)
        _process, _origin, _process_root = None, None, None


atexit.register(stop_viewer)


def verify_scope(root):
    sessions = root/'sessions'
    if sessions.is_symlink():
        raise HTTPException(503, 'Viewer cannot follow a session directory symlink')
    sessions.mkdir(exist_ok=True, mode=0o700)
    with get_conn() as conn:
        allowed = {row[0]+'.jsonl' for row in conn.execute('SELECT id FROM agent_conversations')}
    for directory in sessions.iterdir():
        if directory.is_symlink() or directory.name != 'wayline' or not directory.is_dir():
            raise HTTPException(503, 'Viewer directory contains sessions outside Wayline scope')
        for source in directory.iterdir():
            if source.is_symlink() or not source.is_file() or source.name not in allowed:
                raise HTTPException(503, 'Viewer directory contains sessions outside Wayline scope')


def viewer_origin():
    global _process, _origin, _process_root
    root = agent_dir()
    verify_scope(root)
    with _lock:
        if _process is not None and _process.poll() is None:
            if _process_root != root:
                raise HTTPException(503, 'Viewer is attached to a different Wayline database')
            return _origin
        binary = binary_path()
        if not binary.is_file() or not os.access(binary, os.X_OK):
            raise HTTPException(503, 'Read-only Pi viewer is not built. Run scripts/build-pi-web.py, then reopen this session.')
        (root / 'sessions/wayline').mkdir(parents=True, exist_ok=True, mode=0o700)
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        # No worker auth/config environment or host HOME crosses into the viewer.
        environment = {'PATH': '/usr/bin:/bin', 'HOME': str(root), 'PI_CODING_AGENT_DIR': str(root), 'PI_WEB_DEV': '1'}
        with (root / 'viewer.log').open('ab') as log:
            _process = subprocess.Popen([str(binary), '-host', '127.0.0.1', '-p', str(port)], env=environment,
                                        cwd=root, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
        _origin, _process_root = f'http://127.0.0.1:{port}', root
        deadline = time.monotonic()+8
        while time.monotonic() < deadline:
            if _process.poll() is not None:
                raise HTTPException(503, 'Read-only Pi viewer exited; inspect its scoped viewer.log')
            try:
                response = requests.get(_origin+'/api/sessions?limit=1', timeout=.3)
                if response.status_code == 200 and response.headers.get('X-Wayline-Viewer') == 'read-only':
                    return _origin
            except requests.RequestException:
                pass
            time.sleep(.05)
        raise HTTPException(503, 'Read-only Pi viewer did not become ready')


def validate_viewer_request(path, request):
    if request.method not in ('GET', 'HEAD') or (path not in ALLOWED and not path.startswith('/static/assets/')):
        raise HTTPException(403, 'Wayline session viewer is read-only')
    identifier = request.query_params.get('id')
    if identifier and identifier != '__all__':
        try:
            if not identifier.endswith('.jsonl'):
                raise ValueError()
            conversation = str(UUID(identifier[:-6]))
        except ValueError:
            raise HTTPException(404, 'Unknown Wayline session')
        with get_conn() as conn:
            if not conn.execute('SELECT 1 FROM agent_conversations WHERE id=? AND pi_session_id IS NOT NULL', (conversation,)).fetchone():
                raise HTTPException(404, 'Unknown Wayline session')


def proxy(path: str, request: Request):
    validate_viewer_request(path, request)
    origin = viewer_origin()
    try:
        response = requests.request(request.method, origin+path, params=list(request.query_params.multi_items()),
                                    stream=True, timeout=(2, 10), allow_redirects=False)
    except requests.RequestException:
        raise HTTPException(503, 'Read-only Pi viewer is unavailable')
    headers = {key: value for key, value in response.headers.items()
               if key.lower() in ('content-type', 'cache-control', 'content-security-policy', 'x-wayline-viewer')}
    headers['X-Accel-Buffering'] = 'no'
    async def content():
        # An explicit async generator owns response.close on browser disconnect.
        # The upstream sends five-second comments; the finite read timeout also
        # bounds a stalled child without treating comments as worker activity.
        parts = iter(response.iter_lines(chunk_size=1) if path == '/events' else response.iter_content(chunk_size=65536))
        try:
            while True:
                part = await anyio.to_thread.run_sync(lambda: next(parts, None))
                if part is None:
                    break
                yield part+b'\n' if path == '/events' else part
        except requests.RequestException:
            # A stopped viewer or timed-out idle connection is an ordinary SSE
            # disconnect. EventSource reconnects; no fake activity is emitted.
            return
        finally:
            with anyio.CancelScope(shield=True):
                await anyio.to_thread.run_sync(response.close)
    return StreamingResponse(content(), status_code=response.status_code, headers=headers)


# These root paths are reserved for the unmodified upstream frontend. Wayline's
# existing /settings endpoint and dashboard root are deliberately not proxied.
@router.api_route('/session', methods=['GET', 'HEAD', 'POST', 'PUT', 'DELETE', 'PATCH'])
@router.api_route('/events', methods=['GET', 'HEAD', 'POST', 'PUT', 'DELETE', 'PATCH'])
@router.api_route('/custom-themes.css', methods=['GET', 'HEAD', 'POST', 'PUT', 'DELETE', 'PATCH'])
def exact(request: Request):
    return proxy(request.url.path, request)


@router.api_route('/api/{path:path}', methods=['GET', 'HEAD', 'POST', 'PUT', 'DELETE', 'PATCH'])
def api(path: str, request: Request):
    return proxy('/api/'+path, request)


@router.api_route('/static/assets/{path:path}', methods=['GET', 'HEAD', 'POST', 'PUT', 'DELETE', 'PATCH'])
def assets(path: str, request: Request):
    return proxy('/static/assets/'+path, request)
