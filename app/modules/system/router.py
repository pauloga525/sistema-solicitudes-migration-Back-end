"""Rutas públicas de sistema: estado de mantenimiento y salud."""
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from app.core.dependencies import AuthUser, client_ip, require_admin
from app.core.exceptions import BadRequest
from app.modules.audit.service import log_user_event
from app.modules.system import submission_window as sw

from app.database.connection import database, get_database
from app.modules.system.service import get_maintenance

router = APIRouter(tags=["sistema"])


@router.get("/system/maintenance")
async def maintenance():
    m = await get_maintenance()
    return {"enabled": m["enabled"], "message": m["message"]}


@router.get("/health")
async def health():
    try:
        await get_database().command("ping")
        db_ok = True
    except Exception:  # noqa: BLE001
        db_ok = False
    return {"status": "ok" if db_ok else "degraded", "database": db_ok, "transactions": database.supports_transactions}


@router.get("/system/submission-window")
async def submission_window():
    """Estado del envío de solicitudes (público: lo usa el formulario del estudiante)."""
    w = await sw.get_window()
    return {k: w[k] for k in ("configured", "open", "state", "start", "end", "message")}


class WindowIn(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    start: str | None = Field(None, max_length=40)
    end: str | None = Field(None, max_length=40)


@router.put("/system/submission-window")
async def set_submission_window(body: WindowIn, request: Request, user: AuthUser = Depends(require_admin)):
    """Activación del sistema (ADMIN o SUPERADMIN): ambas fechas, o ninguna para quitar la restricción."""
    from app.modules.limits.service import parse_local
    if bool(body.start) != bool(body.end):
        raise BadRequest("Indica la fecha de inicio y la de cierre, o deja ambas vacías para no restringir el envío.")
    start = parse_local(body.start) if body.start else None
    end = parse_local(body.end) if body.end else None
    if start and end and start >= end:
        raise BadRequest("La fecha de cierre debe ser posterior a la de inicio.")
    w = await sw.set_window(start, end, user.user_id)
    await log_user_event("SUBMISSION_WINDOW_UPDATED", actor_id=user.user_id, actor_email=user.email or user.username,
                         actor_role=user.role, actor_ip=client_ip(request), metadata={"start": w["start"], "end": w["end"]})
    return w
