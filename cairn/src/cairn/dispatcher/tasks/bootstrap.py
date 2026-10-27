from __future__ import annotations

import logging
import time
import uuid

from cairn.dispatcher.config import DispatchConfig, WorkerConfig
from cairn.dispatcher.goal_expansion import expansion_prompt, recover_expansion
from cairn.dispatcher.history import add_optional_history
from cairn.dispatcher.artifacts import capture_required_artifacts
from cairn.dispatcher.contracts import (
    parse_json_output,
    validate_bootstrap_conclude_payload,
    validate_bootstrap_execute_payload,
)
from cairn.dispatcher.prompting import format_hints, load_prompt, render_prompt
from cairn.dispatcher.protocol.client import CairnClient
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.containers import ContainerManager
from cairn.dispatcher.runtime.heartbeat import HeartbeatLease
from cairn.dispatcher.tasks.common import (
    best_effort_release,
    cancel_reason,
    did_timeout,
    project_allows_conclude_fallback,
    preview,
    run_worker_process,
    task_healthcheck_enabled,
    write_conclude_result,
    write_conclude_result_with_fact_id,
)
from cairn.dispatcher.workers.registry import get_driver
from cairn.server.models import Intent, ProjectDetail

LOG = logging.getLogger(__name__)


def run_bootstrap_task(
    config: DispatchConfig,
    client: CairnClient,
    container_manager: ContainerManager,
    project: ProjectDetail,
    intent: Intent,
    worker: WorkerConfig,
    cancellation: TaskCancellation,
) -> str:
    driver = get_driver(worker.type, config.runtime.execution)
    task_started = time.perf_counter()
    healthcheck_timeout = config.runtime.healthcheck_timeout
    lease = HeartbeatLease.for_intent(client, project.project.id, intent.id, worker.name, config.runtime.interval)
    lease.start()
    try:
        container_name = container_manager.ensure_running(project.project.runtime_id)

        if task_healthcheck_enabled(config):
            LOG.info(
                "checking worker health project=%s intent=%s worker=%s timeout=%ss",
                project.project.id,
                intent.id,
                worker.name,
                healthcheck_timeout,
            )
            health = driver.check_health(worker, timeout=healthcheck_timeout)
            if cancellation.is_cancelled:
                LOG.info(
                    "bootstrap cancelled during healthcheck project=%s intent=%s worker=%s reason=%s",
                    project.project.id,
                    intent.id,
                    worker.name,
                    cancellation.reason,
                )
                best_effort_release(client, project.project.id, intent.id, worker.name)
                return "cancelled"
            if lease.failure is not None:
                LOG.warning(
                    "heartbeat lost during bootstrap healthcheck project=%s intent=%s worker=%s status=%s",
                    project.project.id,
                    intent.id,
                    worker.name,
                    lease.failure.status_code,
                )
                best_effort_release(client, project.project.id, intent.id, worker.name)
                return "failed"
            if not health.ok:
                LOG.warning(
                    "worker unhealthy project=%s intent=%s worker=%s status=%s detail=%s",
                    project.project.id,
                    intent.id,
                    worker.name,
                    health.status,
                    health.detail,
                )
                best_effort_release(client, project.project.id, intent.id, worker.name)
                return "unhealthy"

        prompt = render_prompt(
            load_prompt(config.runtime.prompt_group, "bootstrap.md"),
            _bootstrap_prompt_replacements(project),
        )
        prompt = add_optional_history(prompt, config, container_manager, container_name, project)
        prompt = expansion_prompt(prompt, project)

        session = driver.prepare_session()
        execute = driver.build_execute(worker, prompt, session)
        session = execute.session
        execute_started = time.perf_counter()
        first = run_worker_process(
            container_manager,
            container_name,
            worker,
            execute.argv,
            phase="bootstrap",
            usage_client=client, usage_project_id=project.project.id,
            timeout_seconds=config.tasks.bootstrap.timeout,
            lease=lease,
            cancellation=cancellation,
        )
        execute_ms = int((time.perf_counter() - execute_started) * 1000)
        session = driver.extract_session(session, first.stdout, first.stderr)
        cancelled = cancel_reason(first, cancellation)
        if cancelled is not None:
            LOG.info(
                "bootstrap cancelled project=%s intent=%s worker=%s reason=%s execute_ms=%s",
                project.project.id,
                intent.id,
                worker.name,
                cancelled,
                execute_ms,
            )
            best_effort_release(client, project.project.id, intent.id, worker.name)
            return "cancelled"
        if lease.failure is not None:
            LOG.warning(
                "heartbeat lost during bootstrap project=%s intent=%s worker=%s status=%s execute_ms=%s",
                project.project.id,
                intent.id,
                worker.name,
                lease.failure.status_code,
                execute_ms,
            )
            best_effort_release(client, project.project.id, intent.id, worker.name)
            return "failed"
        if not did_timeout(first) and first.returncode == 0:
            try:
                model_output = driver.extract_response_text(first.stdout, first.stderr)
                payload = parse_json_output(model_output)
                kind, data = validate_bootstrap_execute_payload(
                    payload, project.expansion.max_goals if project.expansion else None,
                )
            except Exception as exc:
                LOG.warning(
                    "bootstrap parse failed project=%s intent=%s worker=%s error=%s execute_ms=%s total_ms=%s stdout_preview=%s stderr_preview=%s",
                    project.project.id,
                    intent.id,
                    worker.name,
                    exc,
                    execute_ms,
                    int((time.perf_counter() - task_started) * 1000),
                    preview(first.stdout),
                    preview(first.stderr),
                )
                if project.expansion:
                    message = "The completion response could not be accepted because its goal expansion output was invalid: " + str(exc)[:1_000]
                    closed = write_conclude_result(
                        client, project.project.id, intent.id, worker.name, message,
                        source="expansion_validation", phase_ms=execute_ms,
                    )
                    if closed != "success":
                        return closed
                    return recover_expansion(client, client.get_project(project.project.id), worker.name, exc)
                return _try_conclude_fallback(
                    config,
                    client,
                    container_manager,
                    container_name,
                    worker,
                    driver,
                    project,
                    intent,
                    session,
                    lease,
                    cancellation,
                )
            if kind == "rejected":
                LOG.warning(
                    "bootstrap rejected project=%s intent=%s worker=%s execute_ms=%s total_ms=%s stdout_preview=%s",
                    project.project.id,
                    intent.id,
                    worker.name,
                    execute_ms,
                    int((time.perf_counter() - task_started) * 1000),
                    preview(first.stdout),
                )
                best_effort_release(client, project.project.id, intent.id, worker.name)
                return "rejected"
            return _write_bootstrap_complete_result(
                client,
                container_manager,
                project,
                intent.id,
                worker.name,
                data["fact_description"],
                data["complete_description"],
                goals=data.get("goals"),
                producer_agent_id=f"producer_{uuid.uuid4().hex}",
                producer_session_id=session or f"producer_session_{uuid.uuid4().hex}",
                source="bootstrap",
                phase_ms=execute_ms,
                total_ms=int((time.perf_counter() - task_started) * 1000),
            )
        if did_timeout(first):
            LOG.warning(
                "bootstrap timed out project=%s intent=%s worker=%s execute_ms=%s total_ms=%s stdout_preview=%s stderr_preview=%s",
                project.project.id,
                intent.id,
                worker.name,
                execute_ms,
                int((time.perf_counter() - task_started) * 1000),
                preview(first.stdout),
                preview(first.stderr),
            )
            return _try_conclude_fallback(
                config,
                client,
                container_manager,
                container_name,
                worker,
                driver,
                project,
                intent,
                session,
                lease,
                cancellation,
            )
        LOG.warning(
            "bootstrap command failed project=%s intent=%s worker=%s code=%s execute_ms=%s total_ms=%s stdout_preview=%s stderr_preview=%s",
            project.project.id,
            intent.id,
            worker.name,
            first.returncode,
            execute_ms,
            int((time.perf_counter() - task_started) * 1000),
            preview(first.stdout),
            preview(first.stderr),
        )
        best_effort_release(client, project.project.id, intent.id, worker.name)
        return "failed"
    except Exception:
        LOG.exception("bootstrap task crashed project=%s intent=%s worker=%s", project.project.id, intent.id, worker.name)
        best_effort_release(client, project.project.id, intent.id, worker.name)
        return "failed"
    finally:
        lease.stop()


def _try_conclude_fallback(
    config: DispatchConfig,
    client: CairnClient,
    container_manager: ContainerManager,
    container_name: str,
    worker: WorkerConfig,
    driver,
    project: ProjectDetail,
    intent: Intent,
    session: str | None,
    lease: HeartbeatLease,
    cancellation: TaskCancellation,
) -> str:
    if not driver.supports_conclude() or not session:
        LOG.info(
            "bootstrap conclude fallback unavailable project=%s intent=%s worker=%s supports_conclude=%s has_session=%s",
            project.project.id,
            intent.id,
            worker.name,
            driver.supports_conclude(),
            bool(session),
        )
        best_effort_release(client, project.project.id, intent.id, worker.name)
        return "failed"
    if lease.failure is not None:
        LOG.warning(
            "bootstrap conclude fallback skipped because heartbeat already lost project=%s intent=%s worker=%s",
            project.project.id,
            intent.id,
            worker.name,
        )
        best_effort_release(client, project.project.id, intent.id, worker.name)
        return "failed"
    if cancellation.is_cancelled:
        LOG.info(
            "bootstrap conclude fallback skipped because task was cancelled project=%s intent=%s worker=%s reason=%s",
            project.project.id,
            intent.id,
            worker.name,
            cancellation.reason,
        )
        best_effort_release(client, project.project.id, intent.id, worker.name)
        return "cancelled"

    if not project_allows_conclude_fallback(
        client,
        project.project.id,
        worker_name=worker.name,
        intent_id=intent.id,
    ):
        best_effort_release(client, project.project.id, intent.id, worker.name)
        return "failed"

    container_name = container_manager.ensure_running(project.project.runtime_id)

    prompt = render_prompt(
        load_prompt(config.runtime.prompt_group, "bootstrap_conclude.md"),
        _bootstrap_prompt_replacements(project),
    )
    prompt = add_optional_history(prompt, config, container_manager, container_name, project)
    conclude_argv = driver.build_conclude(worker, prompt, session)
    LOG.info("starting bootstrap conclude fallback project=%s intent=%s worker=%s", project.project.id, intent.id, worker.name)
    conclude_started = time.perf_counter()
    result = run_worker_process(
        container_manager,
        container_name,
        worker,
        conclude_argv,
        phase="bootstrap_conclude",
        usage_client=client, usage_project_id=project.project.id,
        timeout_seconds=config.tasks.bootstrap.conclude_timeout,
        lease=lease,
        cancellation=cancellation,
    )
    conclude_ms = int((time.perf_counter() - conclude_started) * 1000)
    cancelled = cancel_reason(result, cancellation)
    if cancelled is not None:
        LOG.info(
            "bootstrap conclude cancelled project=%s intent=%s worker=%s reason=%s conclude_ms=%s",
            project.project.id,
            intent.id,
            worker.name,
            cancelled,
            conclude_ms,
        )
        best_effort_release(client, project.project.id, intent.id, worker.name)
        return "cancelled"
    if lease.failure is not None:
        best_effort_release(client, project.project.id, intent.id, worker.name)
        return "failed"
    if result.timed_out or result.returncode != 0:
        LOG.warning(
            "bootstrap conclude failed project=%s intent=%s worker=%s code=%s timed_out=%s conclude_ms=%s stdout_preview=%s stderr_preview=%s",
            project.project.id,
            intent.id,
            worker.name,
            result.returncode,
            result.timed_out,
            conclude_ms,
            preview(result.stdout),
            preview(result.stderr),
        )
        best_effort_release(client, project.project.id, intent.id, worker.name)
        return "failed"
    try:
        model_output = driver.extract_response_text(result.stdout, result.stderr)
        payload = parse_json_output(model_output)
        conclude_data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        if isinstance(conclude_data, dict) and isinstance(conclude_data.get("complete"), dict):
            LOG.warning(
                "bootstrap conclude returned unexpected complete payload project=%s intent=%s worker=%s complete_preview=%s",
                project.project.id,
                intent.id,
                worker.name,
                preview(str(conclude_data.get("complete"))),
            )
        kind, fact_description = validate_bootstrap_conclude_payload(payload)
    except Exception as exc:
        LOG.warning(
            "bootstrap conclude parse failed project=%s intent=%s worker=%s error=%s conclude_ms=%s stdout_preview=%s stderr_preview=%s",
            project.project.id,
            intent.id,
            worker.name,
            exc,
            conclude_ms,
            preview(result.stdout),
            preview(result.stderr),
        )
        best_effort_release(client, project.project.id, intent.id, worker.name)
        return "failed"
    if kind == "rejected":
        LOG.warning(
            "bootstrap conclude rejected project=%s intent=%s worker=%s conclude_ms=%s stdout_preview=%s",
            project.project.id,
            intent.id,
            worker.name,
            conclude_ms,
            preview(result.stdout),
        )
        best_effort_release(client, project.project.id, intent.id, worker.name)
        return "rejected"
    return write_conclude_result(
        client,
        project.project.id,
        intent.id,
        worker.name,
        fact_description,
        source="bootstrap_conclude",
        phase_ms=conclude_ms,
    )


def _bootstrap_prompt_replacements(project: ProjectDetail) -> dict[str, str]:
    facts = {fact.id: fact.description for fact in project.facts}
    hints = [
        {
            "id": hint.id,
            "content": hint.content,
            "creator": hint.creator,
            "created_at": hint.created_at,
        }
        for hint in project.hints
    ]
    return {
        "origin": facts.get("origin", ""),
        "goal": facts.get("goal", ""),
        "hints": format_hints(hints),
    }


def _write_bootstrap_complete_result(
    client: CairnClient,
    container_manager: ContainerManager,
    project: ProjectDetail,
    intent_id: str,
    worker_name: str,
    fact_description: str,
    complete_description: str,
    *,
    source: str,
    phase_ms: int,
    total_ms: int | None = None,
    goals: list[dict] | None = None,
    producer_agent_id: str | None = None,
    producer_session_id: str | None = None,
) -> str:
    project_id = project.project.id
    conclude = write_conclude_result_with_fact_id(
        client,
        project_id,
        intent_id,
        worker_name,
        fact_description,
        source=source,
        phase_ms=phase_ms,
        total_ms=total_ms,
    )
    if conclude.status != "success":
        return "failed"
    if conclude.fact_id is None:
        LOG.warning(
            "bootstrap complete deferred because conclude response omitted fact id project=%s intent=%s worker=%s source=%s",
            project_id,
            intent_id,
            worker_name,
            source,
        )
        return "success"

    if project.required_artifacts and not capture_required_artifacts(
        client, container_manager, client.get_project(project_id), worker_name,
    ):
        return "success"
    kwargs = {"goals": goals} if goals is not None else {}
    if project.project.verification_enabled:
        kwargs.update(producer_agent_id=producer_agent_id, producer_session_id=producer_session_id)
    response = client.complete(project_id, [conclude.fact_id], complete_description, worker_name, **kwargs)
    if project.expansion and response.status_code in (409, 422):
        return recover_expansion(client, project, worker_name, response.text)
    if response.status_code in (403, 409):
        LOG.info(
            "bootstrap complete deferred project=%s intent=%s worker=%s source=%s status=%s fact_id=%s",
            project_id,
            intent_id,
            worker_name,
            source,
            response.status_code,
            conclude.fact_id,
        )
        return "success"
    if not response.ok:
        LOG.warning(
            "bootstrap complete write failed project=%s intent=%s worker=%s source=%s fact_id=%s status=%s body=%s",
            project_id,
            intent_id,
            worker_name,
            source,
            conclude.fact_id,
            response.status_code,
            response.text,
        )
        return "success"
    if total_ms is None:
        LOG.info(
            "bootstrap completed project=%s intent=%s worker=%s source=%s from=%s phase_ms=%s",
            project_id,
            intent_id,
            worker_name,
            source,
            [conclude.fact_id],
            phase_ms,
        )
    else:
        LOG.info(
            "bootstrap completed project=%s intent=%s worker=%s source=%s from=%s phase_ms=%s total_ms=%s",
            project_id,
            intent_id,
            worker_name,
            source,
            [conclude.fact_id],
            phase_ms,
            total_ms,
        )
    return "success"
