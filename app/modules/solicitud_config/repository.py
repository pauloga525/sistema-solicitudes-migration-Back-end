from __future__ import annotations

from typing import Any

from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.shared.utilities.dates import utcnow


class SolicitudConfigRepository:
    def __init__(self, db: AsyncIOMotorDatabase):
        self.col = db["solicitudconfigs"]

    async def find_by_type(self, request_type: str) -> dict[str, Any] | None:
        return await self.col.find_one({"requestType": request_type})

    async def create(self, request_type: str, seed: dict[str, list[dict]]) -> dict[str, Any]:
        now = utcnow()
        doc = {"requestType": request_type, "courses": seed["courses"], "subjects": seed["subjects"],
               "createdAt": now, "updatedAt": now, "__v": 0}
        try:
            res = await self.col.insert_one(doc)
            return {"_id": res.inserted_id, **{k: v for k, v in doc.items() if k != "_id"}}
        except DuplicateKeyError:  # otra petición lo creó en paralelo
            existing = await self.find_by_type(request_type)
            assert existing is not None
            return existing

    async def set_item_active(self, config_id: ObjectId, array_field: str, name: str, active: int) -> dict[str, Any] | None:
        # Equivalente a `item.active = x; config.save()`: modifica solo el primer ítem con ese nombre.
        return await self.col.find_one_and_update(
            {"_id": config_id, f"{array_field}.name": name},
            {"$set": {f"{array_field}.$.active": active, "updatedAt": utcnow()}},
            return_document=ReturnDocument.AFTER,
        )

    async def add_subjects(self, config_id: ObjectId, items: list[dict]) -> dict[str, Any] | None:
        """Agrega materias que aún no existen (no modifica ni elimina las actuales)."""
        names = [i["name"] for i in items]
        return await self.col.find_one_and_update(
            {"_id": config_id, "subjects.name": {"$nin": names}},
            {"$push": {"subjects": {"$each": items}}, "$set": {"updatedAt": utcnow()}},
            return_document=ReturnDocument.AFTER,
        )

    async def add_course_subjects(self, config_id: ObjectId, items: list[dict]) -> dict[str, Any] | None:
        """Agrega pares curso/materia nuevos (solo si aún no hay ninguno de esos pares)."""
        cond = [{"courseSubjects": {"$not": {"$elemMatch": {"key": i["key"], "name": i["name"]}}}} for i in items[:1]]
        return await self.col.find_one_and_update(
            {"_id": config_id, **(cond[0] if cond else {})},
            {"$push": {"courseSubjects": {"$each": items}}, "$set": {"updatedAt": utcnow()}},
            return_document=ReturnDocument.AFTER,
        )

    async def set_course_subject(self, config_id: ObjectId, key: str, name: str, active: int) -> dict[str, Any] | None:
        return await self.col.find_one_and_update(
            {"_id": config_id, "courseSubjects": {"$elemMatch": {"key": key, "name": name}}},
            {"$set": {"courseSubjects.$[e].active": active, "updatedAt": utcnow()}},
            array_filters=[{"e.key": key, "e.name": name}], return_document=ReturnDocument.AFTER,
        )

    async def set_subject_all_courses(self, config_id: ObjectId, name: str, active: int) -> None:
        """La activación general de una materia se aplica a todos los cursos que la tienen."""
        await self.col.update_one({"_id": config_id, "courseSubjects.name": name},
                                  {"$set": {"courseSubjects.$[e].active": active}}, array_filters=[{"e.name": name}])

    async def activate_all_courses_once(self, config_id: ObjectId, marker: str) -> dict[str, Any] | None:
        """Activa todos los cursos una única vez (queda la marca; después el administrador decide)."""
        return await self.col.find_one_and_update(
            {"_id": config_id, marker: {"$exists": False}},
            {"$set": {"courses.$[].active": 1, marker: utcnow(), "updatedAt": utcnow()}},
            return_document=ReturnDocument.AFTER,
        )
