from typing import Annotated

from fastapi import APIRouter, Path, Response

from cairn.server.db import get_conn
from cairn.server.runtime_models import DispatcherReport, RuntimeOverview, RuntimeOverrideUpdate, RuntimeSettingsState
from cairn.server.runtime_service import get_runtime_settings, runtime_overview, save_dispatcher_report, set_runtime_settings

router = APIRouter(tags=["runtime"])


@router.get("/runtime", response_model=RuntimeOverview)
def get_runtime():
    with get_conn() as conn:
        return runtime_overview(conn)


@router.get("/runtime/settings", response_model=RuntimeSettingsState)
def read_runtime_settings():
    with get_conn() as conn:
        return get_runtime_settings(conn)


@router.put("/runtime/settings", response_model=RuntimeOverview)
def update_runtime_settings(body: RuntimeOverrideUpdate):
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        return set_runtime_settings(conn, body)


@router.post("/runtime/dispatchers/{dispatcher_id}", status_code=204, response_class=Response)
def publish_dispatcher(
    dispatcher_id: Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]{1,64}$")], body: DispatcherReport,
):
    with get_conn() as conn:
        save_dispatcher_report(conn, dispatcher_id, body)
