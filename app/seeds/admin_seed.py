"""Seed idempotente del administrador inicial (equivalente a AuthService.seedAdmin)."""
from __future__ import annotations

import logging

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.config import Settings
from app.core.security import hash_password
from app.modules.auth.repository import AdminRepository

logger = logging.getLogger("app.seed")
DEV_DEFAULT_PASSWORD = "admin123"


async def seed_admin(db: AsyncIOMotorDatabase, settings: Settings) -> str:
    repo = AdminRepository(db)
    username = settings.admin_seed_username
    # Instalación existente: nunca se toca un admin ya creado.
    if await repo.find_by_username(username):
        return "exists"
    if not settings.admin_seed_password and settings.is_production:
        logger.error("No se creó el administrador inicial: falta la variable de entorno "
                     "ADMIN_SEED_PASSWORD en producción. Defínala y reinicie el servidor.")
        return "skipped"
    await repo.create(username, hash_password(settings.admin_seed_password or DEV_DEFAULT_PASSWORD))
    if settings.admin_seed_password:
        logger.info("✅ Admin inicial creado — usuario: %s", username)
    else:
        logger.warning("✅ Admin de desarrollo creado — usuario: %s (contraseña por defecto de desarrollo). "
                       "Defina ADMIN_SEED_PASSWORD antes de desplegar a producción.", username)
    return "created"
