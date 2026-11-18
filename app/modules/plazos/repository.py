from __future__ import annotations

from datetime import datetime
from typing import Any

from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo import ReturnDocument

from app.shared.utilities.dates import utcnow


class AnioLectivoRepository:
    """Colección `aniolectivos` (modelo AnioLectivo de Mongoose)."""

    def __init__(self, db: AsyncIOMotorDatabase):
        self.col = db["aniolectivos"]

    async def find_all(self) -> list[dict[str, Any]]:
        return await self.col.find().sort("startDate", -1).to_list(length=None)

    async def find_by_name(self, name: str) -> dict[str, Any] | None:
        return await self.col.find_one({"name": name})

    async def create(self, data: dict[str, Any]) -> dict[str, Any]:
        now = utcnow()
        doc = {**data, "createdAt": now, "updatedAt": now, "__v": 0}
        res = await self.col.insert_one(doc)
        return {"_id": res.inserted_id, **{k: v for k, v in doc.items() if k != "_id"}}

    async def update(self, oid: ObjectId, data: dict[str, Any]) -> dict[str, Any] | None:
        return await self.col.find_one_and_update({"_id": oid}, {"$set": {**data, "updatedAt": utcnow()}},
                                                  return_document=ReturnDocument.AFTER)

    async def delete(self, oid: ObjectId) -> dict[str, Any] | None:
        return await self.col.find_one_and_delete({"_id": oid})

    async def find_covering(self, date: datetime) -> list[dict[str, Any]]:
        return await self.col.find({"startDate": {"$lte": date}, "endDate": {"$gte": date}}).to_list(length=None)


class RequestReadRepository:
    """Lectura de `requests` para calcular el cupo consumido (sin dependencia circular)."""

    def __init__(self, db: AsyncIOMotorDatabase):
        self.col = db["requests"]

    async def find_for_quota(self, student_dni: str, request_type: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
        return await self.col.find({
            "studentDni": student_dni,
            "requestType": request_type,
            "status": {"$nin": ["rechazado", "cancelado"]},
            "createdAt": {"$gte": start, "$lte": end},
        }, {"payload": 1}).to_list(length=None)
