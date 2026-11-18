"""Solicitudes de acceso: colaboradores (correo institucional sin ".est") que intentaron ingresar con Google.

No tienen cuenta hasta que el SUPERADMIN les da un rol (normalmente ADMIN, para revisar solicitudes).
"""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import Field
from pymongo.errors import DuplicateKeyError

from app.core.dependencies import AuthUser, require_superadmin
from app.core.exceptions import BadRequest, Conflict, NotFound
from app.database.connection import get_database
from app.modules.auth.schemas import StrictModel
from app.modules.superadmin.common import audit, page_args, paged
from app.modules.users import repository as users
from app.shared.serialization import to_json
from app.shared.utilities.dates import utcnow
from app.shared.validators.object_id import parse_object_id

router = APIRouter(prefix="/access-requests")
STATUSES = ("pending", "granted", "dismissed")


class GrantIn(StrictModel):
    role: Literal["ADMIN", "SUPERADMIN"] = "ADMIN"
    reason: str = Field(min_length=10, max_length=500)


class DismissIn(StrictModel):
    reason: str | None = Field(None, max_length=500)


def _row(r: dict[str, Any]) -> dict[str, Any]:
    return {"id": str(r["_id"]), "email": r.get("email"), "name": r.get("name"), "status": r.get("status", "pending"),
            "attempts": r.get("attempts", 1), "firstSeenAt": r.get("firstSeenAt"), "lastSeenAt": r.get("lastSeenAt"),
            "grantedRole": r.get("grantedRole"), "resolvedAt": r.get("resolvedAt")}


def col():
    return get_database()["access_requests"]


@router.get("")
async def list_requests(status: str = "pending", q: str | None = None, page: int = 1, pageSize: int = 20,
                        _: AuthUser = Depends(require_superadmin)):
    if status not in STATUSES + ("all",):
        raise BadRequest("Estado no válido")
    query: dict[str, Any] = {} if status == "all" else {"status": status}
    if q and q.strip():
        import re
        query["$or"] = [{"email": {"$regex": re.escape(q.strip()), "$options": "i"}},
                        {"name": {"$regex": re.escape(q.strip()), "$options": "i"}}]
    p, ps = page_args(page, pageSize)
    total = await col().count_documents(query)
    rows = await col().find(query).sort("lastSeenAt", -1).skip((p - 1) * ps).limit(ps).to_list(ps)
    pending = await col().count_documents({"status": "pending"})
    return to_json({**paged([_row(r) for r in rows], total, p, ps), "pending": pending})


async def _pending(id_: str) -> dict[str, Any]:
    r = await col().find_one({"_id": parse_object_id(id_)})
    if not r:
        raise NotFound("Solicitud de acceso no encontrada")
    if r.get("status") != "pending":
        raise Conflict("Esta solicitud de acceso ya fue atendida")
    return r


@router.post("/{id_}/grant", status_code=201)
async def grant(id_: str, body: GrantIn, request: Request, actor: AuthUser = Depends(require_superadmin)):
    """Crea la cuenta del colaborador con el rol elegido. Ingresa con Google (sin contraseña)."""
    r = await _pending(id_)
    try:
        u = await users.insert({"role": body.role, "email": r["email"], "username": None, "name": r.get("name"),
                                "passwordHash": None, "googleSub": r.get("googleSub"), "createdBy": actor.user_id})
    except DuplicateKeyError as exc:
        raise Conflict("Ya existe una cuenta con ese correo") from exc
    await col().update_one({"_id": r["_id"]}, {"$set": {"status": "granted", "grantedRole": body.role, "userId": u["_id"],
                                                        "resolvedAt": utcnow(), "resolvedBy": actor.user_id}})
    await audit(actor, request, "USER_CREATED", target=u,
                metadata={"role": body.role, "via": "access_request", "reason": body.reason.strip()})
    if body.role == "SUPERADMIN":
        await audit(actor, request, "USER_ROLE_CHANGED", target=u, metadata={"from": None, "to": "SUPERADMIN", "escalation": True})
    return to_json({"userId": str(u["_id"]), "role": body.role})


@router.post("/{id_}/dismiss")
async def dismiss(id_: str, body: DismissIn, request: Request, actor: AuthUser = Depends(require_superadmin)):
    """Descarta la solicitud (la persona sigue sin acceso). Si vuelve a intentar, no se reabre sola."""
    r = await _pending(id_)
    await col().update_one({"_id": r["_id"]}, {"$set": {"status": "dismissed", "resolvedAt": utcnow(),
                                                        "resolvedBy": actor.user_id, "dismissReason": body.reason}})
    await audit(actor, request, "ACCESS_REQUEST_DISMISSED", metadata={"email": r["email"], "reason": body.reason})
    return {"ok": True}
