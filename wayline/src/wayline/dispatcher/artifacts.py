from __future__ import annotations

from wayline.dispatcher.protocol.client import WaylineClient
from wayline.dispatcher.runtime.backend import ExecutionBackend
from wayline.server.artifact_models import MAX_ARTIFACT_BYTES
from wayline.server.models import ProjectDetail


def capture_required_artifacts(
    client: WaylineClient, backend: ExecutionBackend, project: ProjectDetail, worker_name: str,
) -> bool:
    """Collect declared outputs before accepting goal completion.

    False means exploration/recovery is pending, or the project stopped. Transport errors raise so
    callers retry rather than advancing a reasoning checkpoint with no corrective work.
    """
    if not project.required_artifacts:
        return True
    current = client.get_project(project.project.id)
    if current.project.status != "active":
        return False
    # Finish the current explorations before collecting outputs. Unclaimed intents
    # also block: the scheduler could start one while this task reads the files.
    if any(item.to is None for item in current.intents):
        return False
    runtime = backend.ensure_running(project.project.runtime_id)
    problems = []
    for requirement in project.required_artifacts:
        try:
            content = backend.read_file(runtime, requirement.path, MAX_ARTIFACT_BYTES)
        except (OSError, ValueError) as exc:
            problems.append(f"{requirement.path}: {exc}")
            continue
        response = client.upload_artifact(project.project.id, requirement.path, content)
        if response.ok:
            continue
        if response.status_code in (403, 409):
            current = client.get_project(project.project.id)
            if current.project.status != "active":
                return False
        if response.status_code == 0 or response.status_code >= 500:
            raise RuntimeError(f"artifact upload failed: {response.status_code} {response.text}")
        problems.append(f"{requirement.path}: upload rejected ({response.status_code})")
    if not problems:
        return True
    paths = ", ".join(item.path for item in project.required_artifacts)
    correction = (
        "Collection errors:\n" + "\n".join(problems) + "\n"
        f"Produce the required artifact files before declaring this stage complete: {paths}. "
        "Files must be regular files inside the workflow working directory, without symlinks, "
        "and each must be at most 10 MiB. Preserve the stage's other completion criteria."
    )
    result = client.request_artifact_recovery(current.project.id, correction[:10000], worker_name)
    if not result.ok:
        if result.status_code == 403:
            return False
        raise RuntimeError(f"could not schedule artifact recovery: {result.status_code}")
    return False
