"""Capture real Pi files/events before runtime cleanup, with a durable retry spool.

The metrics ledger never receives message content. This separate store contains
only explicitly managed Pi conversations and their genuine JSON stdout events.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import threading
import time
from uuid import UUID, uuid4

from cairn.dispatcher.usage_reporting import PROCESS_INSTANCE, _claim_owner
from cairn.server.session_models import CHUNK_BYTES, SESSION_BYTES, EVENT_BYTES

LOG = logging.getLogger(__name__)
SESSION_PATH = re.compile(r'^cairn-sessions/([0-9a-f-]{36})\.jsonl$')


def conversation_from_argv(argv):
    for index, argument in enumerate(argv[:-1]):
        if argument == '--session':
            match = SESSION_PATH.fullmatch(argv[index+1])
            if match:
                try:
                    return str(UUID(match.group(1))), argv[index+1]
                except ValueError:
                    pass
    return None


def _now():
    return datetime.now(timezone.utc).isoformat()


def _write_json(path, data):
    temporary = path.with_suffix('.tmp')
    with temporary.open('w', encoding='utf-8') as target:
        os.chmod(temporary, 0o600)
        target.write(json.dumps(data, ensure_ascii=True))
        target.flush(); os.fsync(target.fileno())
    temporary.replace(path)


class SessionOutbox:
    def __init__(self, client):
        server = str(getattr(client, '_base_url', 'in-process'))
        key = hashlib.sha256(server.encode()).hexdigest()
        base = Path(os.environ.get('CAIRN_SESSION_OUTBOX', str(Path.home()/'.local/share/cairn/session-outbox'))).expanduser()
        self.root = (base/key).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        _claim_owner(self.root/'owner')
        self.client = client

    def flush(self, metadata_path: Path, *, seconds=2):
        try:
            envelope = json.loads(metadata_path.read_text())
            metadata = envelope['invocation']
            if metadata['status'] == 'running' and envelope['owner'] != PROCESS_INSTANCE:
                metadata.update(status='interrupted', finished_at=_now(),
                                capture_status='unavailable', source_error='Dispatcher ended before final session capture')
                _write_json(metadata_path, envelope)
            files = (self.root/f'{metadata["conversation_id"]}.session', self.root/f'{metadata["id"]}.events')
            fingerprint = hashlib.sha256(json.dumps({'invocation': metadata, 'results': envelope.get('results'),
                'sizes': [path.stat().st_size if path.exists() else 0 for path in files]}, sort_keys=True).encode()).hexdigest()
            receipt = metadata_path.with_suffix('.ack')
            if receipt.exists() and receipt.read_text() == fingerprint:
                return True
            # Process/source completion can precede transport delivery. Register
            # identity and its true process state first, but keep delivery pending
            # until both raw and event byte targets have been acknowledged.
            transport = {**metadata, 'delivery_status': 'pending',
                         'delivery_session_bytes': files[0].stat().st_size if files[0].exists() else 0,
                         'delivery_event_bytes': files[1].stat().st_size if files[1].exists() else 0}
            response = self.client.record_session_invocation(transport)
            if not response.ok or not isinstance(response.data, dict):
                return False
            acknowledged = response.data
            deadline = time.monotonic()+seconds
            complete = True
            for events, path, offset_key in (
                (False, self.root/f'{metadata["conversation_id"]}.session', 'bytes_captured'),
                (True, self.root/f'{metadata["id"]}.events', 'events_bytes'),
            ):
                if not path.exists():
                    continue
                offset = acknowledged[offset_key]
                with path.open('rb') as source:
                    source.seek(offset)
                    while time.monotonic() < deadline:
                        data = source.read(CHUNK_BYTES)
                        if not data:
                            break
                        # A partially written header stays private until Pi has
                        # written its first complete JSONL line.
                        if not events and offset == 0 and b'\n' not in data:
                            break
                        result = self.client.record_session_chunk(metadata['id'], {
                            'offset': offset, 'data': base64.b64encode(data).decode('ascii')}, events=events)
                        if not result.ok or not isinstance(result.data, dict):
                            return False
                        new_offset = result.data.get('offset')
                        if not isinstance(new_offset, int) or new_offset <= offset:
                            return False
                        offset = new_offset
                        source.seek(offset)
                    if offset < path.stat().st_size:
                        complete = False
            links = envelope.get('results')
            if links:
                result = self.client.record_session_result(metadata['conversation_id'], links)
                if not result.ok:
                    return False
            if complete and metadata['status'] != 'running':
                final = {**transport, 'delivery_status': 'delivered',
                         'delivery_session_bytes': files[0].stat().st_size if files[0].exists() else 0,
                         'delivery_event_bytes': files[1].stat().st_size if files[1].exists() else 0}
                delivered = self.client.record_session_invocation(final)
                if not delivered.ok:
                    return False
            if complete:
                receipt.write_text(fingerprint)
                os.chmod(receipt, 0o600)
            return complete
        except Exception:
            LOG.warning('Session replay deferred; durable capture spool retained', exc_info=False)
            return False


def flush_sessions(client):
    if not hasattr(client, 'record_session_invocation'):
        return
    box = SessionOutbox(client)
    deadline = time.monotonic()+2
    for path in sorted(box.root.glob('*.json')):
        box.flush(path, seconds=max(.1, deadline-time.monotonic()))
        if time.monotonic() >= deadline:
            break


class SessionCapture:
    def __init__(self, client, backend, runtime, worker, phase, project_id, argv, usage_context=None,
                 intent_id=None, source_fact_ids=None):
        selected = conversation_from_argv(argv)
        if selected is None:
            raise ValueError('Pi invocation has no managed session path')
        conversation_id, self.source_path = selected
        self.box = SessionOutbox(client)
        self.backend, self.runtime = backend, runtime
        self.id = str(usage_context[0].attempt_id) if usage_context else str(uuid4())
        self.path = self.box.root/f'{self.id}.json'
        self.session_path = self.box.root/f'{conversation_id}.session'
        self.events_path = self.box.root/f'{self.id}.events'
        self.lock = threading.RLock()
        self.source_lock = threading.Lock()
        self.pending_read = None
        self.stop = threading.Event()
        self.buffer = ''
        self.source_missing = True
        self.source_identity_failed = False
        self.next_heartbeat = 0.0
        self.envelope = {'owner': PROCESS_INSTANCE, 'invocation': {
            'id': self.id, 'conversation_id': conversation_id, 'project_id': project_id,
            'worker_name': worker.name, 'phase': phase, 'intent_id': intent_id,
            'source_fact_ids': list(source_fact_ids or []),
            'started_at': usage_context[0].started_at.isoformat() if usage_context else _now(),
            'heartbeat_at': _now(), 'dispatcher_instance_id': PROCESS_INSTANCE,
            'finished_at': None, 'status': 'running', 'capture_status': 'waiting',
            'source_error': None, 'events_truncated': False, 'cancel_reason': None,
        }}
        self._save()
        self.thread = threading.Thread(target=self._run, name=f'pi-capture-{self.id[:8]}', daemon=True)

    def _save(self):
        with self.lock:
            if self.envelope['invocation']['status'] == 'running' and time.monotonic() >= self.next_heartbeat:
                self.envelope['invocation']['heartbeat_at'] = _now()
                self.next_heartbeat = time.monotonic()+5
            _write_json(self.path, self.envelope)

    def start(self):
        self.thread.start()

    def observe_stdout(self, chunk):
        # The process reader must never wait on HTTP. Only bounded disk writes
        # happen here; replay is owned by the separate capture thread.
        with self.lock:
            state = self.envelope['invocation']
            if state['events_truncated']:
                return
            self.buffer += chunk
            if len(self.buffer.encode('utf-8')) > CHUNK_BYTES and '\n' not in self.buffer:
                self.buffer = ''
                state['events_truncated'] = True
                self._save(); return
            while '\n' in self.buffer:
                line, self.buffer = self.buffer.split('\n', 1)
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict) or not isinstance(event.get('type'), str):
                    continue
                raw = (line+'\n').encode('utf-8')
                current = self.events_path.stat().st_size if self.events_path.exists() else 0
                if len(raw) > CHUNK_BYTES or current+len(raw) > EVENT_BYTES:
                    state['events_truncated'] = True
                    self.buffer = ''; self._save(); return
                with self.events_path.open('ab') as target:
                    os.chmod(self.events_path, 0o600)
                    target.write(raw)

    def _read_source(self, offset, maximum):
        # Docker transport can stall independently of the worker. One daemon
        # read per capture keeps its timeout away from stdout draining and
        # cancellation. A later pass may consume a completed pending read.
        selected = (offset, maximum)
        if self.pending_read is not None:
            key, ready, result = self.pending_read
            if not ready.wait(.75):
                raise TimeoutError('Pi source reader did not respond')
            self.pending_read = None
            if key == selected:
                if result[1] is not None:
                    raise result[1]
                return result[0]
        ready, result = threading.Event(), [None, None]
        def read():
            try:
                result[0] = self.backend.read_session_chunk(self.runtime, self.source_path, offset, maximum)
            except Exception as error:
                result[1] = error
            finally:
                ready.set()
        self.pending_read = (selected, ready, result)
        threading.Thread(target=read, name=f'pi-source-{self.id[:8]}', daemon=True).start()
        if not ready.wait(.75):
            raise TimeoutError('Pi source reader did not respond')
        self.pending_read = None
        if result[1] is not None:
            raise result[1]
        return result[0]

    def _copy_source(self):
        with self.source_lock:
            state = self.envelope['invocation']
            if self.source_identity_failed or state['capture_status'] == 'truncated':
                return
            current = self.session_path.stat().st_size if self.session_path.exists() else 0
            if current:
                with self.session_path.open('rb') as saved:
                    prefix = saved.readline(CHUNK_BYTES)
                try:
                    actual = self._read_source(0, len(prefix))
                except (FileNotFoundError, ValueError, OSError, RuntimeError):
                    actual = None
                if actual is not None and actual != prefix:
                    self.source_identity_failed = True
                    state.update(capture_status='unavailable', source_error='Pi session source identity changed; earlier bytes were retained')
                    self._save(); return
            # At most 16 MiB per conversation. Chunking prevents one large read
            # or transport request from becoming unbounded.
            while True:
                try:
                    data = self._read_source(current,
                                                           min(CHUNK_BYTES, SESSION_BYTES-current) if current < SESSION_BYTES else 1)
                except FileNotFoundError:
                    if current:
                        state.update(capture_status='unavailable', source_error='Pi session source disappeared')
                    self.source_missing = True
                    self._save(); return
                except (ValueError, OSError, RuntimeError):
                    state.update(capture_status='unavailable', source_error='Pi session source could not be read safely')
                    self.source_missing = True
                    self._save(); return
                self.source_missing = False
                if current >= SESSION_BYTES:
                    if data:
                        state.update(capture_status='truncated', source_error='Conversation reached the 16 MiB capture limit')
                    self._save(); return
                if not data:
                    if current and state['status'] == 'running':
                        state.update(capture_status='capturing', source_error=None)
                    self._save(); return
                with self.session_path.open('ab') as target:
                    os.chmod(self.session_path, 0o600)
                    target.write(data); target.flush(); os.fsync(target.fileno())
                current += len(data)
                state.update(capture_status='capturing', source_error=None)
                if len(data) < CHUNK_BYTES:
                    self._save(); return

    def _run(self):
        while not self.stop.is_set():
            try:
                self._save()
                self._copy_source()
                self.box.flush(self.path)
            except Exception:
                LOG.warning('Pi session capture deferred; source will be retried', exc_info=False)
            self.stop.wait(.4)

    def finish(self, result, error_status=None):
        self.stop.set()
        self.thread.join(timeout=3)
        # Finish copying while the runtime still exists. Server delivery may
        # retry later from the durable dispatcher spool after runtime cleanup.
        self._copy_source()
        with self.lock:
            state = self.envelope['invocation']
            if self.buffer.strip():
                state['events_truncated'] = True
                self.buffer = ''
            status = error_status or ('cancelled' if result.cancelled else 'timed_out' if result.timed_out or result.returncode in (124, 137)
                                     else 'success' if result.returncode == 0 else 'failed')
            if state['capture_status'] not in ('truncated', 'unavailable'):
                if self.source_missing or not self.session_path.exists() or self.session_path.stat().st_size == 0:
                    state.update(capture_status='unavailable', source_error='Pi did not create a readable session file')
                else:
                    state.update(capture_status='complete', source_error=None)
            state.update(status=status, finished_at=_now(), cancel_reason=result.cancel_reason if result is not None else None)
            self._save()
        self.box.flush(self.path)


def start_capture(client, backend, runtime, worker, phase, project_id, argv, usage_context=None,
                  intent_id=None, source_fact_ids=None):
    if worker.type != 'pi' or client is None or project_id is None or not hasattr(client, 'record_session_invocation'):
        return None
    capture = SessionCapture(client, backend, runtime, worker, phase, project_id, argv, usage_context,
                             intent_id=intent_id, source_fact_ids=source_fact_ids)
    capture.start()
    return capture


def _link_session_result(client, session, project_id, *, fact_ids=None, intent_ids=None):
    if not isinstance(session, str) or not hasattr(client, 'record_session_invocation'):
        return
    match = SESSION_PATH.fullmatch(session)
    if not match:
        return
    box = SessionOutbox(client)
    for path in box.root.glob('*.json'):
        envelope = json.loads(path.read_text())
        if envelope['invocation']['conversation_id'] != match.group(1):
            continue
        existing = envelope.get('results', {})
        envelope['results'] = {'project_id': project_id,
            'result_fact_ids': sorted(set([*existing.get('result_fact_ids', []), *(fact_ids or [])])),
            'result_intent_ids': sorted(set([*existing.get('result_intent_ids', []), *(intent_ids or [])]))}
        _write_json(path, envelope)
        box.flush(path)


def link_session_result(client, session, project_id, *, fact_ids=None, intent_ids=None):
    try:
        _link_session_result(client, session, project_id, fact_ids=fact_ids, intent_ids=intent_ids)
    except Exception:
        LOG.warning('Session graph linkage deferred; graph result is unchanged', exc_info=False)
