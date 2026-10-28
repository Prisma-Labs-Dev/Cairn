from __future__ import annotations

import logging
import time
import uuid

from wayline.dispatcher.config import DispatchConfig, WorkerConfig
from wayline.dispatcher.session_capture import link_session_result
from wayline.dispatcher.goal_expansion import expansion_prompt, recover_expansion
from wayline.dispatcher.history import add_optional_history
from wayline.dispatcher.artifacts import capture_required_artifacts
from wayline.dispatcher.contracts import parse_json_output, validate_reason_payload
from wayline.dispatcher.prompting import (
    format_fact_ids,
    format_open_intents,
    load_prompt,
    render_prompt,
)
from wayline.dispatcher.protocol.client import WaylineClient
from wayline.dispatcher.runtime.cancellation import TaskCancellation
from wayline.dispatcher.runtime.containers import ContainerManager
from wayline.dispatcher.runtime.heartbeat import HeartbeatLease
from wayline.dispatcher.tasks.common import (
    best_effort_release_reason,
    cancel_reason,
    did_timeout,
    preview,
    run_worker_process,
    task_healthcheck_enabled,
    write_graph_snapshot_reference,
)
from wayline.dispatcher.workers.registry import get_driver
from wayline.server.models import ProjectDetail

LOG = logging.getLogger(__name__)


def run_reason_task(
    config: DispatchConfig,
    client: WaylineClient,
    container_manager: ContainerManager,
    project: ProjectDetail,
    export_yaml: str,
    worker: WorkerConfig,
    cancellation: TaskCancellation,
) -> str:
    driver = get_driver(worker.type, config.runtime.execution)
    task_started = time.perf_counter()
    healthcheck_timeout = config.runtime.healthcheck_timeout
    lease = HeartbeatLease.for_reason(client, project.project.id, worker.name, config.runtime.interval)
    lease.start()
    try:
        container_name = container_manager.ensure_running(project.project.runtime_id)

        if task_healthcheck_enabled(config):
            LOG.info(
                "checking worker health project=%s worker=%s timeout=%ss",
                project.project.id,
                worker.name,
                healthcheck_timeout,
            )
            health = driver.check_health(worker, timeout=healthcheck_timeout)
            if cancellation.is_cancelled:
                LOG.info(
                    "reason cancelled during healthcheck project=%s worker=%s reason=%s",
                    project.project.id,
                    worker.name,
                    cancellation.reason,
                )
                return "cancelled"
            if lease.failure is not None:
                LOG.warning(
                    "heartbeat lost during reason healthcheck project=%s worker=%s status=%s",
                    project.project.id,
                    worker.name,
                    lease.failure.status_code,
                )
                return "failed"
            if not health.ok:
                LOG.warning(
                    "worker unhealthy project=%s worker=%s status=%s detail=%s",
                    project.project.id,
                    worker.name,
                    health.status,
                    health.detail,
                )
                return "unhealthy"
        open_intents = [
            {
                "id": intent.id,
                "from": intent.from_,
                "description": intent.description,
                "worker": intent.worker,
            }
            for intent in project.intents
            if intent.to is None
        ]
        allowed_fact_ids = [fact.id for fact in project.facts if fact.id != "goal"]
        LOG.debug(
            "reason context prepared project=%s worker=%s facts=%s allowed_fact_ids=%s hints=%s open_intents=%s",
            project.project.id,
            worker.name,
            len(project.facts),
            len(allowed_fact_ids),
            len(project.hints),
            len(open_intents),
        )
        prompt = render_prompt(
            load_prompt(config.runtime.prompt_group, "reason.md"),
            {
                "graph_yaml": write_graph_snapshot_reference(
                    container_manager,
                    container_name,
                    export_yaml.strip(),
                    phase="reason_execute",
                ),
                "fact_ids": format_fact_ids(allowed_fact_ids),
                "open_intents": format_open_intents(open_intents),
                "max_intents": str(config.tasks.reason.max_intents),
            },
        )

        prompt = add_optional_history(prompt, config, container_manager, container_name, project)
        prompt = expansion_prompt(prompt, project)
        producer_agent_id = f"producer_{uuid.uuid4().hex}"
        session = driver.prepare_session()
        producer_session_id = session or f"producer_session_{uuid.uuid4().hex}"
        command = driver.build_execute(worker, prompt, session)
        execute_started = time.perf_counter()
        result = run_worker_process(
            container_manager,
            container_name,
            worker,
            command.argv,
            phase="reason_execute",
            usage_client=client, usage_project_id=project.project.id,
            session_source_fact_ids=sorted(allowed_fact_ids),
            timeout_seconds=config.tasks.reason.timeout,
            lease=lease,
            cancellation=cancellation,
        )
        execute_ms = int((time.perf_counter() - execute_started) * 1000)
        total_ms = int((time.perf_counter() - task_started) * 1000)
        session = driver.extract_session(session, result.stdout, result.stderr)
        cancelled = cancel_reason(result, cancellation)
        if cancelled is not None:
            LOG.info(
                "reason cancelled project=%s worker=%s reason=%s execute_ms=%s",
                project.project.id,
                worker.name,
                cancelled,
                execute_ms,
            )
            return "cancelled"
        if lease.failure is not None:
            LOG.warning(
                "heartbeat lost during reason project=%s worker=%s status=%s execute_ms=%s",
                project.project.id,
                worker.name,
                lease.failure.status_code,
                execute_ms,
            )
            return "failed"
        if did_timeout(result):
            LOG.warning(
                "reason timed out project=%s worker=%s execute_ms=%s total_ms=%s stdout_preview=%s stderr_preview=%s",
                project.project.id,
                worker.name,
                execute_ms,
                total_ms,
                preview(result.stdout),
                preview(result.stderr),
            )
            return "failed"
        if result.returncode != 0:
            LOG.warning(
                "reason command failed project=%s worker=%s code=%s execute_ms=%s total_ms=%s stdout_preview=%s stderr_preview=%s",
                project.project.id,
                worker.name,
                result.returncode,
                execute_ms,
                total_ms,
                preview(result.stdout),
                preview(result.stderr),
            )
            return "failed"
        try:
            model_output = driver.extract_response_text(result.stdout, result.stderr)
            payload = parse_json_output(model_output)
            kind, data = validate_reason_payload(
                payload, open_intents_empty=not open_intents, max_intents=config.tasks.reason.max_intents,
                expansion_max_goals=project.expansion.max_goals if project.expansion else None, result_contract=project.result_contract,
            )
        except Exception as exc:
            LOG.warning(
                "reason parse failed project=%s worker=%s error=%s execute_ms=%s total_ms=%s stdout_preview=%s stderr_preview=%s",
                project.project.id,
                worker.name,
                exc,
                execute_ms,
                total_ms,
                preview(result.stdout),
                preview(result.stderr),
            )
            if project.expansion or project.result_contract:
                return recover_expansion(client, project, worker.name, exc)
            return "failed"
        if kind == "rejected":
            LOG.warning(
                "reason rejected project=%s worker=%s execute_ms=%s total_ms=%s stdout_preview=%s",
                project.project.id,
                worker.name,
                execute_ms,
                total_ms,
                preview(result.stdout),
            )
            return "rejected"
        if kind == "unavailable":
            response = client.unavailable(project.project.id, data["from"], data["description"], worker.name)
            return "success" if response.ok or response.status_code == 403 else "failed"
        if kind == "complete":
            if project.required_artifacts and not capture_required_artifacts(
                client, container_manager, project, worker.name,
            ):
                # Recovery is now an open exploration intent. Advance the reason
                # checkpoint so repeated completion attempts cannot starve it.
                return "success"
            kwargs = {"goals": data["goals"]} if "goals" in data else {}
            kwargs.update({key: data[key] for key in ("selected_goal", "selection_reason", "result") if key in data})
            if project.project.verification_enabled:
                kwargs.update(producer_agent_id=producer_agent_id, producer_session_id=session or producer_session_id)
            response = client.complete(project.project.id, data["from"], data["description"], worker.name, **kwargs)
            if response.status_code == 403:
                LOG.info("project became inactive during reason complete project=%s worker=%s", project.project.id, worker.name)
                return "success"
            if (project.expansion or project.result_contract) and response.status_code in (409, 422):
                return recover_expansion(client, project, worker.name, response.text)
            if not response.ok:
                LOG.warning(
                    "reason complete write failed project=%s worker=%s status=%s body=%s",
                    project.project.id,
                    worker.name,
                    response.status_code,
                    response.text,
                )
                return "failed"
            link_response_result(client, session, project.project.id, response)
            LOG.info(
                "project completed project=%s worker=%s from=%s execute_ms=%s total_ms=%s",
                project.project.id,
                worker.name,
                data["from"],
                execute_ms,
                total_ms,
            )
            return "success"
        if kind == "intents":
            created = 0
            for intent_data in data:
                response = client.create_intent(project.project.id, intent_data["from"], intent_data["description"], worker.name)
                if response.status_code == 403:
                    LOG.info("project became inactive during reason intent create project=%s worker=%s created=%s", project.project.id, worker.name, created)
                    return "success"
                if response.status_code == 409:
                    LOG.info("reason intent lost race project=%s worker=%s from=%s", project.project.id, worker.name, intent_data["from"])
                    continue
                if not response.ok:
                    LOG.warning(
                        "reason intent write failed project=%s worker=%s status=%s body=%s",
                        project.project.id,
                        worker.name,
                        response.status_code,
                        response.text,
                    )
                    continue
                link_response_result(client, session, project.project.id, response)
                created += 1
                LOG.info(
                    "reason created intent project=%s worker=%s from=%s description=%s",
                    project.project.id,
                    worker.name,
                    intent_data["from"],
                    intent_data["description"],
                )
            LOG.info(
                "reason finished project=%s worker=%s created_intents=%s/%s execute_ms=%s total_ms=%s",
                project.project.id,
                worker.name,
                created,
                len(data),
                execute_ms,
                total_ms,
            )
            if created == 0:
                LOG.warning(
                    "reason created no intents project=%s worker=%s attempted=%s execute_ms=%s total_ms=%s",
                    project.project.id,
                    worker.name,
                    len(data),
                    execute_ms,
                    total_ms,
                )
                return "failed"
            return "success"
        LOG.info(
            "reason finished without graph change project=%s worker=%s execute_ms=%s total_ms=%s",
            project.project.id,
            worker.name,
            execute_ms,
            total_ms,
        )
        return "success"
    finally:
        lease.stop()
        best_effort_release_reason(client, project.project.id, worker.name)


def link_response_result(client, session, project_id, response):
    # The accepted server response supplies graph IDs. Never infer them from
    # timestamps, worker names, or the model's proposed text.
    if response.ok and isinstance(response.data, dict):
        identifier = response.data.get('id')
        if isinstance(identifier, str) and 'from' in response.data and 'to' in response.data:
            link_session_result(client, session, project_id,
                                intent_ids=[identifier], fact_ids=[response.data['to']] if response.data.get('to') else [])
