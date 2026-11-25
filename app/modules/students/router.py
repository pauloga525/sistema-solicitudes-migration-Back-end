"""Área del estudiante (`/me/*`). Cada consulta filtra por el usuario de la sesión (sin IDOR)."""
from __future__ import annotations

import re
from typing import Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from app.core.dependencies import AuthUser, client_ip, require_student
from app.core.exceptions import AppError, BadRequest, NotFound
from app.core.rate_limit import Throttle
from app.database.connection import get_database
from app.modules.audit.service import log_user_event
from app.modules.auth.schemas import StrictModel
from app.core.config import get_settings
from app.modules.catalog import BACH_COURSES, SPECIALTIES, CourseLiteral, ParaleloLiteral, RequestTypeLiteral, paralelos_for
from app.modules.requests import workflow
from app.modules.requests.router import pdf_service, service
from app.modules.requests.schemas import CreateRequestDTO, create_request_schema
from app.modules.requests.service import RequestsService
from app.modules.requests.state_machine import ACTIVE, CANCELADO
from app.modules.students import service as st
from app.shared.serialization import to_json
from app.shared.validators.class_validator import validate_dto
from app.shared.validators.object_id import parse_object_id

router = APIRouter(prefix="/me", tags=["estudiante"])
PHONE = r"^0\d{8,9}$"


class ProfileIn(StrictModel):
    firstNames: str = Field(min_length=2, max_length=60)
    lastNames: str = Field(min_length=2, max_length=60)
    cedula: str = Field(min_length=10, max_length=10)
    course: CourseLiteral
    paralelo: ParaleloLiteral
    specialty: Literal[tuple(SPECIALTIES)] | None = None  # type: ignore[valid-type]


class RepresentativeIn(StrictModel):
    firstNames: str = Field(min_length=2, max_length=60)
    lastNames: str = Field(min_length=2, max_length=60)
    cedula: str = Field(min_length=10, max_length=10)
    email: str = Field(min_length=5, max_length=254)
    phone: str = Field(pattern=PHONE)
    relationship: Literal[st.RELATIONSHIPS] = "Representante legal"  # type: ignore[valid-type]
    isPrimary: bool = False


class RepresentativeUpdate(StrictModel):
    firstNames: str | None = Field(None, min_length=2, max_length=60)
    lastNames: str | None = Field(None, min_length=2, max_length=60)
    email: str | None = Field(None, min_length=5, max_length=254)
    phone: str | None = Field(None, pattern=PHONE)
    relationship: Literal[st.RELATIONSHIPS] | None = None  # type: ignore[valid-type]
    isPrimary: bool | None = None


class MyRequestIn(StrictModel):
    requestType: RequestTypeLiteral
    representativeLinkId: str = Field(min_length=24, max_length=24)
    payload: dict


def _norm_name(v: str) -> str:
    return re.sub(r"\s+", " ", v).strip()


def _rep_email(v: str) -> str:
    """Correo personal del representante (los padres no tienen correo institucional).
    Con REPRESENTATIVE_EMAIL_INSTITUTIONAL=true se vuelve a exigir el dominio institucional."""
    import os
    e = v.strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", e):
        raise BadRequest("Correo del representante inválido")
    d = get_settings().institutional_email_domain
    if os.environ.get("REPRESENTATIVE_EMAIL_INSTITUTIONAL", "false").lower() == "true" and not e.endswith("@" + d):
        raise BadRequest(f"El correo del representante debe ser institucional (@{d})")
    return e


async def _audit(user: AuthUser, request: Request, event: str, metadata: dict | None = None) -> None:
    await log_user_event(event, actor_id=user.user_id, actor_email=user.email, actor_role=user.role,
                         actor_ip=client_ip(request), target_id=user.user_id, metadata=metadata)


# ── perfil ────────────────────────────────────────────────────────────────────
@router.get("/profile")
async def get_profile(user: AuthUser = Depends(require_student)):
    prof = await st.get_profile(user.oid)
    reps = await st.links_of(user.oid)
    return to_json({"email": user.email, "profile": st.profile_out(prof), "representatives": reps,
                    "relationships": list(st.RELATIONSHIPS), "locks": await _locks(user)})


async def _locks(user: AuthUser) -> dict:
    prof = await st.get_profile(user.oid)
    has_primary = bool(await get_database()["student_representatives"].find_one(
        {"studentUserId": user.oid, "active": True, "isPrimary": True}, {"_id": 1}))
    return {"student": st.student_data_locked(prof), "representative": st.rep_data_locked(prof, has_primary)}


async def _assert_rep_unlocked(user: AuthUser) -> None:
    if (await _locks(user))["representative"]:
        raise AppError(403, st.LOCKED_MSG, code="PROFILE_LOCKED")


async def _relock_rep(user: AuthUser) -> None:
    await get_database()["student_profiles"].update_one({"userId": user.oid}, {"$set": {"repLocked": True}})


@router.put("/profile")
async def put_profile(body: ProfileIn, request: Request, user: AuthUser = Depends(require_student)):
    data = body.model_dump()
    data["names"] = st.full_name(data["firstNames"], data["lastNames"], "del estudiante")
    data["firstNames"], data["lastNames"] = (_norm_name(data["firstNames"]), _norm_name(data["lastNames"]))
    st.assert_cedula(data["cedula"])
    if data["course"] in BACH_COURSES:
        if not data.get("specialty"):
            raise BadRequest("Elige tu especialidad")
    else:
        data["specialty"] = None
    if data["paralelo"] not in paralelos_for(data["course"], data.get("specialty")):
        raise BadRequest("El paralelo no corresponde al curso" + (" y la especialidad" if data.get("specialty") else "") + " elegidos")
    out = await st.save_profile(user.oid, data)
    await _audit(user, request, "PROFILE_UPDATED")
    return to_json({"profile": out, "representatives": await st.links_of(user.oid), "locks": await _locks(user)})


# ── representantes ────────────────────────────────────────────────────────────
async def _my_link(user: AuthUser, link_id: str) -> dict:
    oid = parse_object_id(link_id)
    ln = await get_database()["student_representatives"].find_one({"_id": oid, "studentUserId": user.oid, "active": True})
    if not ln:
        raise NotFound("Representante no encontrado")
    return ln


@router.post("/representatives", status_code=201)
async def add_rep(body: RepresentativeIn, request: Request, user: AuthUser = Depends(require_student)):
    if not await st.get_profile(user.oid):
        raise BadRequest("Primero completa tus datos personales")
    await _assert_rep_unlocked(user)
    data = body.model_dump()
    data["names"] = st.full_name(data["firstNames"], data["lastNames"], "del representante")
    data["firstNames"], data["lastNames"] = (_norm_name(data["firstNames"]), _norm_name(data["lastNames"]))
    st.assert_cedula(data["cedula"], "La cédula del representante")
    data["email"] = _rep_email(data["email"])
    prof = await st.get_profile(user.oid)
    from app.shared.crypto.field_encryption import hash_field
    if prof and prof.get("cedulaHash") == hash_field(data["cedula"]):
        raise BadRequest("La cédula del representante no puede ser la tuya")
    rep, _created = await st.upsert_representative(data, user.oid)
    ln = await st.link(user.oid, rep["_id"], data["relationship"], data["isPrimary"], user.user_id)
    await _audit(user, request, "LINK_CREATED", {"linkId": str(ln["_id"])})
    done = await st.refresh_completion(user.oid)
    await _relock_rep(user)
    return to_json({"representatives": await st.links_of(user.oid), "completed": done, "locks": await _locks(user)})


@router.put("/representatives/{link_id}")
async def update_rep(link_id: str, body: RepresentativeUpdate, request: Request, user: AuthUser = Depends(require_student)):
    ln = await _my_link(user, link_id)
    await _assert_rep_unlocked(user)
    data = body.model_dump(exclude_none=True)
    if "firstNames" in data or "lastNames" in data:
        rep = await get_database()["representatives"].find_one({"_id": ln["representativeId"]})
        first = data.get("firstNames") or st.dec(rep.get("firstNamesEnc")) or ""
        last = data.get("lastNames") or st.dec(rep.get("lastNamesEnc")) or ""
        data["names"] = st.full_name(first, last, "del representante")
        data["firstNames"], data["lastNames"] = _norm_name(first), _norm_name(last)
    if "email" in data:
        data["email"] = _rep_email(data["email"])
    personal = {k: v for k, v in data.items() if k in ("names", "firstNames", "lastNames", "phone", "email")}
    note = None
    if personal and not await st.update_rep_data(ln["representativeId"], personal, only_if_single_owner=user.oid):
        note = "Este representante también está vinculado a otro estudiante; sus datos solo los puede cambiar la institución."
    if "relationship" in data:
        await get_database()["student_representatives"].update_one({"_id": ln["_id"]}, {"$set": {"relationship": data["relationship"]}})
    if data.get("isPrimary"):
        await st.set_primary(ln["_id"], user.oid)
        await _audit(user, request, "LINK_PRIMARY_SET", {"linkId": link_id})
    await _relock_rep(user)
    return to_json({"representatives": await st.links_of(user.oid), "note": note, "locks": await _locks(user)})


@router.delete("/representatives/{link_id}")
async def remove_rep(link_id: str, request: Request, user: AuthUser = Depends(require_student)):
    ln = await _my_link(user, link_id)
    await _assert_rep_unlocked(user)
    await st.unlink(ln["_id"], user.user_id, user.oid)
    await _audit(user, request, "LINK_REMOVED", {"linkId": link_id})
    return to_json({"representatives": await st.links_of(user.oid), "completed": await st.refresh_completion(user.oid)})


# ── solicitudes ───────────────────────────────────────────────────────────────
async def build_dto(user: AuthUser, body: MyRequestIn) -> CreateRequestDTO:
    """Los datos de identidad salen del perfil cifrado, no del navegador."""
    prof = await st.get_profile(user.oid)
    if not prof or not prof.get("completed"):
        raise AppError(400, "Completa tus datos y los de tu representante antes de enviar solicitudes.", code="PROFILE_INCOMPLETE")
    ln = await _my_link(user, body.representativeLinkId)
    rep = await get_database()["representatives"].find_one({"_id": ln["representativeId"]})
    p = st.profile_out(prof)
    raw = {"requestType": body.requestType, "representativeName": st.dec(rep.get("namesEnc")),
           "representativeDni": st.dec(rep.get("cedulaEnc")), "studentName": p["names"], "studentDni": p["cedula"],
           "course": p["course"], "paralelo": p["paralelo"], "payload": body.payload}
    return validate_dto(create_request_schema(body.requestType), raw, CreateRequestDTO)  # type: ignore[return-value]


def _mine_out(r: dict) -> dict:
    return {k: r.get(k) for k in ("_id", "requestNumber", "requestType", "status", "course", "paralelo", "campus", "payload",
                                  "studentName", "representativeName", "rejectionReason", "dueAt", "createdAt", "updatedAt")}


@router.get("/requests")
async def my_requests(user: AuthUser = Depends(require_student)):
    rows = await get_database()["requests"].find({"studentUserId": user.oid}).sort("createdAt", -1).to_list(200)
    from app.modules.teachers.service import evaluations_for_requests
    ev = await evaluations_for_requests([r["_id"] for r in rows])
    return to_json({"data": [{**_mine_out(r), "evaluations": ev.get(r["_id"], [])} for r in rows]})


# ── avisos del estudiante (p. ej. evaluación programada por el docente) ──────
@router.get("/notifications")
async def my_notifications(user: AuthUser = Depends(require_student)):
    from app.modules.teachers.service import notifications_for
    return to_json(await notifications_for(user.oid, 50))


class ReadIn(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    ids: list[str] | Literal["all"]


@router.post("/notifications/read")
async def read_notifications(body: ReadIn, user: AuthUser = Depends(require_student)):
    from app.modules.teachers.service import mark_read
    return {"updated": await mark_read(user.oid, body.ids)}


@router.get("/subjects")
async def my_subjects(requestType: str = "proceso_mejora", user: AuthUser = Depends(require_student),
                      svc: RequestsService = Depends(service)):
    """Materias que cursa el estudiante (según su curso y paralelo) y habilitadas para el trámite."""
    from app.modules.catalog import REQUEST_TYPES
    from app.modules.curriculum import specialty_of, subjects_for
    if requestType not in REQUEST_TYPES:
        raise AppError(400, "Tipo de trámite no válido")
    prof = await st.get_profile(user.oid)
    if not prof or not prof.get("completed"):
        raise AppError(400, "Completa tus datos primero", code="PROFILE_INCOMPLETE")
    p = st.profile_out(prof)
    spec = specialty_of(p["course"], p["paralelo"])
    inactive = await svc.config_service.inactive_subjects(requestType, p["course"], spec)
    subjects = [s for s in subjects_for(p["course"], p["paralelo"]) if s not in inactive]
    from app.modules.limits.service import current_period, subjects_used_in_trimester
    period = await current_period()
    used = await subjects_used_in_trimester(p["cedula"], requestType, period)
    async for r in get_database()["requests"].find({"studentDni": p["cedula"], "requestType": requestType,
                                                    "status": {"$in": ["pendiente", "en_revision"]}}, {"payload.subjects": 1}):
        used.update((r.get("payload") or {}).get("subjects", []))  # también las que están en trámite
    if (await get_database()["users"].find_one({"_id": user.oid}, {"testAccount": 1}) or {}).get("testAccount"):
        from app.modules.curriculum import TEST_SUBJECT
        subjects.append(TEST_SUBJECT)
    # Las ya pedidas en el trimestre se informan aparte para que el formulario las muestre deshabilitadas.
    return {"course": p["course"], "specialty": spec, "subjects": subjects,
            "usedThisTrimester": [s for s in subjects if s in used], "trimester": period["trimestre"] if period else None}


@router.get("/requests/quota")
async def my_quota(requestType: str = "proceso_mejora", user: AuthUser = Depends(require_student),
                   svc: RequestsService = Depends(service)):
    prof = await st.get_profile(user.oid)
    if not prof or not prof.get("completed"):
        raise AppError(400, "Completa tus datos primero", code="PROFILE_INCOMPLETE")
    p = st.profile_out(prof)
    try:
        cupo = await svc.plazos_service.get_cupo_info(p["cedula"], p["course"], requestType)
    except AppError:
        return {"aporte": None, "limite": 0, "consumido": 0, "disponible": 0}
    a = cupo["aporteActual"]
    return to_json({"aporte": {"name": a["aporte"]["name"], "trimestre": a["trimestre"]["name"], "anio": a["anioLectivo"]["name"],
                               "endDate": a["aporte"]["endDate"]},
                    "limite": cupo["limite"], "consumido": cupo["consumido"], "disponible": cupo["disponible"]})


@router.post("/requests", status_code=201, dependencies=[Depends(Throttle("MeController.create", 10))])
async def create_mine(body: MyRequestIn, request: Request, user: AuthUser = Depends(require_student),
                      svc: RequestsService = Depends(service)):
    from app.modules.system.submission_window import assert_open
    await assert_open()  # primero el cierre por fechas: es el motivo que el estudiante debe ver
    dto = await build_dto(user, body)
    doc = await workflow.create_request(svc, dto, user, client_ip(request))
    return JSONResponse(status_code=201, content=to_json(_mine_out(doc)))


@router.post("/requests/preview", dependencies=[Depends(Throttle("MeController.preview"))])
async def preview_mine(body: MyRequestIn, user: AuthUser = Depends(require_student), pdf=Depends(pdf_service)):
    from app.modules.system.submission_window import assert_open
    await assert_open()
    dto = await build_dto(user, body)
    return Response(content=await pdf.generate_preview_pdf(dto), media_type="application/pdf",
                    headers={"Content-Disposition": 'inline; filename="preview.pdf"'})


async def _mine(user: AuthUser, id_: str) -> dict:
    oid = parse_object_id(id_)
    doc = await get_database()["requests"].find_one({"_id": oid, "studentUserId": user.oid})
    if not doc:
        raise NotFound(f"Solicitud con id {id_} no encontrada")
    return doc


@router.post("/requests/{id_}/cancel")
async def cancel_mine(id_: str, request: Request, user: AuthUser = Depends(require_student)):
    from app.core.config import get_settings
    if not get_settings().student_can_cancel:
        raise AppError(403, "Las solicitudes no se pueden cancelar desde la cuenta del estudiante. "
                            "Si necesitas anularla, comunícate con la institución.", code="CANCEL_DISABLED")
    doc = await _mine(user, id_)
    if doc["status"] not in ACTIVE:
        raise AppError(409, "Solo puedes cancelar solicitudes pendientes o en revisión", code="INVALID_TRANSITION")
    ev = workflow.status_event("CANCELLED_BY_STUDENT", doc["status"], CANCELADO, user, client_ip(request))
    ev["actor_name"] = doc.get("studentName")
    out = await workflow.update_with_event(doc["_id"], {"status": doc["status"], "studentUserId": user.oid},
                                           {"status": CANCELADO}, ev)
    return to_json(_mine_out(out))


@router.get("/requests/{id_}/pdf")
async def my_pdf(id_: str, user: AuthUser = Depends(require_student), pdf=Depends(pdf_service)):
    doc = await _mine(user, id_)
    return Response(content=await pdf.generate_request_pdf(doc), media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="solicitud-{doc["requestNumber"]}.pdf"'})


