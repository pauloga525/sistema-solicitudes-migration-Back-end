from __future__ import annotations

from typing import Any

from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.shared.utilities.dates import utcnow


class RequestRepository:
    """Colección `requests`."""

    def __init__(self, db: AsyncIOMotorDatabase):
        self.col = db["requests"]

    async def insert(self, doc: dict[str, Any]) -> dict[str, Any]:
        now = utcnow()
        full = {**doc, "createdAt": now, "updatedAt": now, "__v": 0}
        res = await self.col.insert_one(full)
        return {"_id": res.inserted_id, **{k: v for k, v in full.items() if k != "_id"}}

    async def find_active_for_duplicates(self, representative_dni: str, student_name: str, request_type: str,
                                         statuses: list[str]) -> list[dict[str, Any]]:
        return await self.col.find({
            "representativeDni": representative_dni,
            "studentName": student_name,
            "requestType": request_type,
            "status": {"$in": statuses},
        }).to_list(length=None)

    async def find_page(self, query: dict[str, Any], page: int, page_size: int) -> tuple[list[dict[str, Any]], int]:
        cursor = self.col.find(query).sort("createdAt", -1).skip((page - 1) * page_size).limit(page_size)
        data = await cursor.to_list(length=page_size)
        total = await self.col.count_documents(query)
        return data, total

    async def find_by_id(self, oid: ObjectId) -> dict[str, Any] | None:
        return await self.col.find_one({"_id": oid})

    async def set_fields(self, oid: ObjectId, fields: dict[str, Any]) -> dict[str, Any] | None:
        return await self.col.find_one_and_update({"_id": oid}, {"$set": {**fields, "updatedAt": utcnow()}},
                                                  return_document=ReturnDocument.AFTER)

    async def approve_if_status(self, oid: ObjectId, from_status: str, to_status: str) -> dict[str, Any] | None:
        """Cambio condicional atómico (solo si sigue en `from_status`)."""
        return await self.col.find_one_and_update({"_id": oid, "status": from_status},
                                                  {"$set": {"status": to_status, "updatedAt": utcnow()}},
                                                  return_document=ReturnDocument.AFTER)


class CounterRepository:
    """Colección `counters`: secuencia atómica por nombre (p. ej. "PM-2026")."""

    def __init__(self, db: AsyncIOMotorDatabase):
        self.col = db["counters"]

    async def next_seq(self, name: str) -> int:
        for attempt in range(3):
            try:
                doc = await self.col.find_one_and_update(
                    {"name": name},
                    {"$setOnInsert": {"__v": 0}, "$inc": {"seq": 1}},
                    upsert=True,
                    return_document=ReturnDocument.AFTER,
                )
                return int(doc["seq"])
            except DuplicateKeyError:  # dos upserts simultáneos del primer número del año
                if attempt == 2:
                    raise
        raise RuntimeError("unreachable")
