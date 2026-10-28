from __future__ import annotations

from datetime import datetime, timezone
from contextlib import contextmanager
import hashlib
import fcntl
import json
import logging
import os
from pathlib import Path
import sqlite3
import time
import threading
import uuid

from wayline.dispatcher.usage_parsers import claude_session_key, json_events, parse_usage
from wayline.server.usage_models import UsageAttempt

LOG = logging.getLogger(__name__)
PROCESS_INSTANCE = uuid.uuid4().hex

_OWNER_GUARD = threading.Lock()
_OWNER_FILES: dict[Path, int] = {}


class UsageOutboxOwnershipError(RuntimeError):
    """Another process owns this outbox; no worker may start using it."""


def _reset_owners_after_fork():
    global _OWNER_GUARD, PROCESS_INSTANCE
    # A fork inherits descriptors referring to the parent's same lock. Close the
    # child's copies without LOCK_UN, which would unlock the parent's ownership.
    for descriptor in _OWNER_FILES.values():
        os.close(descriptor)
    _OWNER_FILES.clear()
    _OWNER_GUARD = threading.Lock()
    PROCESS_INSTANCE = uuid.uuid4().hex


os.register_at_fork(after_in_child=_reset_owners_after_fork)


def _claim_owner(path: Path):
    # One retained descriptor per resolved database path, shared by this process's
    # helper instances and worker threads. The OS releases it on exit or crash.
    with _OWNER_GUARD:
        if path in _OWNER_FILES:
            return
        descriptor = os.open(str(path) + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(descriptor)
            raise UsageOutboxOwnershipError(
                f"Usage outbox is already owned by another dispatcher: {path}. "
                "Set WAYLINE_USAGE_OUTBOX to a separate durable path per dispatcher."
            ) from exc
        except BaseException:
            os.close(descriptor)
            raise
        _OWNER_FILES[path] = descriptor


class UsageOutbox:
    """Durable sanitized events and Claude cumulative baselines, never CLI text."""
    def __init__(self, server_key: str, path: Path | None = None):
        self.server_key = server_key
        self.path = (path or Path(os.environ.get("WAYLINE_USAGE_OUTBOX", str(Path.home() / ".local/share/wayline/usage-outbox.db")))).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _claim_owner(self.path)
        # Cold helpers share process ownership but SQLite cannot concurrently
        # switch a fresh database into WAL while peers initialize its schema.
        with _OWNER_GUARD, self.connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS pending_usage (
                    server_key TEXT NOT NULL, attempt_id TEXT NOT NULL, payload TEXT NOT NULL,
                    state TEXT NOT NULL, owner_pid INTEGER NOT NULL, owner_instance TEXT,
                    PRIMARY KEY(server_key,attempt_id)
                );
                CREATE TABLE IF NOT EXISTS usage_outbox_meta (
                    server_key TEXT PRIMARY KEY, reporter_id TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS claude_usage_baselines (
                    server_key TEXT NOT NULL, session_key TEXT NOT NULL, snapshot TEXT NOT NULL,
                    PRIMARY KEY(server_key,session_key)
                );
            """)
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(pending_usage)")}
            if "owner_instance" not in columns:
                try:
                    conn.execute("ALTER TABLE pending_usage ADD COLUMN owner_instance TEXT")
                except sqlite3.OperationalError:
                    # Another dispatcher thread may have migrated the same outbox.
                    if "owner_instance" not in {row["name"] for row in conn.execute("PRAGMA table_info(pending_usage)")}:
                        raise
            conn.execute("INSERT OR IGNORE INTO usage_outbox_meta(server_key,reporter_id) VALUES(?,?)",
                         (self.server_key, hashlib.sha256(uuid.uuid4().bytes).hexdigest()))

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def begin(self, attempt: UsageAttempt):
        with self.connect() as conn:
            conn.execute("INSERT INTO pending_usage(server_key,attempt_id,payload,state,owner_pid,owner_instance) VALUES(?,?,?,'running',?,?)",
                         (self.server_key, str(attempt.attempt_id), attempt.model_dump_json(), os.getpid(), PROCESS_INSTANCE))

    def finish(self, attempt: UsageAttempt, worker, stdout: str, resumed: bool):
        events = json_events(stdout) if worker.type == "claudecode" else []
        session_key = claude_session_key(events) if events else None
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            baseline_row = conn.execute("SELECT snapshot FROM claude_usage_baselines WHERE server_key=? AND session_key=?",
                                        (self.server_key, session_key)).fetchone() if session_key else None
            parsed = parse_usage(worker, stdout, resumed=resumed, baseline=json.loads(baseline_row["snapshot"]) if baseline_row else None)
            attempt.readings = parsed.readings
            attempt.notes = list(dict.fromkeys([*attempt.notes, *parsed.notes]))
            attempt = UsageAttempt.model_validate(attempt.model_dump())
            conn.execute("INSERT INTO pending_usage(server_key,attempt_id,payload,state,owner_pid,owner_instance) VALUES(?,?,?,'ready',?,?) "
                         "ON CONFLICT(server_key,attempt_id) DO UPDATE SET payload=excluded.payload,state='ready'",
                         (self.server_key, str(attempt.attempt_id), attempt.model_dump_json(), os.getpid(), PROCESS_INSTANCE))
            if parsed.session_key and parsed.snapshot:
                conn.execute("INSERT INTO claude_usage_baselines(server_key,session_key,snapshot) VALUES(?,?,?) "
                             "ON CONFLICT(server_key,session_key) DO UPDATE SET snapshot=excluded.snapshot",
                             (self.server_key, parsed.session_key, json.dumps(parsed.snapshot)))
        return attempt

    def enqueue(self, attempt: UsageAttempt):
        with self.connect() as conn:
            conn.execute("INSERT INTO pending_usage(server_key,attempt_id,payload,state,owner_pid,owner_instance) VALUES(?,?,?,'ready',?,?) "
                         "ON CONFLICT(server_key,attempt_id) DO UPDATE SET payload=excluded.payload,state='ready'",
                         (self.server_key, str(attempt.attempt_id), attempt.model_dump_json(), os.getpid(), PROCESS_INSTANCE))

    def flush(self, client, limit=3):
        with self.connect() as conn:
            rows = conn.execute("SELECT attempt_id,owner_pid,owner_instance,payload FROM pending_usage WHERE server_key=? AND state='running'", (self.server_key,)).fetchall()
            for row in rows:
                if row["owner_instance"] != PROCESS_INSTANCE:
                    # Exclusive process ownership proves that no peer dispatcher
                    # can still be producing this prior instance's usage. Numeric
                    # PIDs are irrelevant across hosts and PID namespaces.
                    payload = json.loads(row["payload"])
                    payload["notes"] = [*payload["notes"], "dispatcher_interrupted"]
                    conn.execute("UPDATE pending_usage SET payload=?,state='ready' WHERE server_key=? AND attempt_id=?",
                                 (json.dumps(payload), self.server_key, row["attempt_id"]))
            rows = conn.execute("SELECT attempt_id,payload FROM pending_usage WHERE server_key=? AND state='ready' ORDER BY json_extract(payload,'$.started_at'),attempt_id LIMIT ?",
                                (self.server_key, limit)).fetchall()
        for row in rows:
            response = client.record_usage_attempt(json.loads(row["payload"]))
            if response.ok:
                with self.connect() as conn:
                    conn.execute("DELETE FROM pending_usage WHERE server_key=? AND attempt_id=?", (self.server_key, row["attempt_id"]))
            elif response.status_code in (400, 409, 422):
                with self.connect() as conn:
                    conn.execute("UPDATE pending_usage SET state='rejected' WHERE server_key=? AND attempt_id=?", (self.server_key, row["attempt_id"]))
                LOG.error("usage event rejected attempt=%s status=%s; retained in local outbox", row["attempt_id"], response.status_code)
            else:
                LOG.warning("usage reporting pending attempt=%s status=%s; retry retains original ID", row["attempt_id"], response.status_code)
                break

    def publish_status(self, client):
        if not hasattr(client, "record_usage_reporter"):
            return
        with self.connect() as conn:
            counts = dict(conn.execute("SELECT state,COUNT(*) FROM pending_usage WHERE server_key=? GROUP BY state", (self.server_key,)).fetchall())
            reporter_id = conn.execute("SELECT reporter_id FROM usage_outbox_meta WHERE server_key=?", (self.server_key,)).fetchone()[0]
        client.record_usage_reporter({"reporter_id": reporter_id, "pending_attempts": counts.get("ready", 0),
                                     "running_attempts": counts.get("running", 0), "rejected_attempts": counts.get("rejected", 0)})


def _outbox(client):
    key = hashlib.sha256(str(getattr(client, "_base_url", "in-process")).encode()).hexdigest()
    return UsageOutbox(key)


def ensure_usage_owner(client):
    if hasattr(client, "record_usage_attempt"):
        _outbox(client)


def invocation_worker(worker, argv):
    if argv is None:
        return worker
    # Only attribute a configured model when this invocation actually selects it.
    # Local Codex/Pi deliberately preserve the host CLI default and ignore their
    # container-only env model selectors.
    env = dict(worker.env)
    model = None
    effort = None
    if worker.type in ("codex", "pi"):
        env.pop("CODEX_MODEL" if worker.type == "codex" else "PI_MODEL", None)
        for index, value in enumerate(argv[:-1]):
            if value == "--model":
                model = argv[index + 1]
            if worker.type == "codex" and value.startswith("model_reasoning_effort="):
                effort = value.partition("=")[2].strip('"')
            if worker.type == "pi" and value == "--thinking":
                effort = argv[index + 1]
    elif worker.type == "claudecode":
        model = env.get("ANTHROPIC_MODEL")
    return worker.model_copy(update={"model": model, "env": env, "reasoning_effort": effort})


def start_attempt(client, project_id, worker, phase, argv=None):
    if client is None or project_id is None or not hasattr(client, "record_usage_attempt"):
        return None
    worker = invocation_worker(worker, argv)
    parsed = parse_usage(worker, "")
    attempt = UsageAttempt(
        attempt_id=uuid.uuid4(), project_id=project_id, worker_name=worker.name, worker_type=worker.type,
        reasoning_effort=getattr(worker, "reasoning_effort", None), phase=phase, status="process_error",
        started_at=datetime.now(timezone.utc), readings=parsed.readings, notes=[],
    )
    try:
        _outbox(client).begin(attempt)
    except UsageOutboxOwnershipError:
        raise
    except Exception:
        LOG.error("could not persist usage attempt=%s; final reporting will still be attempted", attempt.attempt_id)
    return attempt, time.monotonic()


def finish_attempt(context, client, worker, argv, result, error_status=None):
    if context is None:
        return
    worker = invocation_worker(worker, argv)
    attempt, started = context
    attempt.finished_at = datetime.now(timezone.utc)
    attempt.duration_ms = max(0, int((time.monotonic() - started) * 1000))
    attempt.status = error_status or ("cancelled" if result.cancelled else "timed_out" if result.timed_out or result.returncode in (124, 137)
                                    else "success" if result.returncode == 0 else "failed")
    if attempt.status != "success":
        attempt.notes.append("incomplete_process")
    resumed = worker.type == "claudecode" and any(flag in argv for flag in ("-r", "--resume", "--continue"))
    stdout = result.stdout if result is not None else ""
    try:
        box = _outbox(client)
        try:
            box.finish(attempt, worker, stdout, resumed)
        except Exception:
            attempt.readings = parse_usage(worker, "").readings
            attempt.notes = [*attempt.notes, "usage_parser_failed"]
            box.enqueue(UsageAttempt.model_validate(attempt.model_dump()))
            LOG.error("usage parsing failed attempt=%s; retained as unreported", attempt.attempt_id)
        box.flush(client)
        box.publish_status(client)
    except Exception:
        # Metrics must never change the worker's result. A persisted running entry
        # makes an interrupted or failed report visible on the next dispatcher run.
        LOG.error("usage capture/report failed attempt=%s; inspect local usage outbox", attempt.attempt_id)


def flush_usage(client):
    if not hasattr(client, "record_usage_attempt"):
        return
    try:
        box = _outbox(client)
        box.flush(client)
        box.publish_status(client)
    except Exception:
        LOG.error("usage outbox replay failed; queued records retained")
