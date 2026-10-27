from __future__ import annotations

from datetime import datetime, timezone
import json
import sqlite3

from cairn.server.runtime_models import (
    DispatcherReport, RUNTIME_FIELDS, RuntimeDispatcher, RuntimeLimits, RuntimeOverview,
    RuntimeOverrides, RuntimeOverrideUpdate, RuntimeSettingsState,
)
from cairn.server.services import utcnow


def get_runtime_settings(conn: sqlite3.Connection) -> RuntimeSettingsState:
    row = conn.execute("SELECT * FROM runtime_settings WHERE rowid = 1").fetchone()
    return RuntimeSettingsState(
        settings=RuntimeOverrides(**{field: row[field] for field in RUNTIME_FIELDS}),
        settings_revision=row["revision"],
    )


def set_runtime_settings(conn: sqlite3.Connection, update: RuntimeOverrideUpdate) -> RuntimeOverview:
    state = get_runtime_settings(conn)
    changed = state.settings.model_dump()
    changed.update(update.model_dump(exclude_unset=True))
    if changed != state.settings.model_dump():
        conn.execute(
            "UPDATE runtime_settings SET max_workers = ?, max_running_projects = ?, "
            "max_project_workers = ?, revision = revision + 1 WHERE rowid = 1",
            tuple(changed[field] for field in RUNTIME_FIELDS),
        )
    return runtime_overview(conn)


def save_dispatcher_report(conn: sqlite3.Connection, dispatcher_id: str, report: DispatcherReport) -> None:
    conn.execute(
        "INSERT INTO runtime_dispatchers (id, last_seen, report_json) VALUES (?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET last_seen=excluded.last_seen, report_json=excluded.report_json",
        (dispatcher_id, utcnow(), report.model_dump_json()),
    )
    # Dead process IDs are not an audit trail; retain a day for stale-state visibility.
    conn.execute(
        "DELETE FROM runtime_dispatchers WHERE julianday('now') - julianday(last_seen) > 1"
    )


def runtime_overview(conn: sqlite3.Connection) -> RuntimeOverview:
    state = get_runtime_settings(conn)
    dispatchers = []
    now = datetime.now(timezone.utc)
    for row in conn.execute("SELECT * FROM runtime_dispatchers ORDER BY last_seen DESC, id"):
        report = DispatcherReport.model_validate_json(row["report_json"])
        last_seen = datetime.fromisoformat(row["last_seen"].replace("Z", "+00:00"))
        desired = RuntimeLimits(**{
            field: getattr(state.settings, field) or getattr(report.configured, field)
            for field in RUNTIME_FIELDS
        })
        capacity = sum(worker.max_running for worker in report.workers)
        eligible = [worker for worker in report.workers if not worker.cooldown]
        eligible_capacity = sum(worker.max_running for worker in eligible)
        available = sum(max(0, worker.max_running - worker.running) for worker in eligible)
        dispatchers.append(RuntimeDispatcher(
            **report.model_dump(), id=row["id"], last_seen=row["last_seen"],
            online=(now - last_seen).total_seconds() <= report.stale_after_seconds,
            pending=report.applied_settings_revision != state.settings_revision,
            desired=desired, worker_capacity=capacity,
            effective_worker_capacity=min(report.effective.max_workers, eligible_capacity),
            available_worker_slots=min(max(0, report.effective.max_workers - report.counts.running_tasks), available),
            capacity_by_task_type={
                task_type: sum(worker.max_running for worker in eligible if task_type in worker.task_types)
                for task_type in ("bootstrap", "reason", "explore")
            },
        ))
    return RuntimeOverview(**state.model_dump(), dispatchers=dispatchers)
