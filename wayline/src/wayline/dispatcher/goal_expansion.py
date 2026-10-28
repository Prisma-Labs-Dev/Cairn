from __future__ import annotations

import json

from wayline.server.models import ProjectDetail


def expansion_prompt(prompt: str, project: ProjectDetail) -> str:
    instructions = (
        "\n\n# Outcome and approach\n"
        "Keep the requested outcome and explicit hard constraints unchanged. Adapt methods when evidence changes. "
        "A suggested recipe, tool, or ingredient is a preference only when the user has not made it a requirement. "
        "If maple syrup is preferred and honey is allowed, unavailable syrup does not make pancakes impossible. "
        "Required independent work, such as fetching milk and honey from different shops, may run as parallel intents. "
        "Mutually exclusive alternatives must be chosen before acting; do not execute every alternative.\n"
    )
    if project.choice_option:
        instructions += (
            "\n# Selected choice option\n"
            "You are executing the one selected alternative. Try feasible approaches that preserve its goal and hard constraints. "
            "If confirmed evidence shows this option cannot meet its goal, report unavailable instead of completion. "
            "State which goal or hard constraint cannot be met and why; cite observed evidence. "
            "Reason output: {\"accepted\":true,\"data\":{\"unavailable\":{\"from\":[\"f001\"],\"description\":\"Unmet goal and observed reason\"}}}. "
            "Bootstrap output: {\"accepted\":true,\"data\":{\"fact\":{\"description\":\"Observed evidence\"},\"unavailable\":{\"description\":\"Unmet goal and observed reason\"}}}. "
            "Do not declare unavailable merely because a preferred method failed. The server chooses the next ranked untried alternative.\n"
        )
    if project.result_contract:
        instructions += "\n# Typed stage outcome\nAt completion return complete.result with every declared field and no extra fields: " + json.dumps({name: field.model_dump(exclude_none=True) for name, field in project.result_contract.items()}) + ". A negative finding may satisfy the goal. Do not invent evidence.\n"
    if project.expansion:
        instructions += (
            "\n# Goal expansion enabled\n"
            f"Only after the current goal is actually achieved, complete must contain an explicit goals array of at most {project.expansion.max_goals} "
            "goal objects. Each requires a concise nonempty goal, at most 8000 characters, and may include id, title, kind, bootstrap_enabled, artifacts. "
            "Do not include depends_on or expansion. Proposed work never replaces actually achieving the current goal. "
        )
        if project.expansion.mode == "one":
            instructions += (
                "These goals are ranked mutually exclusive alternatives that each meet the requested outcome and hard constraints. "
                "Give each an explicit unique id, list best alternatives first, and include selected_goal with one member id plus selection_reason explaining evidence and preferences. "
                "Only that option executes. Unchosen options remain inactive; fallback tries the best untried option only after an explicit unavailable report. "
                "If no viable alternative exists, return goals: [] without selected_goal and explain why in selection_reason. This pauses for the operator.\n"
                'Example: "goals":[{"id":"pancakes","goal":"Make suitable pancakes."},{"id":"eggs","goal":"Make suitable eggs."}],"selected_goal":"pancakes","selection_reason":"Pancakes meet the stated preference and constraints."\n'
            )
        else:
            instructions += (
                "All proposed goals are required and run in parallel. Following stages wait for every generated goal. "
                "Use goals: [] when no follow-up work is warranted. Do not propose mutually exclusive alternatives in all mode.\n"
            )
    try:
        payload = json.loads(prompt)
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        if project.expansion:
            payload["expansion"] = project.expansion.model_dump()
        if project.choice_option:
            payload["choice_option"] = True
        payload["expansion_instructions"] = instructions
        return json.dumps(payload, ensure_ascii=False)
    return prompt + instructions


def recover_expansion(client, project: ProjectDetail, worker: str, error) -> str:
    message = " ".join(str(error).split())[:1_000]
    response = (client.request_result_recovery(project.project.id, message, worker) if project.result_contract else client.request_expansion_recovery(project.project.id, message, worker))
    if response.status_code == 403:
        return "success"  # workflow stopped/completed while the worker was running
    if not response.ok:
        raise RuntimeError(f"Could not record expansion correction: {response.status_code}")
    # Recovery lives in an open exploration intent. Mark this reason checkpoint
    # consumed so repeated completion attempts cannot starve corrective work.
    return "success"
