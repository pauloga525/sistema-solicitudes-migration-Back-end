"""Docentes (SUPERADMIN): importación del distributivo + correos, lista y correo de cada docente.

La importación se hace en dos pasos: vista previa (se guarda un borrador) y confirmación. Confirmar no borra nada:
las asignaciones anteriores quedan inactivas y los docentes que ya no aparecen se marcan inactivos.
"""
from __future__ import annotations

import re
from typing import Any

from bson import ObjectId
from fastapi import APIRouter, Depends, Request
from pydantic import Field
from pymongo.errors import DuplicateKeyError

from app.core.config import get_settings
from app.core.dependencies import AuthUser, require_superadmin
from app.core.exceptions import AppError, BadRequest, Conflict, NotFound
from app.database.connection import get_database
from app.modules.auth.schemas import StrictModel
from app.modules.catalog import BACH_COURSES, COURSES, SPECIALTIES, paralelos_for
from app.modules.curriculum import TEST_SUBJECT, specialty_of, subjects_for
from app.modules.superadmin.common import audit, page_args, paged
from app.modules.teachers.importer import EMAIL_RE, build_preview
from app.modules.users import repository as users
from app.shared.middleware.memory_multipart import read_files
from app.shared.serialization import to_json
from app.shared.utilities.dates import utcnow
from app.shared.validators.object_id import parse_object_id

router = APIRouter(prefix="/teachers")
MAX_FILE = 8 * 1024 * 1024
XLSX = re.compile(r"\.xlsx$", re.IGNORECASE)


def db():
    return get_database()


def _email(v: str | None) -> str | None:
    if v is None or not str(v).strip():
        return None
    e = str(v).strip().lower()
    d = get_settings().institutional_email_domain
    if not EMAIL_RE.match(e) or not e.endswith("@" + d):
        raise BadRequest(f"El correo del docente debe ser institucional (@{d}).")
    if get_settings().is_student_email(e):
        raise BadRequest("Ese es un correo de estudiante (.est).")
    return e


# ── importación ──────────────────────────────────────────────────────────────
@router.post("/import/preview", status_code=201)
async def preview(request: Request, actor: AuthUser = Depends(require_superadmin)):
    files = await read_files(request, ("distributivo", "correos"), MAX_FILE, XLSX, "Solo se aceptan archivos .xlsx.")
    if not files or "distributivo" not in files or "correos" not in files:
        raise BadRequest("Sube los dos archivos: el distributivo y la lista de correos.")
    data = build_preview(files["distributivo"].content, files["correos"].content, get_settings().institutional_email_domain)
    existing = {t["nameKey"]: t async for t in db()["teachers"].find({}, {"nameKey": 1, "email": 1})}
    for t in data["teachers"]:
        prev = existing.get(t["nameKey"])
        # Un correo puesto a mano antes se conserva si el archivo no trae uno.
        if prev and prev.get("email") and not t["email"]:
            t["email"], t["match"] = prev["email"], "anterior"
    res = await db()["teacher_imports"].insert_one({"status": "borrador", "createdBy": actor.user_id, "createdAt": utcnow(),
                                                    "summary": data["summary"], "teachers": data["teachers"]})
    return to_json({"draftId": str(res.inserted_id), "summary": data["summary"],
                    "teachers": [{k: t[k] for k in ("nameKey", "name", "email", "match", "candidates")} | {"materias": len(t["assignments"])}
                                 for t in data["teachers"]]})


class ConfirmIn(StrictModel):
    draftId: str = Field(min_length=24, max_length=24)
    emails: dict[str, str | None] = Field(default_factory=dict)  # nameKey → correo elegido a mano
    exclude: list[str] = Field(default_factory=list, max_length=500)  # nameKey de docentes que no se importan


async def _link_user(teacher: dict[str, Any]) -> str:
    """Crea o vincula la cuenta de Google del docente. Devuelve qué pasó."""
    email = teacher.get("email")
    if not email:
        return "sin_correo"
    u = await users.find_by_login(email)
    if u is None:
        try:
            u = await users.insert({"role": "TEACHER", "email": email, "username": None, "name": teacher["name"],
                                    "passwordHash": None, "teacherId": teacher["_id"]})
        except DuplicateKeyError:
            u = await users.find_by_login(email)
        result = "creada"
    elif u["role"] == "STUDENT":
        return "conflicto_estudiante"
    else:
        result = "vinculada"
        fields: dict[str, Any] = {"teacherId": teacher["_id"]}
        if u["role"] in ("ADMIN", "SUPERADMIN") and "TEACHER" not in (u.get("extraRoles") or []):
            # Personal administrativo que también dicta clases: suma el rol de docente (multirol).
            fields["extraRoles"] = [*(u.get("extraRoles") or []), "TEACHER"]
        await users.set_fields(u["_id"], fields)
    await db()["teachers"].update_one({"_id": teacher["_id"]}, {"$set": {"userId": u["_id"]}})
    await db()["access_requests"].update_many({"email": email, "status": "pending"},
                                              {"$set": {"status": "granted", "grantedRole": "TEACHER", "resolvedAt": utcnow()}})
    return result


@router.post("/import/confirm")
async def confirm(body: ConfirmIn, request: Request, actor: AuthUser = Depends(require_superadmin)):
    draft = await db()["teacher_imports"].find_one({"_id": parse_object_id(body.draftId), "status": "borrador"})
    if not draft:
        raise NotFound("La vista previa ya no está disponible. Vuelve a subir los archivos.")
    overrides = {k: _email(v) for k, v in body.emails.items()}
    now = utcnow()
    seen: list[ObjectId] = []
    accounts: dict[str, int] = {}
    asg_docs = []
    excluded = set(body.exclude)
    for t in draft["teachers"]:
        if t["nameKey"] in excluded:
            continue  # p. ej. docentes que ya no trabajan en la institución: sus materias quedan sin docente
        email = overrides.get(t["nameKey"], t["email"]) if t["nameKey"] in overrides else t["email"]
        doc = await db()["teachers"].find_one_and_update(
            {"nameKey": t["nameKey"]},
            {"$set": {"name": t["name"], "email": email, "active": True, "importId": draft["_id"], "updatedAt": now},
             "$setOnInsert": {"nameKey": t["nameKey"], "createdAt": now, "userId": None}},
            upsert=True, return_document=True)
        seen.append(doc["_id"])
        r = await _link_user(doc)
        accounts[r] = accounts.get(r, 0) + 1
        for course, spec, par, subj in t["assignments"]:
            asg_docs.append({"teacherId": doc["_id"], "course": course, "specialty": spec or None, "paralelo": par,
                             "subject": subj, "active": True, "importId": draft["_id"], "createdAt": now})
    # Las asignaciones anteriores quedan inactivas (no se borran) y entran las nuevas.
    # Lo agregado a mano por el SUPERADMIN (materias y docentes manuales) no lo toca la importación.
    await db()["teacher_assignments"].update_many({"active": True, "manual": {"$ne": True}},
                                                  {"$set": {"active": False, "replacedAt": now}})
    if asg_docs:
        await db()["teacher_assignments"].insert_many(asg_docs)
    gone = await db()["teachers"].update_many({"_id": {"$nin": seen}, "active": True, "manual": {"$ne": True}},
                                              {"$set": {"active": False, "updatedAt": now}})
    summary = {**draft["summary"], "cuentas": accounts, "inactivados": gone.modified_count, "excluidos": len(excluded),
               "importados": len(seen)}
    await db()["teacher_imports"].update_one({"_id": draft["_id"]}, {"$set": {"status": "aplicada", "appliedAt": now,
                                                                             "appliedBy": actor.user_id, "summary": summary},
                                                                    "$unset": {"teachers": ""}})
    await audit(actor, request, "TEACHERS_IMPORTED", metadata={k: v for k, v in summary.items() if k != "cuentas"} | {"cuentas": accounts})
    return to_json(summary)


# ── lista y correo ───────────────────────────────────────────────────────────
@router.get("")
async def list_teachers(q: str | None = None, status: str = "activos", page: int = 1, pageSize: int = 25,
                        _: AuthUser = Depends(require_superadmin)):
    query: dict[str, Any] = {"active": True}
    if status == "sin_correo":
        query["email"] = None
    elif status == "sin_ingreso":
        query["email"] = {"$ne": None}
    elif status == "inactivos":
        query = {"active": False}
    if q and q.strip():
        rx = {"$regex": re.escape(q.strip()), "$options": "i"}
        query["$or"] = [{"name": rx}, {"email": rx}]
    p, ps = page_args(page, pageSize)
    total = await db()["teachers"].count_documents(query)
    rows = await db()["teachers"].find(query).sort("name", 1).skip((p - 1) * ps).limit(ps).to_list(ps)
    ids = [t["_id"] for t in rows]
    counts = {c["_id"]: c["n"] async for c in db()["teacher_assignments"].aggregate(
        [{"$match": {"teacherId": {"$in": ids}, "active": True}}, {"$group": {"_id": "$teacherId", "n": {"$sum": 1}}}])}
    logins = {u["_id"]: u.get("lastLoginAt") async for u in db()["users"].find(
        {"_id": {"$in": [t.get("userId") for t in rows if t.get("userId")]}}, {"lastLoginAt": 1})}
    out = [{"id": str(t["_id"]), "name": t.get("name"), "email": t.get("email"), "active": t.get("active", True),
            "materias": counts.get(t["_id"], 0), "hasAccount": bool(t.get("userId")), "manual": bool(t.get("manual")),
            "lastLoginAt": logins.get(t.get("userId"))} for t in rows]
    totals = {"activos": await db()["teachers"].count_documents({"active": True}),
              "sinCorreo": await db()["teachers"].count_documents({"active": True, "email": None})}
    last = await db()["teacher_imports"].find_one({"status": "aplicada"}, sort=[("appliedAt", -1)])
    return to_json({**paged(out, total, p, ps), "totals": totals,
                    "lastImport": {"at": last["appliedAt"], "summary": last.get("summary")} if last else None})


@router.get("/catalog")
async def catalog(_: AuthUser = Depends(require_superadmin)):
    """Cursos, paralelos y materias válidas, para asignar materias a mano."""
    out = []
    for c in COURSES:
        pars = [p for codes in SPECIALTIES.values() for p in codes] if c in BACH_COURSES else list(paralelos_for(c))
        out.append({"course": c, "paralelos": [{"code": p, "specialty": specialty_of(c, p), "subjects": [*subjects_for(c, p), TEST_SUBJECT]}
                                               for p in pars]})
    return out


async def _teacher(id_: str) -> dict[str, Any]:
    t = await db()["teachers"].find_one({"_id": parse_object_id(id_)})
    if not t:
        raise NotFound("Docente no encontrado")
    return t


async def _detail(t: dict[str, Any]) -> dict[str, Any]:
    asg = await db()["teacher_assignments"].find({"teacherId": t["_id"], "active": True}).to_list(None)
    u = await users.get(t["userId"], {"role": 1, "extraRoles": 1, "isActive": 1, "lastLoginAt": 1}) if t.get("userId") else None
    order = {c: i for i, c in enumerate(COURSES)}
    return to_json({"id": str(t["_id"]), "name": t.get("name"), "email": t.get("email"), "active": t.get("active", True),
                    "hasAccount": bool(t.get("userId")), "manual": bool(t.get("manual")),
                    "account": {"id": str(u["_id"]), "role": u["role"], "roles": users.effective_roles(u), "isActive": u.get("isActive", True),
                                "lastLoginAt": u.get("lastLoginAt")} if u else None,
                    "assignments": sorted([{"course": a["course"], "paralelo": a["paralelo"], "subject": a["subject"],
                                            "specialty": a.get("specialty"), "manual": bool(a.get("manual"))} for a in asg],
                                          key=lambda a: (order.get(a["course"], 99), a["paralelo"], a["subject"]))})


@router.get("/{id_}")
async def teacher_detail(id_: str, _: AuthUser = Depends(require_superadmin)):
    return await _detail(await _teacher(id_))


class AssignIn(StrictModel):
    course: str = Field(min_length=2, max_length=20)
    paralelo: str = Field(min_length=1, max_length=4)
    subject: str = Field(min_length=2, max_length=120)


def _valid_assignment(body: AssignIn) -> str | None:
    if body.course not in COURSES:
        raise BadRequest("Curso no válido")
    pars = [p for codes in SPECIALTIES.values() for p in codes] if body.course in BACH_COURSES else paralelos_for(body.course)
    if body.paralelo not in pars:
        raise BadRequest("Paralelo no válido para ese curso")
    if body.subject not in (*subjects_for(body.course, body.paralelo), TEST_SUBJECT):
        raise BadRequest("Esa materia no corresponde a ese curso y paralelo")
    return specialty_of(body.course, body.paralelo)


@router.post("/{id_}/assignments")
async def add_assignment(id_: str, body: AssignIn, request: Request, actor: AuthUser = Depends(require_superadmin)):
    """Agrega una materia a mano (correcciones o pruebas). Una nueva importación no la quita."""
    t = await _teacher(id_)
    spec = _valid_assignment(body)
    key = {"teacherId": t["_id"], "course": body.course, "paralelo": body.paralelo, "subject": body.subject, "active": True}
    if await db()["teacher_assignments"].find_one(key, {"_id": 1}):
        raise Conflict("El docente ya tiene esa materia en ese curso y paralelo.")
    await db()["teacher_assignments"].insert_one({**key, "specialty": spec, "manual": True, "createdAt": utcnow(),
                                                  "createdBy": actor.user_id})
    await audit(actor, request, "TEACHER_ASSIGNMENT_ADDED", metadata={"teacherId": id_, **body.model_dump()})
    return await _detail(t)


@router.post("/{id_}/assignments/remove")
async def remove_assignment(id_: str, body: AssignIn, request: Request, actor: AuthUser = Depends(require_superadmin)):
    """Quita una materia: la asignación queda inactiva (no se borra)."""
    t = await _teacher(id_)
    res = await db()["teacher_assignments"].update_many(
        {"teacherId": t["_id"], "course": body.course, "paralelo": body.paralelo, "subject": body.subject, "active": True},
        {"$set": {"active": False, "removedAt": utcnow(), "removedBy": actor.user_id}})
    if not res.modified_count:
        raise NotFound("El docente no tiene esa materia.")
    await audit(actor, request, "TEACHER_ASSIGNMENT_REMOVED", metadata={"teacherId": id_, **body.model_dump()})
    return await _detail(t)


class EmailIn(StrictModel):
    email: str = Field(min_length=5, max_length=254)


@router.patch("/{id_}/email")
async def set_email(id_: str, body: EmailIn, request: Request, actor: AuthUser = Depends(require_superadmin)):
    t = await db()["teachers"].find_one({"_id": parse_object_id(id_)})
    if not t:
        raise NotFound("Docente no encontrado")
    email = _email(body.email)
    if await db()["teachers"].find_one({"email": email, "_id": {"$ne": t["_id"]}}, {"_id": 1}):
        raise Conflict("Ese correo ya pertenece a otro docente.")
    if t.get("userId") and t.get("email") != email:
        u = await users.get(t["userId"])
        if u and u.get("role") == "TEACHER":
            try:
                await users.set_fields(u["_id"], {"email": email})
            except DuplicateKeyError as exc:
                raise Conflict("Ya existe otra cuenta con ese correo.") from exc
            await users.col().update_one({"_id": u["_id"]}, {"$inc": {"sessionVersion": 1}})
    await db()["teachers"].update_one({"_id": t["_id"]}, {"$set": {"email": email, "updatedAt": utcnow()}})
    t = await db()["teachers"].find_one({"_id": t["_id"]})
    result = await _link_user(t)
    if result == "conflicto_estudiante":
        raise AppError(409, "Ese correo pertenece a una cuenta de estudiante.", code="EMAIL_IS_STUDENT")
    await audit(actor, request, "TEACHER_EMAIL_SET", metadata={"teacherId": id_, "email": email})
    return {"ok": True, "account": result}
