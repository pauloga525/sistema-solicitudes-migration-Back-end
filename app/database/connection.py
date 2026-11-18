"""Conexión asíncrona a MongoDB con Motor y transacciones (si hay replica set)."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from app.core.config import Settings

log = logging.getLogger("app.db")


class Database:
    client: AsyncIOMotorClient | None = None
    db: AsyncIOMotorDatabase | None = None
    supports_transactions: bool = False


database = Database()


async def connect(settings: Settings) -> AsyncIOMotorDatabase:
    # tz_aware: las fechas se leen como UTC conscientes de zona (igual que Date de JS).
    database.client = AsyncIOMotorClient(settings.mongodb_uri, tz_aware=True, serverSelectionTimeoutMS=10_000)
    database.db = database.client.get_database(settings.database_name)
    hello = await database.client.admin.command("hello")
    database.supports_transactions = bool(hello.get("setName") or hello.get("msg") == "isdbgrid")
    if not database.supports_transactions:
        log.warning("MongoDB no es replica set: las operaciones críticas usan actualizaciones condicionales con "
                    "compensación en lugar de transacciones. Para producción active un replica set (ver docs/DESPLIEGUE.md).")
    return database.db


async def disconnect() -> None:
    if database.client is not None:
        database.client.close()
    database.client = None
    database.db = None


def get_database() -> AsyncIOMotorDatabase:
    if database.db is None:  # pragma: no cover
        raise RuntimeError("La base de datos no está inicializada")
    return database.db


@asynccontextmanager
async def transaction() -> AsyncIterator[Any]:
    """Abre una transacción si el servidor la soporta; si no, entrega None (el llamador compensa)."""
    if not database.supports_transactions or database.client is None:
        yield None
        return
    async with await database.client.start_session() as session:
        async with session.start_transaction():
            yield session
