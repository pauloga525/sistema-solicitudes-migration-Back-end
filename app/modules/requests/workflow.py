"""Operaciones de solicitudes que combinan cambio de datos + auditoría de forma atómica.

Con replica set se usa una transacción; sin él, una actualización condicionada al estado actual y,
si el evento de auditoría no se puede guardar, se revierte el cambio (compensación).
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from bson import ObjectId
from pymongo import ReturnDocument

from app.core.dependencies import AuthUser
from app.core.exceptions import AppError, NotFound
from app.database.connection import get_database, transaction
from app.modules.audit.service import log_request_event
from app.modules.limits import service as limits
from app.modules.requests.request_type_config import REQUEST_TYPE_METADATA
from app.modules.requests.schemas import CreateRequestDTO
from app.modules.requests.service import RequestsService
from app.modules.system.service import get_settings_doc
from app.shared.utilities.dates import utcnow

log = logging.getLogger("app.requests")
CONFLICT_MSG = "La solicitud cambió mientras la editabas. Recarga la página e inténtalo de nuevo."


def actor_name(user: AuthUser | None) -> str | None:
    return None if user is None else (user.name or user.username)


async def update_with_event(oid: ObjectId, expected: dict[str, Any], set_fields: dict[str, Any], event: dict[str, Any],
                            unset_fields: list[str] | None = None) -> dict[str, Any]:
    """Aplica `set_fields` solo si el documento cumple `expected` y registra `event` en la misma unidad."""
    col = get_database()["requests"]
    update: dict[str, Any] = {"$set": {**set_fields, "updatedAt": utcnow()}}
    if unset_fields:
        update["$unset"] = {k: "" for k in unset_fields}
    async with transaction() as session:
        before = await col.find_one_and_update({"_id": oid, **expected}, update, session=session,
                                               return_document=ReturnDocument.BEFORE)
        if before is None:
            if not await col.find_one({"_id": oid}, {"_id": 1}, session=session):
                raise NotFound(f"Solicitud con id {oid} no encontrada")
            raise AppError(409, CONFLICT_MSG, code="STALE_STATE")
        try:
            await log_request_event(oid, session=session, strict=True, **event)
        except Exception:
            if session is None:  # compensación: se restaura lo que había
                restore = {k: before.get(k) for k in list(set_fields) + (unset_fields or []) if k in before}
                drop = [k for k in list(set_fields) if k not in before]
                comp: dict[str, Any] = {"$set": {**restore, "updatedAt": before.get("updatedAt")}}
                if drop:
                    comp["$unset"] = {k: "" for k in drop}
                await col.update_one({"_id": oid}, comp)
                log.error("Evento de auditoría no guardado; se revirtió el cambio en %s", oid)
            raise
    after = dict(before)
    after.update(set_fields)
    after["updatedAt"] = update["$set"]["updatedAt"]
    for k in unset_fields or []:
        after.pop(k, None)
    new_status = set_fields.get("status")
    if new_status in ("rechazado", "cancelado") and before.get("status") != new_status:
        from app.modules.teachers.service import on_request_closed
        await on_request_closed(after, new_status)
    return after


def status_event(event_type: str, before: str, after: str, user: AuthUser | None, ip: str | None,
                 comment: str | None = None) -> dict[str, Any]:
    return {"event_type": event_type, "from_status": before, "to_status": after,
            "actor_id": user.user_id if user else None, "actor_name": actor_name(user) or "Sistema",
            "actor_role": user.role if user else "SYSTEM", "actor_ip": ip, "comment": comment}


async def create_request(svc: RequestsService, dto: CreateRequestDTO, user: AuthUser, ip: str | None) -> dict[str, Any]:
    """Creación del estudiante: candado → plazo → límites → reglas originales → inserción → evento."""
    from app.modules.system.submission_window import assert_open
    await assert_open()  # activación del sistema (fechas que define el administrador)
    now = utcnow()
    period = await limits.current_period(now)
    async with limits.student_lock(dto.studentDni, dto.requestType):
        await limits.assert_within_deadline(dto.requestType, period, now)
        await limits.assert_within_limits(dto.studentDni, user.oid, dto.requestType, period, now)
        # Una vez por materia y por trimestre (también si no se presentó o si la canceló).
        used = await limits.subjects_used_in_trimester(dto.studentDni, dto.requestType, period)
        repeated = [s for s in dto.payload["subjects"] if s in used]
        if repeated:
            raise AppError(400, f"Ya solicitaste la evaluación de mejora de {', '.join(repeated)} en el "
                                f"{period['trimestre'] if period else 'trimestre'}. Solo se permite una vez por materia en cada "
                                "trimestre; podrás volver a pedirla en el siguiente.", code="SUBJECT_USED_IN_TRIMESTER")
        extra: dict[str, Any] = {"studentUserId": user.oid, "assignedTo": None,
                                 "periodKey": period["key"] if period else None}
        from app.database.connection import get_database
        if (await get_database()["users"].find_one({"_id": user.oid}, {"testAccount": 1}) or {}).get("testAccount"):
            extra["isTest"] = True  # cuenta de prueba: puede pedir la materia de prueba; la solicitud queda marcada
        days = (await get_settings_doc()).get("dueDaysDefault")
        extra["dueAt"] = now + timedelta(days=int(days)) if days else None
        doc = await svc.create(dto, extra=extra)
    initial = REQUEST_TYPE_METADATA[dto.requestType].initial_status.value
    await log_request_event(doc["_id"], "REQUEST_CREATED", to_status=initial, actor_id=user.user_id,
                            actor_name=dto.studentName, actor_role=user.role, actor_ip=ip)
    from app.modules.teachers.service import notify_representative_created, on_request_created
    await on_request_created(doc)  # aviso y correo a los docentes de cada materia (nunca falla la creación)
    await notify_representative_created(doc)  # correo al representante con estudiante, curso, fecha y materias
    return doc
