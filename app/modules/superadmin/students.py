"""Fichas de estudiantes y representantes, vínculos e inconsistencias (SUPERADMIN).

Los datos personales están cifrados: el listado los descifra en el servidor solo para el SUPERADMIN
y la búsqueda por nombre se hace después de descifrar (volumen escolar). Cada consulta de ficha
queda auditada.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import Field

from app.core.dependencies import AuthUser, require_superadmin
from app.core.exceptions import BadRequest, NotFound
from app.database.connection import get_database
from app.modules.auth.schemas import StrictModel
from app.modules.requests.state_machine import ACTIVE
from app.modules.students import service as st
from app.modules.superadmin.common import audit, page_args, paged
from app.modules.system.service import enforce_rate_limit
from app.shared.crypto.field_encryption import hash_field
from app.shared.serialization import to_json
from app.shared.utilities.dates import utcnow
from app.shared.validators.object_id import parse_object_id

router = APIRouter()


class LinkIn(StrictModel):
    studentUserId: str = Field(min_length=24, max_length=24)
    representativeId: str | None = Field(None, min_length=24, max_length=24)
    cedula: str | None = Field(None, min_length=10, max_length=10)
    names: str | None = Field(None, min_length=5, max_length=120)
    phone: str | None = Field(None, pattern=r"^0\d{8,9}$")
    relationship: Literal[st.RELATIONSHIPS]  # type: ignore[valid-type]
    isPrimary: bool = False


class ReasonIn(StrictModel):
    reason: str = Field(min_length=10, max_length=500)


class UnlockIn(StrictModel):
    reason: str = Field(min_length=10, max_length=500)
    scope: Literal["all", "student", "representative"] = "all"


class RepEditIn(StrictModel):
    names: str | None = Field(None, min_length=5, max_length=120)
    phone: str | None = Field(None, pattern=r"^0\d{8,9}$")
    email: str | None = Field(None, max_length=254)
    isActive: bool | None = None


def _fold(s: str | None) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", (s or "").lower()) if unicodedata.category(c) != "Mn")


async def _sensitive(actor: AuthUser, request: Request, what: str, target_id: str | None = None) -> None:
    await enforce_rate_limit("SENSITIVE_VIEW", actor.user_id, "Demasiadas consultas de datos personales. Espera un momento.")
    await audit(actor, request, "SENSITIVE_DATA_VIEWED", metadata={"view": what, "id": target_id})


# ── estudiantes ───────────────────────────────────────────────────────────────
@router.get("/students")
async def list_students(request: Request, q: str | None = None, course: str | None = None, paralelo: str | None = None,
                        padron: str | None = None, profile: str | None = None, status: str | None = None,
                        page: int = 1, pageSize: int | None = None, actor: AuthUser = Depends(require_superadmin)):
    p, size = page_args(page, pageSize)
    db = get_database()
    uq: dict[str, Any] = {"role": "STUDENT"}
    if status in ("active", "inactive"):
        uq["isActive"] = status == "active"
    us = await db["users"].find(uq, {"passwordHash": 0}).sort("createdAt", -1).to_list(None)
    profs = {pr["userId"]: pr async for pr in db["student_profiles"].find({"userId": {"$in": [u["_id"] for u in us]}})}
    nreps: dict[Any, int] = {}
    async for row in db["student_representatives"].aggregate([{"$match": {"active": True}},
                                                              {"$group": {"_id": "$studentUserId", "n": {"$sum": 1}}}]):
        nreps[row["_id"]] = row["n"]
    nreq: dict[Any, int] = {}
    async for row in db["requests"].aggregate([{"$match": {"studentUserId": {"$ne": None}}},
                                               {"$group": {"_id": "$studentUserId", "n": {"$sum": 1}}}]):
        nreq[row["_id"]] = row["n"]
    needle = _fold(q.strip()) if q and q.strip() else None
    needle_hash = hash_field(q.strip()) if q and re.fullmatch(r"\d{10}", q.strip()) else None
    rows = []
    for u in us:
        pr = profs.get(u["_id"])
        if course and (not pr or pr.get("course") != course):
            continue
        if paralelo and (not pr or pr.get("paralelo") != paralelo):
            continue
        if padron in ("yes", "no") and bool(pr and pr.get("padronMatch")) != (padron == "yes"):
            continue
        if profile == "complete" and not (pr and pr.get("completed")):
            continue
        if profile == "incomplete" and pr and pr.get("completed"):
            continue
        name = st.dec(pr.get("namesEnc")) if pr else None
        if needle_hash and not (pr and pr.get("cedulaHash") == needle_hash):
            continue
        if needle and not needle_hash and needle not in _fold(name) and needle not in _fold(u.get("email")):
            continue
        rows.append({"id": str(u["_id"]), "email": u.get("email"), "name": name, "course": pr.get("course") if pr else None,
                     "paralelo": pr.get("paralelo") if pr else None, "padronMatch": bool(pr and pr.get("padronMatch")),
                     "profileCompleted": bool(pr and pr.get("completed")), "isActive": u.get("isActive", True),
                     "representatives": nreps.get(u["_id"], 0), "requests": nreq.get(u["_id"], 0),
                     "lastLoginAt": u.get("lastLoginAt"), "createdAt": u.get("createdAt")})
    await _sensitive(actor, request, "students.list")
    return to_json(paged(rows[(p - 1) * size: p * size], len(rows), p, size))


@router.get("/students/{id_}")
async def student_detail(id_: str, request: Request, actor: AuthUser = Depends(require_superadmin)):
    oid = parse_object_id(id_)
    db = get_database()
    u = await db["users"].find_one({"_id": oid, "role": "STUDENT"}, {"passwordHash": 0})
    if not u:
        raise NotFound("Estudiante no encontrado")
    reqs = await db["requests"].find({"studentUserId": oid}, {"pdfAccessToken": 0}).sort("createdAt", -1).to_list(100)
    by_status: dict[str, int] = {}
    for r in reqs:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    await _sensitive(actor, request, "students.detail", id_)
    prof = await st.get_profile(oid)
    reps = await st.links_of(oid)
    return to_json({"user": {"id": id_, "email": u.get("email"), "isActive": u.get("isActive", True),
                             "lastLoginAt": u.get("lastLoginAt"), "createdAt": u.get("createdAt"),
                             "forcePasswordChange": bool(u.get("forcePasswordChange"))},
                    "profile": st.profile_out(prof), "representatives": reps,
                    "locks": {"student": st.student_data_locked(prof), "representative": st.rep_data_locked(prof, any(r["isPrimary"] for r in reps)),
                              "unlockedAt": prof and prof.get("unlockedAt"), "unlockReason": prof and prof.get("unlockReason")},
                    "requestsByStatus": by_status,
                    "requests": [{k: r.get(k) for k in ("_id", "requestNumber", "requestType", "status", "createdAt", "dueAt")}
                                 for r in reqs[:20]]})


@router.post("/students/{id_}/unlock")
async def unlock_profile(id_: str, body: UnlockIn, request: Request, actor: AuthUser = Depends(require_superadmin)):
    """Habilita que el estudiante corrija sus datos (solicitud presencial al Departamento de Sistemas).
    Al guardarlos de nuevo, el sistema los vuelve a bloquear."""
    oid = parse_object_id(id_)
    prof = await st.get_profile(oid)
    if not prof:
        raise BadRequest("El estudiante todavía no registró sus datos: no hay nada que desbloquear")
    fields: dict[str, Any] = {"unlockedAt": utcnow(), "unlockedBy": actor.user_id, "unlockReason": body.reason.strip()}
    if body.scope in ("all", "student"):
        fields["locked"] = False
    if body.scope in ("all", "representative"):
        fields["repLocked"] = False
    await get_database()["student_profiles"].update_one({"_id": prof["_id"]}, {"$set": fields})
    await audit(actor, request, "PROFILE_UNLOCKED", metadata={"studentUserId": id_, "scope": body.scope, "reason": body.reason.strip()})
    return await student_detail(id_, request, actor)


@router.post("/students/{id_}/lock")
async def lock_profile(id_: str, request: Request, actor: AuthUser = Depends(require_superadmin)):
    """Vuelve a bloquear la edición sin esperar a que el estudiante guarde."""
    oid = parse_object_id(id_)
    res = await get_database()["student_profiles"].update_one({"userId": oid}, {"$set": {"locked": True, "repLocked": True}})
    if res.matched_count == 0:
        raise NotFound("Estudiante sin datos registrados")
    await audit(actor, request, "PROFILE_LOCKED", metadata={"studentUserId": id_})
    return await student_detail(id_, request, actor)


# ── representantes ────────────────────────────────────────────────────────────
@router.get("/representatives")
async def list_reps(request: Request, q: str | None = None, linked: str | None = None, page: int = 1,
                    pageSize: int | None = None, actor: AuthUser = Depends(require_superadmin)):
    p, size = page_args(page, pageSize)
    db = get_database()
    counts: dict[Any, int] = {}
    async for row in db["student_representatives"].aggregate([{"$match": {"active": True}},
                                                              {"$group": {"_id": "$representativeId", "n": {"$sum": 1}}}]):
        counts[row["_id"]] = row["n"]
    needle = _fold(q.strip()) if q and q.strip() else None
    needle_hash = hash_field(q.strip()) if q and re.fullmatch(r"\d{10}", q.strip()) else None
    rows = []
    async for r in db["representatives"].find({}).sort("createdAt", -1):
        n = counts.get(r["_id"], 0)
        if linked == "none" and n:
            continue
        if linked == "multiple" and n < 2:
            continue
        name = st.dec(r.get("namesEnc"))
        if needle_hash and r.get("cedulaHash") != needle_hash:
            continue
        if needle and not needle_hash and needle not in _fold(name):
            continue
        rows.append({"id": str(r["_id"]), "name": name, "cedula": st.dec(r.get("cedulaEnc")), "phone": st.dec(r.get("phoneEnc")),
                     "isActive": r.get("isActive", True), "students": n, "createdAt": r.get("createdAt")})
    await _sensitive(actor, request, "representatives.list")
    return to_json(paged(rows[(p - 1) * size: p * size], len(rows), p, size))


@router.get("/representatives/{id_}")
async def rep_detail(id_: str, request: Request, actor: AuthUser = Depends(require_superadmin)):
    oid = parse_object_id(id_)
    db = get_database()
    r = await db["representatives"].find_one({"_id": oid})
    if not r:
        raise NotFound("Representante no encontrado")
    links = await db["student_representatives"].find({"representativeId": oid}).sort("linkedAt", -1).to_list(50)
    profs = {pr["userId"]: pr async for pr in db["student_profiles"].find({"userId": {"$in": [l["studentUserId"] for l in links]}})}
    users = {u["_id"]: u async for u in db["users"].find({"_id": {"$in": [l["studentUserId"] for l in links]}}, {"email": 1, "isActive": 1})}
    await _sensitive(actor, request, "representatives.detail", id_)
    return to_json({**st.rep_out(r), "links": [{
        "linkId": str(l["_id"]), "studentUserId": str(l["studentUserId"]), "active": l.get("active", True),
        "studentName": st.dec(profs.get(l["studentUserId"], {}).get("namesEnc")),
        "studentEmail": users.get(l["studentUserId"], {}).get("email"),
        "studentActive": users.get(l["studentUserId"], {}).get("isActive", True),
        "relationship": l.get("relationship"), "isPrimary": bool(l.get("isPrimary")), "linkedAt": l.get("linkedAt"),
        "unlinkedAt": l.get("unlinkedAt")} for l in links]})


@router.patch("/representatives/{id_}")
async def rep_edit(id_: str, body: RepEditIn, request: Request, actor: AuthUser = Depends(require_superadmin)):
    oid = parse_object_id(id_)
    r = await get_database()["representatives"].find_one({"_id": oid})
    if not r:
        raise NotFound("Representante no encontrado")
    data = body.model_dump(exclude_none=True)
    if "names" in data:
        st.assert_person_name(data["names"], "El nombre")
    await st.update_rep_data(oid, {k: v for k, v in data.items() if k != "isActive"}, only_if_single_owner=None)
    if "isActive" in data:
        await get_database()["representatives"].update_one({"_id": oid}, {"$set": {"isActive": data["isActive"], "updatedAt": utcnow()}})
    await audit(actor, request, "USER_UPDATED", metadata={"representativeId": id_, "fields": sorted(data)})
    return to_json(st.rep_out(await get_database()["representatives"].find_one({"_id": oid})))


# ── vínculos ──────────────────────────────────────────────────────────────────
@router.post("/links", status_code=201)
async def create_link(body: LinkIn, request: Request, actor: AuthUser = Depends(require_superadmin)):
    sid = parse_object_id(body.studentUserId)
    db = get_database()
    if not await db["users"].find_one({"_id": sid, "role": "STUDENT"}, {"_id": 1}):
        raise NotFound("Estudiante no encontrado")
    if body.representativeId:
        rep = await db["representatives"].find_one({"_id": parse_object_id(body.representativeId)})
        if not rep:
            raise NotFound("Representante no encontrado")
    else:
        if not (body.cedula and body.names and body.phone):
            raise BadRequest("Indica un representante existente o sus datos (nombres, cédula y teléfono)")
        st.assert_cedula(body.cedula, "La cédula del representante")
        st.assert_person_name(body.names, "El nombre del representante")
        rep, _ = await st.upsert_representative({"names": body.names, "cedula": body.cedula, "phone": body.phone}, None)
    ln = await st.link(sid, rep["_id"], body.relationship, body.isPrimary, actor.user_id)
    await audit(actor, request, "LINK_CREATED", metadata={"linkId": str(ln["_id"]), "studentUserId": body.studentUserId})
    return to_json({"representatives": await st.links_of(sid)})


@router.post("/links/{id_}/primary")
async def link_primary(id_: str, request: Request, actor: AuthUser = Depends(require_superadmin)):
    ln = await st.set_primary(parse_object_id(id_))
    await audit(actor, request, "LINK_PRIMARY_SET", metadata={"linkId": id_})
    return to_json({"representatives": await st.links_of(ln["studentUserId"])})


@router.post("/links/{id_}/unlink")
async def link_remove(id_: str, body: ReasonIn, request: Request, actor: AuthUser = Depends(require_superadmin)):
    ln = await st.unlink(parse_object_id(id_), actor.user_id)
    await audit(actor, request, "LINK_REMOVED", metadata={"linkId": id_, "reason": body.reason})
    return to_json({"representatives": await st.links_of(ln["studentUserId"])})


# ── inconsistencias ───────────────────────────────────────────────────────────
@router.get("/inconsistencies")
async def inconsistencies(_: AuthUser = Depends(require_superadmin)):
    db = get_database()
    students = await db["users"].find({"role": "STUDENT"}, {"email": 1, "isActive": 1, "createdAt": 1}).to_list(None)
    profs = {p["userId"]: p async for p in db["student_profiles"].find({})}
    active_links = await db["student_representatives"].find({"active": True}).to_list(None)
    by_student: dict[Any, list] = {}
    for l in active_links:
        by_student.setdefault(l["studentUserId"], []).append(l)
    reps = {r["_id"]: r async for r in db["representatives"].find({})}
    users_by_id = {u["_id"]: u for u in students}

    def srow(u: dict, note: str) -> dict:
        p = profs.get(u["_id"])
        return {"id": str(u["_id"]), "email": u.get("email"), "name": st.dec(p.get("namesEnc")) if p else None, "note": note}

    no_profile = [srow(u, "Registrado sin datos personales") for u in students if u["_id"] not in profs]
    no_rep = [srow(u, "Sin representante vinculado") for u in students if u["_id"] in profs and not by_student.get(u["_id"])]
    no_primary = [srow(u, "Sin representante principal") for u in students
                  if by_student.get(u["_id"]) and not any(l.get("isPrimary") for l in by_student[u["_id"]])]
    multi_primary = [srow(u, "Más de un representante principal") for u in students
                     if sum(1 for l in by_student.get(u["_id"], []) if l.get("isPrimary")) > 1]
    not_in_padron = [srow(u, "La cédula no está en el padrón") for u in students
                     if u["_id"] in profs and not profs[u["_id"]].get("padronMatch")]
    linked_rep_ids = {l["representativeId"] for l in active_links}
    orphan_reps = [{"id": str(r["_id"]), "name": st.dec(r.get("namesEnc")), "note": "Representante sin estudiantes"}
                   for rid, r in reps.items() if rid not in linked_rep_ids]
    inactive_links = [{"linkId": str(l["_id"]), "studentUserId": str(l["studentUserId"]),
                       "name": st.dec(profs.get(l["studentUserId"], {}).get("namesEnc")),
                       "note": "Vínculo activo con estudiante desactivado" if not users_by_id.get(l["studentUserId"], {}).get("isActive", True)
                       else "Vínculo activo con representante inactivo"}
                      for l in active_links
                      if not users_by_id.get(l["studentUserId"], {}).get("isActive", True)
                      or not reps.get(l["representativeId"], {}).get("isActive", True)]
    legacy = await db["requests"].count_documents({"studentUserId": {"$exists": False}})
    overdue = await db["requests"].count_documents({"status": {"$in": list(ACTIVE)}, "dueAt": {"$lt": utcnow()}})
    groups = [
        {"key": "no_profile", "label": "Estudiantes sin datos personales", "rows": no_profile},
        {"key": "no_rep", "label": "Estudiantes sin representante", "rows": no_rep},
        {"key": "no_primary", "label": "Estudiantes sin representante principal", "rows": no_primary},
        {"key": "multi_primary", "label": "Estudiantes con más de un principal", "rows": multi_primary},
        {"key": "not_in_padron", "label": "Cédula no encontrada en el padrón", "rows": not_in_padron},
        {"key": "orphan_reps", "label": "Representantes sin estudiantes", "rows": orphan_reps},
        {"key": "inactive_links", "label": "Vínculos con cuentas o representantes inactivos", "rows": inactive_links},
    ]
    return to_json({"groups": groups, "legacyRequests": legacy, "overdueRequests": overdue,
                    "total": sum(len(g["rows"]) for g in groups)})
