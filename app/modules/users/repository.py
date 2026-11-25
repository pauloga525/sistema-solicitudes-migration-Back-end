"""Colección `users`: cuentas de estudiantes, administradores y SUPERADMIN."""
from __future__ import annotations

from typing import Any

from bson import ObjectId

from app.database.connection import get_database
from app.shared.utilities.dates import utcnow

ROLES = ("STUDENT", "TEACHER", "ADMIN", "SUPERADMIN")
STAFF_ROLES = ("ADMIN", "SUPERADMIN")
ROLE_HOME = {"SUPERADMIN": "/superadmin/dashboard", "ADMIN": "/admin/dashboard", "TEACHER": "/docente", "STUDENT": "/estudiante"}


def col():
    return get_database()["users"]


def normalize_email(email: str) -> str:
    return email.strip().lower()


async def get(oid: ObjectId, projection: dict | None = None) -> dict[str, Any] | None:
    return await col().find_one({"_id": oid}, projection)


async def find_by_login(identifier: str) -> dict[str, Any] | None:
    ident = identifier.strip()
    if "@" in ident:
        return await col().find_one({"email": normalize_email(ident)})
    return await col().find_one({"username": ident})


async def insert(doc: dict[str, Any], session: Any = None) -> dict[str, Any]:
    now = utcnow()
    full = {"isActive": True, "forcePasswordChange": False, "tempPasswordExpiresAt": None, "lastLoginAt": None,
            **doc, "createdAt": now, "updatedAt": now}
    res = await col().insert_one(full, session=session)
    return {"_id": res.inserted_id, **full}


async def set_fields(oid: ObjectId, fields: dict[str, Any], session: Any = None) -> None:
    await col().update_one({"_id": oid}, {"$set": {**fields, "updatedAt": utcnow()}}, session=session)


async def count_active_superadmins() -> int:
    return await col().count_documents({"role": "SUPERADMIN", "isActive": True})
