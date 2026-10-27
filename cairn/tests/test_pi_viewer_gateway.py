from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from cairn.server import db, pi_viewer
from cairn.server.app import app
from cairn.server import session_service


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv('CAIRN_PI_VIEWER_DIR', raising=False)
    db.configure(tmp_path/'cairn.db')
    return TestClient(app)


def test_reserved_routes_deny_worker_control_and_arbitrary_host_reads(client, monkeypatch):
    def unexpected():
        raise AssertionError('Rejected route must not start viewer')
    monkeypatch.setattr(pi_viewer, 'viewer_origin', unexpected)
    for method, path in [('POST', '/api/chat'), ('GET', '/api/commands'), ('GET', '/api/files?path=/etc/passwd'),
                         ('GET', '/api/models'), ('POST', '/api/new-session'), ('GET', '/api/update')]:
        assert client.request(method, path).status_code == 403
    assert client.get('/session?id=unknown.jsonl').status_code == 404
    assert client.get('/api/session?id='+str(uuid4())+'.jsonl').status_code == 404


def test_gateway_reserved_assets_precede_existing_static_mount(client, monkeypatch):
    calls=[]
    def proxy(path, request):
        from fastapi.responses import Response
        calls.append(path)
        return Response('upstream viewer asset', media_type='application/javascript')
    monkeypatch.setattr(pi_viewer, 'proxy', proxy)
    assert client.get('/static/assets/app-fixture.js').text == 'upstream viewer asset'
    assert calls == ['/static/assets/app-fixture.js']
    assert 'cytoscape' in client.get('/static/vendor/cytoscape.min.js').text.lower()
    assert '<title>Cairn' in client.get('/').text
    assert isinstance(client.get('/settings').json(), dict)


def test_storage_refuses_global_pi_configuration(client, monkeypatch):
    monkeypatch.setenv('CAIRN_PI_VIEWER_DIR', str(Path.home()/'.pi/agent'))
    with pytest.raises(Exception) as caught:
        session_service.agent_dir()
    assert caught.value.status_code == 503


def test_storage_and_viewer_refuse_unrelated_sessions_and_symlinks(client, tmp_path):
    root=session_service.agent_dir()
    unrelated=root/'sessions/unrelated'
    unrelated.mkdir(parents=True)
    (unrelated/'personal.jsonl').write_text('{"private":"unrelated"}\n')
    with pytest.raises(Exception) as caught:
        pi_viewer.verify_scope(root)
    assert caught.value.status_code == 503
    (unrelated/'personal.jsonl').unlink(); unrelated.rmdir()
    managed=root/'sessions/cairn'; managed.mkdir()
    private=tmp_path/'personal.jsonl'; private.write_text('unrelated history')
    (managed/(str(uuid4())+'.jsonl')).symlink_to(private)
    with pytest.raises(Exception) as caught:
        pi_viewer.verify_scope(root)
    assert caught.value.status_code == 503
    identifier=str(uuid4())
    (managed/(identifier+'.jsonl')).symlink_to(private)
    with pytest.raises(Exception):
        session_service.conversation_file(identifier)
    assert private.read_text() == 'unrelated history'


def test_build_pins_source_and_never_installs_global_extension():
    root=Path(__file__).resolve().parents[2]
    script=(root/'scripts/build-pi-web.py').read_text()
    patch=(root/'vendor/pi-web/readonly.patch').read_text()
    assert pi_viewer.UPSTREAM_COMMIT in script
    assert "'ci', '--ignore-scripts'" in script
    assert 'cairnReadOnly(mux)' in patch
    assert 'ChatSender:          nil' in patch
    assert 'warmModelsCache()' in patch
    assert '{{.ServiceWorker}}' in patch and 'does not register a service worker' in patch
    assert 'return false // Cairn invocation records' in patch


def test_viewer_sse_closes_upstream_when_browser_disconnects(client, monkeypatch):
    import anyio
    from fastapi import Request
    import threading
    class Upstream:
        headers={'content-type':'text/event-stream'}
        status_code=200
        def __init__(self): self.closed=threading.Event()
        def iter_lines(self, chunk_size):
            yield b':ok'
            while not self.closed.wait(.01):
                yield b': heartbeat'
        def close(self): self.closed.set()
    upstream=Upstream()
    calls=[]
    def connect(*args, **kwargs):
        calls.append(kwargs)
        return upstream
    monkeypatch.setattr(pi_viewer, 'viewer_origin', lambda:'http://fixture')
    monkeypatch.setattr(pi_viewer.requests, 'request', connect)
    async def exercise():
        disconnected, observed, done = anyio.Event(), anyio.Event(), anyio.Event()
        scope={'type':'http','method':'GET','path':'/events','query_string':b'id=__all__',
               'headers':[], 'asgi':{'version':'3.0','spec_version':'2.3'}}
        response=pi_viewer.proxy('/events',Request(scope))
        async def receive():
            await disconnected.wait()
            return {'type':'http.disconnect'}
        async def send(message):
            if message.get('body') == b': heartbeat\n': observed.set()
        async def serve():
            try: await response(scope,receive,send)
            finally: done.set()
        async with anyio.create_task_group() as group:
            group.start_soon(serve)
            with anyio.fail_after(1): await observed.wait()
            disconnected.set()
            with anyio.fail_after(1): await done.wait()
        assert upstream.closed.is_set()
    anyio.run(exercise)
    assert calls[0]['timeout'] == (2,10)


def test_stalled_viewer_sse_read_timeout_is_a_clean_disconnect(client, monkeypatch):
    import anyio
    from fastapi import Request
    import requests
    class Upstream:
        headers={'content-type':'text/event-stream'}
        status_code=200
        closed=False
        def iter_lines(self, chunk_size):
            yield b':ok'
            raise requests.ReadTimeout('fixture idle timeout')
        def close(self): self.closed=True
    upstream=Upstream()
    monkeypatch.setattr(pi_viewer,'viewer_origin',lambda:'http://fixture')
    monkeypatch.setattr(pi_viewer.requests,'request',lambda *a,**k:upstream)
    scope={'type':'http','method':'GET','path':'/events','query_string':b'id=__all__','headers':[]}
    async def exercise():
        response=pi_viewer.proxy('/events',Request(scope))
        assert [part async for part in response.body_iterator] == [b':ok\n']
        assert upstream.closed
    anyio.run(exercise)
