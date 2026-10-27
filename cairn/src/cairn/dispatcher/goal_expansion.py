from __future__ import annotations

import json

from cairn.server.models import ProjectDetail


def expansion_prompt(prompt: str, project: ProjectDetail) -> str:
    if project.expansion is None:
        return prompt
    instructions = (
        "\n\n# Goal expansion enabled\n"
        f"Only after the current goal is actually achieved, complete must contain goals: [] or at most {project.expansion.max_goals} "
        "independent follow-up goal objects. Each object requires a concise nonempty goal, at most 8000 characters, "
        "and may include id, title, kind, bootstrap_enabled, artifacts. Do not include depends_on or expansion. "
        "Use an explicit empty array when no follow-up goals are warranted. The server supplies dependencies, starts "
        "the new goals in parallel, and makes existing downstream stages wait for all of them. "
        "Do not claim the current goal is achieved just to propose more work.\n"
        'Example completion addition: "goals": [{"goal": "Investigate item A"}, {"goal": "Investigate item B"}]\n'
    )
    try:
        payload = json.loads(prompt)
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        payload["expansion"] = project.expansion.model_dump()
        payload["expansion_instructions"] = instructions
        return json.dumps(payload, ensure_ascii=False)
    return prompt + instructions


def recover_expansion(client, project: ProjectDetail, worker: str, error) -> str:
    message = " ".join(str(error).split())[:1_000]
    response = client.request_expansion_recovery(project.project.id, message, worker)
    if response.status_code == 403:
        return "success"  # workflow stopped/completed while the worker was running
    if not response.ok:
        raise RuntimeError(f"Could not record expansion correction: {response.status_code}")
    # Recovery lives in an open exploration intent. Mark this reason checkpoint
    # consumed so repeated completion attempts cannot starve corrective work.
    return "success"
