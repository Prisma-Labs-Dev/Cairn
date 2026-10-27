from __future__ import annotations

import sqlite3

from cairn.server import db


def test_configure_adds_bootstrap_enabled_to_legacy_projects_table(tmp_path, monkeypatch) -> None:
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE projects (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                reason_worker TEXT,
                reason_trigger TEXT,
                reason_started_at TEXT,
                reason_last_heartbeat_at TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO projects (id, title, created_at) VALUES ('proj_001', 'legacy', '2026-01-01T00:00:00Z')"
        )

    monkeypatch.setattr(db, "_db_path", None)
    db.configure(path)

    with db.get_conn() as conn:
        row = conn.execute("SELECT bootstrap_enabled FROM projects WHERE id = 'proj_001'").fetchone()
    assert row["bootstrap_enabled"] == 1


def test_configure_maps_disabled_bootstrap_mode_to_false(tmp_path, monkeypatch) -> None:
    path = tmp_path / "intermediate.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE projects (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                bootstrap_mode TEXT NOT NULL DEFAULT 'auto',
                created_at TEXT NOT NULL,
                reason_worker TEXT,
                reason_trigger TEXT,
                reason_started_at TEXT,
                reason_last_heartbeat_at TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO projects (id, title, bootstrap_mode, created_at) VALUES ('proj_001', 'disabled', 'disabled', '2026-01-01T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO projects (id, title, bootstrap_mode, created_at) VALUES ('proj_002', 'enabled', 'enabled', '2026-01-01T00:00:00Z')"
        )

    monkeypatch.setattr(db, "_db_path", None)
    db.configure(path)

    with db.get_conn() as conn:
        rows = conn.execute("SELECT id, bootstrap_enabled FROM projects ORDER BY id").fetchall()
    assert [(row["id"], row["bootstrap_enabled"]) for row in rows] == [
        ("proj_001", 0),
        ("proj_002", 1),
    ]


def test_configure_adds_artifact_recovery_state_to_existing_workflow_stages(tmp_path, monkeypatch) -> None:
    path = tmp_path / "workflow-before-recovery.db"
    with sqlite3.connect(path) as conn:
        conn.execute("""
            CREATE TABLE workflow_stages (
                workflow_id TEXT NOT NULL, id TEXT NOT NULL, position INTEGER NOT NULL,
                definition_json TEXT NOT NULL, project_id TEXT UNIQUE,
                handoffs_json TEXT NOT NULL DEFAULT '[]', completion_json TEXT,
                PRIMARY KEY (workflow_id, id)
            )
        """)
        conn.execute(
            "INSERT INTO workflow_stages (workflow_id,id,position,definition_json) VALUES (?,?,?,?)",
            ("workflow_001", "research", 0, '{"id":"research","title":"Research","goal":"Investigate"}'),
        )
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(path)
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM workflow_stages WHERE id = 'research'").fetchone()
    assert row["workflow_id"] == "workflow_001"
    assert row["artifact_recovery_attempts"] == 0
    assert row["artifact_error"] is None
