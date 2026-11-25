"""Gestión de usuarios (SUPERADMIN): alta con contraseña temporal, edición, rol, estado, restablecimiento y accesos."""
from __future__ import annotations

import re
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import Field
from pymongo.errors import DuplicateKeyError

from app.core.config import get_settings
from app.core.dependencies import AuthUser, require_not_self, require_superadmin
from app.core.exceptions import AppError, BadRequest, Conflict, NotFound
from app.core.security import issue_temp_password
from app.database.connection import get_database, transaction
from app.modules.audit.service import ROLE_LABELS, USER_EVENT_LABELS
from app.modules.auth.schemas import StrictModel
from app.modules.students import service as st
from app.modules.superadmin.common import audit, page_args, paged
from app.modules.teachers.service import ensure_teacher_for_user, release_teacher_role
from app.modules.system.service import date_range, enforce_rate_limit
from app.modules.users import repository as users
from app.shared.crypto.field_encryption import hash_field
from app.shared.serialization import to_json
from app.shared.utilities.dates import utcnow
from app.shared.validators.object_id import parse_object_id

router = APIRouter(prefix="/users")
RoleLit = Literal["STUDENT", "TEACHER", "ADMIN", "SUPERADMIN"]
ACCESS_EVENTS = ["LOGIN_SUCCESS", "LOGIN_FAILED", "LOGOUT", "PASSWORD_CHANGED", "USER_CREATED", "USER_UPDATED",
                 "USER_DEACTIVATED", "USER_REACTIVATED", "USER_DELETED", "USER_PASSWORD_RESET", "USER_ROLE_CHANGED",
                 "PRIVILEGE_ESCALATION_BLOCKED", "STUDENT_REGISTERED"]
LAST_SA = "No puedes dejar el sistema sin un SUPERADMIN activo. Crea o activa otro antes."


class CreateUserIn(StrictModel):
    email: str = Field(min_length=5, max_length=254)
    name: str = Field(min_length=5, max_length=120)
    cedula: str = Field(min_length=10, max_length=10)
    role: RoleLit
    username: str | None = Field(None, min_length=3, max_length=40, pattern=r"^[a-zA-Z0-9._-]+$")


class UpdateUserIn(StrictModel):
    name: str | None = Field(None, min_length=5, max_length=120)
    email: str | None = Field(None, min_length=5, max_length=254)
    cedula: str | None = Field(None, min_length=10, max_length=10)


class RoleIn(StrictModel):
    role: RoleLit
    reason: str = Field(min_length=10, max_length=500)


class ReasonIn(StrictModel):
    reason: str = Field(min_length=10, max_length=500)


class DeleteIn(StrictModel):
    reason: str = Field(min_length=10, max_length=500)
    # Debe coincidir con el correo (o usuario) de la cuenta: evita borrar la equivocada.
    confirm: str = Field(min_length=3, max_length=254)


def _institutional(email: str) -> str:
    e = users.normalize_email(email)
    d = get_settings().institutional_email_domain
    if not re.fullmatch(r"[a-z0-9._%+-]+@" + re.escape(d), e):
        raise BadRequest(f"El correo debe ser institucional (@{d})")
    return e


def user_row(u: dict[str, Any]) -> dict[str, Any]:
    return {"id": str(u["_id"]), "role": u["role"], "roleLabel": ROLE_LABELS.get(u["role"], u["role"]),
            "email": u.get("email"), "username": u.get("username"), "name": u.get("name"),
            "isActive": u.get("isActive", True), "forcePasswordChange": bool(u.get("forcePasswordChange")),
            "tempPasswordExpiresAt": u.get("tempPasswordExpiresAt"), "lastLoginAt": u.get("lastLoginAt"),
            "createdAt": u.get("createdAt"), "legacy": bool(u.get("legacyAdminId")),
            "google": bool(u.get("googleSub")), "hasPassword": bool(u.get("passwordHash"))}


async def _target(id_: str) -> dict[str, Any]:
    u = await users.get(parse_object_id(id_))
    if not u:
        raise NotFound("Usuario no encontrado")
    return u


async def _assert_not_last_sa(target: dict[str, Any]) -> None:
    if target["role"] == "SUPERADMIN" and target.get("isActive", True) and await users.count_active_superadmins() <= 1:
        raise AppError(409, LAST_SA, code="LAST_SUPERADMIN")


@router.get("")
async def list_users(role: str | None = None, status: str | None = None, q: str | None = None, page: int = 1,
                     pageSize: int | None = None, _: AuthUser = Depends(require_superadmin)):
    p, size = page_args(page, pageSize)
    query: dict[str, Any] = {}
    if role in users.ROLES:
        query["role"] = role
    elif role == "staff":  # página "Administradores": administradores y super administradores
        query["role"] = {"$in": list(users.STAFF_ROLES)}
    if status in ("active", "inactive"):
        query["isActive"] = status == "active"
    if status == "pending":
        query["forcePasswordChange"] = True
    if q and q.strip():
        rx = {"$regex": re.escape(q.strip()[:80]), "$options": "i"}
        query["$or"] = [{"email": rx}, {"username": rx}, {"name": rx}]
    col = users.col()
    total = await col.count_documents(query)
    # Orden: SUPERADMIN, ADMIN, STUDENT; dentro de cada rol, los más recientes primero.
    rows = await col.aggregate([
        {"$match": query},
        {"$addFields": {"_ro": {"$indexOfArray": [["SUPERADMIN", "ADMIN", "TEACHER", "STUDENT"], "$role"]}}},
        {"$sort": {"_ro": 1, "createdAt": -1}}, {"$skip": (p - 1) * size}, {"$limit": size},
        {"$project": {"passwordHash": 0, "_ro": 0}}]).to_list(size)
    return to_json(paged([user_row(u) for u in rows], total, p, size))


@router.get("/{id_}")
async def get_user(id_: str, _: AuthUser = Depends(require_superadmin)):
    u = await _target(id_)
    out = user_row(u)
    out["cedula"] = st.dec(u.get("cedulaEnc"))
    if u["role"] == "STUDENT":
        prof = await st.get_profile(u["_id"])
        out["profileCompleted"] = bool(prof and prof.get("completed"))
    return to_json(out)


@router.post("", status_code=201)
async def create_user(body: CreateUserIn, request: Request, actor: AuthUser = Depends(require_superadmin)):
    await enforce_rate_limit("SA_CREATE_USER", actor.user_id, "Creaste muchos usuarios seguidos. Espera un momento.")
    email = _institutional(body.email)
    st.assert_cedula(body.cedula)
    name = re.sub(r"\s+", " ", body.name).strip()
    st.assert_person_name(name, "El nombre")
    h = hash_field(body.cedula)
    if await users.col().find_one({"cedulaHash": h}, {"_id": 1}) or await get_database()["student_profiles"].find_one({"cedulaHash": h}, {"_id": 1}):
        raise Conflict("Esa cédula ya pertenece a otra cuenta")
    if get_settings().allow_password_login:
        temp, pw = issue_temp_password()
    else:
        # Solo Google: la persona ingresa con su cuenta institucional; no hay contraseña temporal.
        temp, pw = None, {"passwordHash": None, "forcePasswordChange": False, "tempPasswordExpiresAt": None}
    try:
        u = await users.insert({"role": body.role, "email": email, "username": body.username or None, "name": name,
                                "cedulaEnc": st.enc(body.cedula), "cedulaHash": h, **pw, "createdBy": actor.user_id})
    except DuplicateKeyError as exc:
        raise Conflict("Ya existe un usuario con ese correo o nombre de usuario") from exc
    await audit(actor, request, "USER_CREATED", target=u, metadata={"role": body.role})
    if body.role == "TEACHER":
        await ensure_teacher_for_user(u)
    if body.role == "SUPERADMIN":
        await audit(actor, request, "USER_ROLE_CHANGED", target=u, metadata={"from": None, "to": "SUPERADMIN", "escalation": True})
    # La contraseña temporal se muestra UNA vez; no se guarda en claro ni se registra.
    return to_json({"user": user_row(u), "tempPassword": temp, "expiresAt": pw["tempPasswordExpiresAt"]})


@router.patch("/{id_}")
async def update_user(id_: str, body: UpdateUserIn, request: Request, actor: AuthUser = Depends(require_superadmin)):
    u = await _target(id_)
    fields: dict[str, Any] = {}
    if body.name is not None:
        name = re.sub(r"\s+", " ", body.name).strip()
        st.assert_person_name(name, "El nombre")
        fields["name"] = name
    if body.email is not None:
        fields["email"] = _institutional(body.email)
    if body.cedula is not None:
        st.assert_cedula(body.cedula)
        h = hash_field(body.cedula)
        if await users.col().find_one({"cedulaHash": h, "_id": {"$ne": u["_id"]}}, {"_id": 1}):
            raise Conflict("Esa cédula ya pertenece a otra cuenta")
        fields.update({"cedulaEnc": st.enc(body.cedula), "cedulaHash": h})
    if not fields:
        raise BadRequest("No hay cambios")
    try:
        await users.set_fields(u["_id"], fields)
    except DuplicateKeyError as exc:
        raise Conflict("Ya existe un usuario con ese correo") from exc
    await audit(actor, request, "USER_UPDATED", target=u, metadata={"fields": sorted(k for k in fields if not k.endswith(("Enc", "Hash")))})
    return to_json(user_row(await users.get(u["_id"])))


@router.post("/{id_}/role")
async def change_role(id_: str, body: RoleIn, request: Request, actor: AuthUser = Depends(require_superadmin)):
    u = await _target(id_)
    if actor.user_id == str(u["_id"]):
        await audit(actor, request, "PRIVILEGE_ESCALATION_BLOCKED", target=u, result="FAILURE", metadata={"to": body.role})
        require_not_self(actor.user_id, str(u["_id"]))
    if u["role"] == body.role:
        raise BadRequest("El usuario ya tiene ese rol")
    if body.role == "TEACHER" and get_settings().is_student_email(u.get("email") or ""):
        raise BadRequest("Una cuenta de estudiante (.est) no puede tener el rol de docente.")
    if body.role != "SUPERADMIN":
        await _assert_not_last_sa(u)
    async with transaction() as s:
        res = await users.col().update_one({"_id": u["_id"], "role": u["role"]},
                                           {"$set": {"role": body.role, "updatedAt": utcnow()}, "$inc": {"sessionVersion": 1}},
                                           session=s)
        if res.matched_count == 0:
            raise AppError(409, "El usuario cambió mientras lo editabas. Recarga.", code="STALE_STATE")
        await audit(actor, request, "USER_ROLE_CHANGED", target=u, session=s,
                    metadata={"from": u["role"], "to": body.role, "reason": body.reason,
                              "escalation": body.role == "SUPERADMIN" or (u["role"] in ("STUDENT", "TEACHER") and body.role == "ADMIN")})
    if body.role != "SUPERADMIN" and u["role"] == "SUPERADMIN" and await users.count_active_superadmins() == 0:
        # Carrera entre dos degradaciones simultáneas: se revierte la nuestra.
        await users.col().update_one({"_id": u["_id"]}, {"$set": {"role": "SUPERADMIN"}})
        raise AppError(409, LAST_SA, code="LAST_SUPERADMIN")
    if body.role == "TEACHER":
        await ensure_teacher_for_user(u)
    elif u["role"] == "TEACHER":
        await release_teacher_role(u)
    return to_json(user_row(await users.get(u["_id"])))


@router.post("/{id_}/deactivate")
async def deactivate(id_: str, body: ReasonIn, request: Request, actor: AuthUser = Depends(require_superadmin)):
    u = await _target(id_)
    require_not_self(actor.user_id, str(u["_id"]))
    if not u.get("isActive", True):
        raise BadRequest("El usuario ya está desactivado")
    await _assert_not_last_sa(u)
    await users.col().update_one({"_id": u["_id"]}, {"$set": {"isActive": False, "deactivatedAt": utcnow(),
                                                               "deactivatedBy": actor.user_id, "updatedAt": utcnow()},
                                                      "$inc": {"sessionVersion": 1}})
    if u["role"] == "SUPERADMIN" and await users.count_active_superadmins() == 0:
        await users.col().update_one({"_id": u["_id"]}, {"$set": {"isActive": True}})
        raise AppError(409, LAST_SA, code="LAST_SUPERADMIN")
    await audit(actor, request, "USER_DEACTIVATED", target=u, metadata={"reason": body.reason})
    return to_json(user_row(await users.get(u["_id"])))


@router.post("/{id_}/delete")
async def delete_user(id_: str, body: DeleteIn, request: Request, actor: AuthUser = Depends(require_superadmin)):
    """Elimina la cuenta de forma permanente.

    Se borran la cuenta, el perfil del estudiante, sus vínculos con representantes, los representantes
    que no queden vinculados a otro estudiante y sus excepciones de límite. Las solicitudes y la
    auditoría se conservan (son registro institucional); las solicitudes asignadas a un administrador
    eliminado vuelven a quedar sin responsable.
    """
    u = await _target(id_)
    require_not_self(actor.user_id, str(u["_id"]))
    expected = (u.get("email") or u.get("username") or "").strip().lower()
    if not expected or body.confirm.strip().lower() != expected:
        raise BadRequest("Escribe el correo de la cuenta para confirmar la eliminación")
    await _assert_not_last_sa(u)
    db = get_database()
    uid = u["_id"]
    links = await db["student_representatives"].find({"studentUserId": uid}, {"representativeId": 1}).to_list(None)
    rep_ids = list({ln["representativeId"] for ln in links if ln.get("representativeId")})
    requests_kept = await db["requests"].count_documents({"studentUserId": uid})
    async with transaction() as session:
        await db["student_profiles"].delete_many({"userId": uid}, session=session)
        await db["student_representatives"].delete_many({"studentUserId": uid}, session=session)
        await db["limit_exceptions"].delete_many({"studentUserId": uid}, session=session)
        shared = set(await db["student_representatives"].distinct(
            "representativeId", {"representativeId": {"$in": rep_ids}}, session=session)) if rep_ids else set()
        orphan = [r for r in rep_ids if r not in shared]
        if orphan:
            await db["representatives"].delete_many({"_id": {"$in": orphan}}, session=session)
        unassigned = (await db["requests"].update_many({"assignedTo": uid}, {"$set": {"assignedTo": None}},
                                                       session=session)).modified_count
        await db["users"].delete_one({"_id": uid}, session=session)
    await audit(actor, request, "USER_DELETED", target=u,
                metadata={"reason": body.reason.strip(), "requestsKept": requests_kept,
                          "representativesRemoved": len(orphan), "requestsUnassigned": unassigned})
    return {"deleted": True}


@router.post("/{id_}/reactivate")
async def reactivate(id_: str, request: Request, actor: AuthUser = Depends(require_superadmin)):
    u = await _target(id_)
    require_not_self(actor.user_id, str(u["_id"]))
    if u.get("isActive", True):
        raise BadRequest("El usuario ya está activo")
    await users.set_fields(u["_id"], {"isActive": True, "deactivatedAt": None, "deactivatedBy": None})
    await audit(actor, request, "USER_REACTIVATED", target=u)
    return to_json(user_row(await users.get(u["_id"])))


@router.post("/{id_}/reset-password")
async def reset_password(id_: str, request: Request, actor: AuthUser = Depends(require_superadmin)):
    if not get_settings().allow_password_login:
        raise BadRequest("El ingreso es solo con Google: no hay contraseñas que restablecer.")
    u = await _target(id_)
    require_not_self(actor.user_id, str(u["_id"]))
    await enforce_rate_limit("SA_RESET_PASS", actor.user_id, "Hiciste muchos restablecimientos seguidos. Espera un momento.")
    temp, pw = issue_temp_password()
    # Nueva huella de contraseña + versión de sesión: todas las sesiones abiertas del usuario caen.
    await users.col().update_one({"_id": u["_id"]}, {"$set": {**pw, "updatedAt": utcnow()}, "$inc": {"sessionVersion": 1}})
    await audit(actor, request, "USER_PASSWORD_RESET", target=u)
    return to_json({"tempPassword": temp, "expiresAt": pw["tempPasswordExpiresAt"]})


@router.get("/{id_}/access")
async def access_history(id_: str, page: int = 1, from_: str | None = Query(None, alias="from"), to: str | None = None,
                         _: AuthUser = Depends(require_superadmin)):
    u = await _target(id_)
    p, size = page_args(page)
    # Accesos y cambios de la cuenta (no las consultas de datos que hizo el usuario como SUPERADMIN).
    q: dict[str, Any] = {"$or": [{"actorId": str(u["_id"])}, {"targetId": str(u["_id"])}],
                         "eventType": {"$in": ACCESS_EVENTS}}
    rng = date_range(from_, to)
    if rng:
        q["createdAt"] = rng
    col = get_database()["user_events"]
    total = await col.count_documents(q)
    rows = await col.find(q).sort("createdAt", -1).skip((p - 1) * size).limit(size).to_list(size)
    return to_json(paged([{"id": str(e["_id"]), "eventType": e["eventType"], "eventLabel": USER_EVENT_LABELS.get(e["eventType"], e["eventType"]),
                           "result": e.get("result"), "actorEmail": e.get("actorEmail"), "actorIp": e.get("actorIp"),
                           "targetEmail": e.get("targetEmail"), "metadata": _safe_meta(e.get("metadata")),
                           "createdAt": e["createdAt"]} for e in rows], total, p, size))


def _safe_meta(m: Any) -> Any:
    return {k: v for k, v in m.items() if k not in ("password", "tempPassword")} if isinstance(m, dict) else None


