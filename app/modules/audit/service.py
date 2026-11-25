"""Auditoría append-only: eventos de solicitudes y de usuarios (patrón PUM Web).

Este módulo solo inserta y lee; no existe ninguna función que edite o borre eventos
(la retención de eventos de usuario la hace un índice TTL de MongoDB).
"""
from __future__ import annotations

import logging
from typing import Any

from bson import ObjectId

from app.database.connection import get_database
from app.shared.utilities.dates import utcnow

log = logging.getLogger("app.audit")

REQUEST_EVENT_LABELS = {
    "REQUEST_CREATED": "Solicitud creada",
    "AUTO_APPROVED": "Aprobada automáticamente al abrir el detalle",
    "AUTO_APPROVED_ON_SCHEDULE": "Aprobada al programar la evaluación",
    "STATUS_CHANGED": "Estado cambiado",
    "STATUS_CORRECTED": "Estado corregido por SUPERADMIN",
    "CANCELLED_BY_STUDENT": "Cancelada por el estudiante",
    "ASSIGNED": "Responsable asignado",
    "DUE_SET": "Plazo de atención asignado",
    "DUE_CLEARED": "Plazo de atención retirado",
    "REQUEST_DELETED": "Solicitud eliminada por SUPERADMIN",
}

USER_EVENT_LABELS = {
    "LOGIN_SUCCESS": "Inicio de sesión", "LOGIN_FAILED": "Intento de inicio fallido", "LOGOUT": "Cierre de sesión",
    "STUDENT_REGISTERED": "Estudiante registrado", "PROFILE_UPDATED": "Perfil actualizado",
    "PASSWORD_CHANGED": "Contraseña cambiada",
    "USER_CREATED": "Usuario creado", "USER_UPDATED": "Usuario actualizado", "USER_DEACTIVATED": "Usuario desactivado", "USER_DELETED": "Usuario eliminado", "ACCESS_REQUEST_DISMISSED": "Solicitud de acceso descartada",
    "SUBMISSION_WINDOW_UPDATED": "Fechas de envío actualizadas",
    "TEACHERS_IMPORTED": "Docentes importados", "TEACHER_EMAIL_SET": "Correo de docente actualizado",
    "TEACHER_ASSIGNMENT_ADDED": "Materia asignada a docente", "TEACHER_ASSIGNMENT_REMOVED": "Materia quitada a docente",
    "STUDENT_TEST_ACCOUNT": "Cuenta de prueba (estudiante)", "REQUEST_DELETED": "Solicitud eliminada",
    "MAIL_TEST": "Correo de prueba", "MAIL_RETRY": "Reintento de correos fallidos",
    "USER_REACTIVATED": "Usuario reactivado", "USER_PASSWORD_RESET": "Contraseña restablecida",
    "USER_ROLE_CHANGED": "Rol cambiado", "PRIVILEGE_ESCALATION_BLOCKED": "Intento de escalamiento bloqueado",
    "LINK_CREATED": "Representante vinculado", "LINK_REMOVED": "Representante desvinculado",
    "LINK_PRIMARY_SET": "Representante principal cambiado",
    "LIMIT_CHANGED": "Límite modificado", "LIMIT_EXCEPTION_GRANTED": "Excepción de límite otorgada",
    "LIMIT_EXCEPTION_REVOKED": "Excepción de límite revocada", "DEADLINE_CHANGED": "Plazo de envío modificado",
    "SETTINGS_CHANGED": "Configuración modificada", "MAINTENANCE_MODE_ENABLED": "Mantenimiento activado",
    "MAINTENANCE_MODE_DISABLED": "Mantenimiento desactivado", "ERROR_RESOLVED": "Error marcado como resuelto",
    "ERROR_REOPENED": "Error reabierto", "SENSITIVE_DATA_VIEWED": "Consulta de datos personales cifrados",
    "EXPORT": "Exportación",
    "PROFILE_UNLOCKED": "Edición de datos habilitada", "PROFILE_LOCKED": "Edición de datos bloqueada",
}

ROLE_LABELS = {"STUDENT": "Estudiante", "TEACHER": "Docente", "ADMIN": "Administrador", "SUPERADMIN": "Super Administrador", "SYSTEM": "Sistema"}


async def log_request_event(request_id: ObjectId, event_type: str, *, from_status: str | None = None,
                            to_status: str | None = None, actor_id: str | None = None, actor_name: str | None = None,
                            actor_role: str | None = None, actor_ip: str | None = None, comment: str | None = None,
                            session: Any = None, strict: bool = False) -> None:
    """Con `session` o `strict`, un fallo propaga la excepción (y revierte la transacción)."""
    doc = {"requestId": request_id, "eventType": event_type, "fromStatus": from_status, "toStatus": to_status,
           "actorId": actor_id, "actorName": actor_name, "actorRole": actor_role, "actorIp": actor_ip,
           "comment": comment, "createdAt": utcnow()}
    col = get_database()["request_events"]
    if session is not None or strict:
        await col.insert_one(doc, session=session)
        return
    try:
        await col.insert_one(doc)
    except Exception as exc:  # noqa: BLE001 — un evento informativo no debe romper la operación
        log.error("No se pudo registrar el evento de solicitud %s: %s", event_type, exc)


async def log_user_event(event_type: str, *, result: str = "SUCCESS", actor_id: str | None = None,
                         actor_email: str | None = None, actor_role: str | None = None, actor_ip: str | None = None,
                         target_id: str | None = None, target_email: str | None = None, target_role: str | None = None,
                         metadata: dict[str, Any] | None = None, session: Any = None) -> None:
    doc = {"eventType": event_type, "result": result, "actorId": actor_id, "actorEmail": actor_email,
           "actorRole": actor_role, "actorIp": actor_ip, "targetId": target_id, "targetEmail": target_email,
           "targetRole": target_role, "metadata": metadata, "createdAt": utcnow()}
    try:
        await get_database()["user_events"].insert_one(doc, session=session)
    except Exception as exc:  # noqa: BLE001
        if session is not None:
            raise
        log.error("No se pudo registrar el evento de usuario %s: %s", event_type, exc)


async def request_history(request_id: ObjectId) -> list[dict[str, Any]]:
    rows = await get_database()["request_events"].find({"requestId": request_id}).sort("createdAt", 1).to_list(1000)
    return [{"id": str(e["_id"]), "eventType": e["eventType"], "eventLabel": REQUEST_EVENT_LABELS.get(e["eventType"], e["eventType"]),
             "fromStatus": e.get("fromStatus"), "toStatus": e.get("toStatus"), "actorName": e.get("actorName"),
             "actorRole": e.get("actorRole"), "actorRoleLabel": ROLE_LABELS.get(e.get("actorRole") or "", e.get("actorRole")),
             "actorIp": e.get("actorIp"), "comment": e.get("comment"), "createdAt": e["createdAt"]} for e in rows]
