from __future__ import annotations

from typing import Any

from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.shared.utilities.dates import utcnow


class StudentRepository:
    """Colección `students`. Solo escritura: el padrón no tiene endpoints de lectura (deliberado)."""

    def __init__(self, db: AsyncIOMotorDatabase):
        self.col = db["students"]

    async def find_by_hash(self, cedula_hash: str) -> dict[str, Any] | None:
        return await self.col.find_one({"cedulaHash": cedula_hash}, {"_id": 1})

    async def insert(self, data: dict[str, Any]) -> None:
        now = utcnow()
        await self.col.insert_one({**{k: v for k, v in data.items() if v is not None},
                                   "createdAt": now, "updatedAt": now, "__v": 0})

    async def update(self, oid: ObjectId, data: dict[str, Any]) -> None:
        # Igual que `doc.save()` de Mongoose: los campos que quedan undefined se eliminan.
        set_fields = {k: v for k, v in data.items() if v is not None}
        unset_fields = {k: "" for k, v in data.items() if v is None}
        update: dict[str, Any] = {"$set": {**set_fields, "updatedAt": utcnow()}}
        if unset_fields:
            update["$unset"] = unset_fields
        await self.col.update_one({"_id": oid}, update)
