"""Colección `admins` del sistema original (se conserva como respaldo; solo el seed escribe aquí)."""
from __future__ import annotations

from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.shared.utilities.dates import utcnow


class AdminRepository:
    def __init__(self, db: AsyncIOMotorDatabase):
        self.col = db["admins"]

    async def find_by_username(self, username: str) -> dict[str, Any] | None:
        return await self.col.find_one({"username": username})

    async def create(self, username: str, password_hash: str) -> dict[str, Any]:
        now = utcnow()
        doc = {"username": username, "passwordHash": password_hash, "role": "admin",
               "createdAt": now, "updatedAt": now, "__v": 0}
        res = await self.col.insert_one(doc)
        return {"_id": res.inserted_id, **doc}
