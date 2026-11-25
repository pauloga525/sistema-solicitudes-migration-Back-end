"""Índices idénticos (mismo nombre, claves y unicidad) a los que crea Mongoose.

`create_index` es idempotente: si el índice ya existe con la misma definición
no hace nada, así que es seguro ejecutarlo contra la base de producción.
No se eliminan ni modifican índices existentes.
"""
from __future__ import annotations

from motor.motor_asyncio import AsyncIOMotorDatabase

INDEXES: dict[str, list[tuple[str, bool]]] = {
    "admins": [("username", True)],
    "requests": [("requestNumber", True), ("studentDni", False)],
    "counters": [("name", True)],
    "solicitudconfigs": [("requestType", True)],
    "aniolectivos": [("name", True)],
    "students": [("cedulaHash", True)],
}


async def ensure_indexes(db: AsyncIOMotorDatabase) -> None:
    for collection, specs in INDEXES.items():
        for key, unique in specs:
            kwargs = {"name": f"{key}_1"}
            if unique:
                kwargs["unique"] = True
            await db[collection].create_index([(key, 1)], **kwargs)


# ── Colecciones nuevas del panel SUPERADMIN y del área de estudiantes ─────────
async def ensure_new_indexes(db: AsyncIOMotorDatabase) -> None:
    """Solo crea índices en colecciones nuevas (no toca los del sistema original)."""
    users = db["users"]
    await users.create_index([("email", 1)], name="email_unique", unique=True,
                             partialFilterExpression={"email": {"$type": "string"}})
    await users.create_index([("username", 1)], name="username_unique", unique=True,
                             partialFilterExpression={"username": {"$type": "string"}})
    await users.create_index([("cedulaHash", 1)], name="cedulaHash_unique", unique=True,
                             partialFilterExpression={"cedulaHash": {"$type": "string"}})
    await users.create_index([("role", 1), ("isActive", 1)], name="role_active")
    await db["student_profiles"].create_index([("userId", 1)], name="userId_unique", unique=True)
    await db["student_profiles"].create_index([("cedulaHash", 1)], name="cedulaHash_unique", unique=True)
    await db["representatives"].create_index([("cedulaHash", 1)], name="cedulaHash_unique", unique=True)
    links = db["student_representatives"]
    await links.create_index([("studentUserId", 1), ("representativeId", 1)], name="active_link_unique", unique=True,
                             partialFilterExpression={"active": True})
    await links.create_index([("studentUserId", 1)], name="one_primary_per_student", unique=True,
                             partialFilterExpression={"active": True, "isPrimary": True})
    await links.create_index([("representativeId", 1)], name="representativeId")
    await db["request_events"].create_index([("requestId", 1), ("createdAt", 1)], name="request_createdAt")
    await db["request_events"].create_index([("createdAt", -1)], name="createdAt_desc")
    await db["user_events"].create_index([("actorId", 1), ("createdAt", -1)], name="actor_createdAt")
    await db["user_events"].create_index([("targetId", 1), ("createdAt", -1)], name="target_createdAt")
    await db["request_limits"].create_index([("requestType", 1), ("scope", 1)], name="type_scope_unique", unique=True)
    await db["limit_exceptions"].create_index([("studentUserId", 1), ("requestType", 1), ("windowKey", 1)],
                                              name="active_exception_unique", unique=True,
                                              partialFilterExpression={"isActive": True})
    await db["request_deadlines"].create_index([("requestType", 1), ("periodKey", 1)], name="type_period_unique", unique=True)
    await db["system_errors"].create_index([("resolved", 1), ("createdAt", -1)], name="resolved_createdAt")
    # Panel docente
    await db["teachers"].create_index([("nameKey", 1)], name="nameKey_unique", unique=True)
    await db["teachers"].create_index([("email", 1)], name="email", sparse=True)
    await db["teacher_assignments"].create_index([("course", 1), ("paralelo", 1), ("subject", 1), ("active", 1)], name="course_par_subject")
    await db["teacher_assignments"].create_index([("teacherId", 1), ("active", 1)], name="teacher_active")
    await db["teacher_tracking"].create_index([("requestId", 1), ("subject", 1)], name="request_subject_unique", unique=True)
    await db["teacher_tracking_events"].create_index([("requestId", 1), ("createdAt", 1)], name="request_created")
    await db["email_outbox"].create_index([("context.requestId", 1)], name="context_request")
    await db["notifications"].create_index([("requestId", 1)], name="request")
    await db["notifications"].create_index([("userId", 1), ("read", 1), ("createdAt", -1)], name="user_read_created")
    await db["email_outbox"].create_index([("status", 1), ("createdAt", -1)], name="status_created")
    # Colaboradores (correo sin ".est") que intentaron ingresar: el SUPERADMIN puede darles acceso.
    await db["access_requests"].create_index([("email", 1)], name="email_unique", unique=True)
    await db["access_requests"].create_index([("status", 1), ("lastSeenAt", -1)], name="status_lastSeen")
    # Campos aditivos en `requests` (los índices nuevos no cambian los existentes).
    await db["requests"].create_index([("studentUserId", 1), ("createdAt", -1)], name="studentUserId_createdAt")
    await db["requests"].create_index([("status", 1), ("dueAt", 1)], name="status_dueAt")
    await apply_retention(db)


async def _ttl(db: AsyncIOMotorDatabase, coll: str, field: str, name: str, seconds: int) -> None:
    """Crea el índice TTL o ajusta su expiración con collMod (no se borra ni recrea)."""
    existing = await db[coll].index_information()
    if name in existing:
        if existing[name].get("expireAfterSeconds") != seconds:
            await db.command({"collMod": coll, "index": {"name": name, "expireAfterSeconds": seconds}})
    else:
        await db[coll].create_index([(field, 1)], name=name, expireAfterSeconds=seconds)


async def apply_retention(db: AsyncIOMotorDatabase) -> None:
    from app.core.config import get_settings

    cfg = get_settings()
    row = await db["system_settings"].find_one({"_id": "singleton"}) or {}
    days_audit = int(row.get("userAuditRetentionDays") or cfg.user_audit_retention_days)
    days_err = int(row.get("errorRetentionDays") or cfg.error_log_retention_days)
    await _ttl(db, "user_events", "createdAt", "ttl_createdAt", days_audit * 86400)
    await _ttl(db, "system_errors", "createdAt", "ttl_createdAt", days_err * 86400)
    await _ttl(db, "rate_limits", "windowStart", "ttl_windowStart", cfg.rate_limit_retention_s)
    await _ttl(db, "limit_locks", "expiresAt", "ttl_expiresAt", 3600)
