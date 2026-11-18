"""Arranque de cuentas: copia no destructiva de `admins` → `users` y seeds de ADMIN/SUPERADMIN."""
from __future__ import annotations

import logging

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.config import Settings
from app.core.security import hash_password_cost
from app.shared.utilities.dates import utcnow

log = logging.getLogger("app.seed")
DEV_SUPERADMIN_PASSWORD = "superadmin123"


async def migrate_legacy_admins(db: AsyncIOMotorDatabase) -> int:
    """Copia cada admin del sistema original a `users` (rol ADMIN) si aún no está. `admins` no se modifica."""
    copied = 0
    async for a in db["admins"].find({}):
        if await db["users"].find_one({"legacyAdminId": a["_id"]}) or await db["users"].find_one({"username": a["username"]}):
            continue
        now = utcnow()
        await db["users"].insert_one({
            "role": "SUPERADMIN" if a.get("role") == "SUPERADMIN" else "ADMIN", "username": a["username"], "email": None,
            "name": a["username"], "passwordHash": a["passwordHash"], "isActive": True, "forcePasswordChange": False,
            "tempPasswordExpiresAt": None, "lastLoginAt": None, "legacyAdminId": a["_id"],
            "createdAt": a.get("createdAt", now), "updatedAt": now})
        copied += 1
    if copied:
        log.info("Cuentas de administrador copiadas a 'users': %d", copied)
    return copied


async def seed_superadmin(db: AsyncIOMotorDatabase, settings: Settings) -> str:
    """Crea el SUPERADMIN inicial solo si no existe ninguno (idempotente)."""
    if await db["users"].find_one({"role": "SUPERADMIN"}):
        return "exists"
    if not settings.superadmin_seed_password and settings.is_production:
        log.error("No se creó el SUPERADMIN inicial: falta SUPERADMIN_SEED_PASSWORD en producción.")
        return "skipped"
    password = settings.superadmin_seed_password or DEV_SUPERADMIN_PASSWORD
    now = utcnow()
    await db["users"].insert_one({
        "role": "SUPERADMIN", "email": settings.superadmin_seed_email, "username": None, "name": "Super Administrador",
        "passwordHash": hash_password_cost(password, 12), "isActive": True,
        # Sin contraseña definida por variable de entorno se obliga a cambiarla en el primer ingreso.
        "forcePasswordChange": not settings.superadmin_seed_password, "tempPasswordExpiresAt": None,
        "lastLoginAt": None, "createdAt": now, "updatedAt": now})
    log.warning("SUPERADMIN inicial creado: %s%s", settings.superadmin_seed_email,
                "" if settings.superadmin_seed_password else " (contraseña de desarrollo; se exige cambiarla al ingresar)")
    return "created"
