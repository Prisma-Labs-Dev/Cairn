from __future__ import annotations

from wayline.dispatcher.workers.adapters import ClaudeCodeDriver, CodexDriver, MockDriver, PiDriver
from wayline.dispatcher.workers.base import WorkerDriver


_CLAUDE = ClaudeCodeDriver()

DRIVERS: dict[str, WorkerDriver] = {
    "claudecode": _CLAUDE,
    "codex": CodexDriver(),
    "pi": PiDriver(),
    "mock": MockDriver(),
}

# Local variants invoke the host CLIs in their native configuration (no wayline provider
# injection). Local mock processes reuse the installed interpreter; containers use python3.
LOCAL_DRIVERS: dict[str, WorkerDriver] = {
    "claudecode": _CLAUDE,
    "codex": CodexDriver(local=True),
    "pi": PiDriver(local=True),
    "mock": MockDriver(local=True),
}


def get_driver(name: str, execution: str = "container") -> WorkerDriver:
    drivers = LOCAL_DRIVERS if execution == "local" else DRIVERS
    return drivers[name]
