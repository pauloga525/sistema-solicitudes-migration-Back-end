"""Activación del sistema: fechas entre las que se pueden enviar solicitudes (lo define el administrador).

Sin fechas configuradas el envío no se restringe (comportamiento anterior). Las demás reglas siguen
aplicando: aporte activo del calendario, plazos y límites.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.core.exceptions import AppError
from app.modules.limits.service import _utc, fmt_local, to_local
from app.modules.system.service import get_settings_doc, update_settings
from app.shared.utilities.dates import utcnow


async def get_window(now: datetime | None = None) -> dict[str, Any]:
    s = await get_settings_doc()
    start, end = s.get("submissionStart"), s.get("submissionEnd")
    now = now or utcnow()
    configured = bool(start and end)
    is_open = (not configured) or (_utc(start) <= now <= _utc(end))
    state = "sin_fechas" if not configured else ("abierto" if is_open else ("proximo" if now < _utc(start) else "cerrado"))
    return {"configured": configured, "open": is_open, "state": state,
            "start": to_local(start) if start else None, "end": to_local(end) if end else None,
            "message": None if is_open else closed_message(start, end, now),
            "updatedAt": s.get("submissionUpdatedAt")}


def closed_message(start: datetime, end: datetime, now: datetime) -> str:
    if now < _utc(start):
        return (f"El envío de solicitudes todavía no está habilitado. Estará disponible del {fmt_local(start)} "
                f"al {fmt_local(end)}.")
    return f"El envío de solicitudes está cerrado. Estuvo habilitado del {fmt_local(start)} al {fmt_local(end)}."


async def assert_open() -> None:
    w = await get_window()
    if not w["open"]:
        raise AppError(400, w["message"], code="SUBMISSION_CLOSED")


async def set_window(start: datetime | None, end: datetime | None, actor_id: str) -> dict[str, Any]:
    await update_settings({"submissionStart": start, "submissionEnd": end, "submissionUpdatedAt": utcnow(),
                           "submissionUpdatedBy": actor_id})
    return await get_window()
