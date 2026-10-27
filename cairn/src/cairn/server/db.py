from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

DEFAULT_DB = Path.home() / ".local" / "share" / "cairn" / "cairn.db"

_db_path: Path | None = None

SCHEMA = """\
CREATE TABLE IF NOT EXISTS workflows (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    origin TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    deleted_at TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    intent_timeout INTEGER NOT NULL DEFAULT 15,
    reason_timeout INTEGER NOT NULL DEFAULT 15
);

CREATE TABLE IF NOT EXISTS usage_reporters (
    reporter_id TEXT PRIMARY KEY, last_seen TEXT NOT NULL,
    pending_attempts INTEGER NOT NULL, running_attempts INTEGER NOT NULL, rejected_attempts INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS usage_attempts (
    attempt_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, workflow_id TEXT,
    worker_type TEXT NOT NULL, phase TEXT NOT NULL, started_at TEXT NOT NULL,
    coverage TEXT NOT NULL, payload_hash TEXT NOT NULL, payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS usage_attempts_by_time ON usage_attempts(started_at);
CREATE INDEX IF NOT EXISTS usage_attempts_by_project_time ON usage_attempts(project_id,started_at);
CREATE TABLE IF NOT EXISTS usage_readings (
    attempt_id TEXT NOT NULL REFERENCES usage_attempts(attempt_id), position INTEGER NOT NULL,
    model TEXT, model_source TEXT NOT NULL, input_tokens INTEGER, cached_input_tokens INTEGER,
    uncached_input_tokens INTEGER, cache_write_input_tokens INTEGER, output_tokens INTEGER,
    PRIMARY KEY(attempt_id,position)
);

INSERT OR IGNORE INTO settings (rowid, intent_timeout, reason_timeout) VALUES (1, 15, 15);

CREATE TABLE IF NOT EXISTS runtime_settings (
    max_workers INTEGER,
    max_running_projects INTEGER,
    max_project_workers INTEGER,
    revision INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO runtime_settings (rowid, revision) VALUES (1, 0);

CREATE TABLE IF NOT EXISTS runtime_dispatchers (
    id TEXT PRIMARY KEY,
    last_seen TEXT NOT NULL,
    report_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    bootstrap_enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    reason_worker TEXT,
    reason_trigger TEXT,
    reason_started_at TEXT,
    reason_last_heartbeat_at TEXT
);

CREATE TABLE IF NOT EXISTS workflow_stages (
    workflow_id TEXT NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
    id TEXT NOT NULL,
    position INTEGER NOT NULL,
    definition_json TEXT NOT NULL,
    project_id TEXT UNIQUE REFERENCES projects(id),
    handoffs_json TEXT NOT NULL DEFAULT '[]',
    completion_json TEXT,
    artifact_recovery_attempts INTEGER NOT NULL DEFAULT 0,
    artifact_error TEXT,
    history_stage_ids_json TEXT NOT NULL DEFAULT '[]',
    context_version INTEGER NOT NULL DEFAULT 0,
    generated_by_stage_id TEXT,
    expansion_recovery_attempts INTEGER NOT NULL DEFAULT 0,
    expansion_error TEXT,
    PRIMARY KEY (workflow_id, id)
);

CREATE TABLE IF NOT EXISTS workflow_stage_attempts (
    project_id TEXT PRIMARY KEY REFERENCES projects(id),
    stage_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS facts (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    description TEXT NOT NULL,
    PRIMARY KEY (id, project_id)
);

CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
    stage_id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    label TEXT,
    size INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    content BLOB NOT NULL,
    UNIQUE (project_id, path)
);

CREATE INDEX IF NOT EXISTS facts_by_project ON facts(project_id);

CREATE TABLE IF NOT EXISTS intents (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    to_fact_id TEXT,
    description TEXT NOT NULL,
    creator TEXT NOT NULL,
    worker TEXT,
    last_heartbeat_at TEXT,
    created_at TEXT NOT NULL,
    concluded_at TEXT,
    PRIMARY KEY (id, project_id)
);

CREATE TABLE IF NOT EXISTS intent_sources (
    intent_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    fact_id TEXT NOT NULL,
    PRIMARY KEY (intent_id, project_id, fact_id),
    FOREIGN KEY (intent_id, project_id) REFERENCES intents(id, project_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS intents_by_project ON intents(project_id);

CREATE TABLE IF NOT EXISTS hints (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    content TEXT NOT NULL,
    creator TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (id, project_id)
);

CREATE TABLE IF NOT EXISTS counters (
    name TEXT PRIMARY KEY,
    value INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS hints_by_project ON hints(project_id);

INSERT OR IGNORE INTO counters (name, value) VALUES ('project', 0);
INSERT OR IGNORE INTO counters (name, value) VALUES ('workflow', 0);

CREATE TABLE IF NOT EXISTS scoped_counters (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    value INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (project_id, kind)
);
"""


def configure(path: Path) -> None:
    global _db_path
    if _db_path is not None:
        return
    _db_path = path
    _db_path.parent.mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        _ensure_workflow_columns(conn)
        _ensure_project_columns(conn)
        _ensure_workflow_stage_columns(conn)
        from cairn.server.session_service import ensure_schema as ensure_session_schema
        ensure_session_schema(conn)
        from cairn.server.verification_service import ensure_schema
        ensure_schema(conn)
        from cairn.server.workflow_context import migrate_legacy_stage_contexts
        migrate_legacy_stage_contexts(conn)


def _ensure_workflow_columns(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(workflows)")}
    if "deleted_at" not in columns:
        conn.execute("ALTER TABLE workflows ADD COLUMN deleted_at TEXT")


def _ensure_project_columns(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(projects)")}
    if "bootstrap_enabled" not in columns:
        conn.execute("ALTER TABLE projects ADD COLUMN bootstrap_enabled INTEGER NOT NULL DEFAULT 1")
        if "bootstrap_mode" in columns:
            conn.execute(
                "UPDATE projects SET bootstrap_enabled = CASE WHEN bootstrap_mode = 'disabled' THEN 0 ELSE 1 END"
            )
    for column in ("workflow_id", "workflow_stage_id"):
        if column not in columns:
            conn.execute(f"ALTER TABLE projects ADD COLUMN {column} TEXT")


def _ensure_workflow_stage_columns(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(workflow_stages)")}
    for name, definition in (("not_applicable_reason", "TEXT"), ("resolved_at", "TEXT"), ("result_recovery_attempts", "INTEGER NOT NULL DEFAULT 0"),
                             ("result_error", "TEXT"), ("accepted_artifacts_json", "TEXT NOT NULL DEFAULT '[]'"), ("option_state", "TEXT"), ("option_reason", "TEXT"),
                             ("option_history_json", "TEXT NOT NULL DEFAULT '[]'"), ("choice_error", "TEXT")):
        if name not in columns:
            conn.execute(f"ALTER TABLE workflow_stages ADD COLUMN {name} {definition}")
    if "artifact_recovery_attempts" not in columns:
        conn.execute(
            "ALTER TABLE workflow_stages ADD COLUMN artifact_recovery_attempts INTEGER NOT NULL DEFAULT 0"
        )
    if "artifact_error" not in columns:
        conn.execute("ALTER TABLE workflow_stages ADD COLUMN artifact_error TEXT")
    if "history_stage_ids_json" not in columns:
        conn.execute("ALTER TABLE workflow_stages ADD COLUMN history_stage_ids_json TEXT NOT NULL DEFAULT '[]'")
    if "context_version" not in columns:
        conn.execute("ALTER TABLE workflow_stages ADD COLUMN context_version INTEGER NOT NULL DEFAULT 0")
    if "generated_by_stage_id" not in columns:
        conn.execute("ALTER TABLE workflow_stages ADD COLUMN generated_by_stage_id TEXT")
    if "expansion_recovery_attempts" not in columns:
        conn.execute("ALTER TABLE workflow_stages ADD COLUMN expansion_recovery_attempts INTEGER NOT NULL DEFAULT 0")
    if "expansion_error" not in columns:
        conn.execute("ALTER TABLE workflow_stages ADD COLUMN expansion_error TEXT")


@contextmanager
def get_conn() -> Generator[sqlite3.Connection, None, None]:
    assert _db_path is not None
    conn = sqlite3.connect(str(_db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
