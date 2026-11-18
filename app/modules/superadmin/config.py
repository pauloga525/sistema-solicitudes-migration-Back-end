"""Plazos de envío, límites, excepciones y configuración del sistema (SUPERADMIN)."""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import Field
from pymongo.errors import DuplicateKeyError

from app.core.config import get_settings
from app.core.dependencies import AuthUser, require_superadmin
from app.core.exceptions import BadRequest, Conflict, NotFound
from app.database.connection import get_database
from app.modules.auth.schemas import StrictModel
from app.modules.catalog import REQUEST_TYPES
from app.modules.limits import service as limits
from app.modules.students import service as st
from app.modules.superadmin.common import audit
from app.modules.system import service as system
from app.shared.serialization import to_json
from app.shared.utilities.dates import utcnow
from app.shared.validators.object_id import parse_object_id

router = APIRouter()
TypeLit = Literal[REQUEST_TYPES]  # type: ignore[valid-type]


class DeadlineIn(StrictModel):
    requestType: TypeLit
    periodKey: str | None = Field(None, max_length=200)
    opensAt: str = Field(max_length=40)
    closesAt: str = Field(max_length=40)
    isActive: bool = True


class BulkDeadlineIn(StrictModel):
    requestType: TypeLit
    periodKeys: list[str] = Field(min_length=1, max_length=60)
    openHour: str = Field("00:00", pattern=r"^\d{2}:\d{2}$")
    closeHour: str = Field("23:59", pattern=r"^\d{2}:\d{2}$")


class LimitIn(StrictModel):
    requestType: TypeLit
    scope: Literal["aporte", "day", "week"]
    maxRequests: int = Field(ge=1, le=100)
    isActive: bool = True


class PreviewIn(StrictModel):
    requestType: TypeLit
    scope: Literal["aporte", "day", "week"]
    maxRequests: int = Field(ge=1, le=100)


class ExceptionIn(StrictModel):
    studentUserId: str = Field(min_length=24, max_length=24)
    requestType: TypeLit
    periodKey: str | None = Field(None, max_length=200)
    extra: int = Field(ge=1, le=20)
    reason: str = Field(min_length=10, max_length=500)


class ReasonIn(StrictModel):
    reason: str = Field(min_length=10, max_length=500)


class MaintenanceIn(StrictModel):
    enabled: bool
    message: str | None = Field(None, max_length=300)


class ParamsIn(StrictModel):
    dueDaysDefault: int | None = Field(None, ge=1, le=90)
    userAuditRetentionDays: int | None = Field(None, ge=180, le=3650)
    errorRetentionDays: int | None = Field(None, ge=30, le=730)


# ── periodos y plazos de envío ────────────────────────────────────────────────
@router.get("/periods")
async def periods(_: AuthUser = Depends(require_superadmin)):
    return to_json(await limits.list_periods())


def _dl_out(d: dict[str, Any], periods: dict[str, dict]) -> dict[str, Any]:
    p = periods.get(d.get("periodKey") or "")
    now = utcnow()
    state = ("inactivo" if not d.get("isActive") else "abierto" if limits._utc(d["opensAt"]) <= now <= limits._utc(d["closesAt"])
             else "por abrir" if now < limits._utc(d["opensAt"]) else "cerrado")
    return {"id": str(d["_id"]), "requestType": d["requestType"], "periodKey": d.get("periodKey"),
            "periodLabel": f"{p['anio']} · {p['trimestre']} · {p['aporte']}" if p else ("Todos los periodos" if not d.get("periodKey") else "Periodo eliminado"),
            "opensAt": d["opensAt"], "closesAt": d["closesAt"], "opensAtLocal": limits.to_local(d["opensAt"]),
            "closesAtLocal": limits.to_local(d["closesAt"]), "isActive": d.get("isActive", True), "state": state}


@router.get("/deadlines")
async def list_deadlines(_: AuthUser = Depends(require_superadmin)):
    ps = {p["key"]: p for p in await limits.list_periods()}
    rows = await get_database()["request_deadlines"].find({}).sort("opensAt", -1).to_list(500)
    return to_json([_dl_out(d, ps) for d in rows])


async def _upsert_deadline(request_type: str, period_key: str | None, opens, closes, active: bool) -> None:
    if closes <= opens:
        raise BadRequest("El cierre debe ser posterior a la apertura")
    await get_database()["request_deadlines"].update_one(
        {"requestType": request_type, "periodKey": period_key},
        {"$set": {"opensAt": opens, "closesAt": closes, "isActive": active, "updatedAt": utcnow()},
         "$setOnInsert": {"createdAt": utcnow()}}, upsert=True)


@router.put("/deadlines")
async def put_deadline(body: DeadlineIn, request: Request, actor: AuthUser = Depends(require_superadmin)):
    if body.periodKey and not await limits.find_period(body.periodKey):
        raise BadRequest("El periodo no existe")
    await _upsert_deadline(body.requestType, body.periodKey, limits.parse_local(body.opensAt), limits.parse_local(body.closesAt), body.isActive)
    await audit(actor, request, "DEADLINE_CHANGED", metadata=body.model_dump())
    return await list_deadlines(actor)


@router.post("/deadlines/bulk")
async def bulk_deadlines(body: BulkDeadlineIn, request: Request, actor: AuthUser = Depends(require_superadmin)):
    ps = {p["key"]: p for p in await limits.list_periods()}
    missing = [k for k in body.periodKeys if k not in ps]
    if missing:
        raise BadRequest("Uno o más periodos no existen")
    for k in body.periodKeys:
        p = ps[k]
        start = p["startDate"].astimezone(limits.TZ).date().isoformat()
        end = p["endDate"].astimezone(limits.TZ).date().isoformat()
        await _upsert_deadline(body.requestType, k, limits.parse_local(f"{start}T{body.openHour}"),
                               limits.parse_local(f"{end}T{body.closeHour}"), True)
    await audit(actor, request, "DEADLINE_CHANGED", metadata={"bulk": True, "periods": len(body.periodKeys)})
    return await list_deadlines(actor)


@router.post("/deadlines/{id_}/toggle")
async def toggle_deadline(id_: str, request: Request, actor: AuthUser = Depends(require_superadmin)):
    oid = parse_object_id(id_)
    d = await get_database()["request_deadlines"].find_one({"_id": oid})
    if not d:
        raise NotFound("Plazo no encontrado")
    await get_database()["request_deadlines"].update_one({"_id": oid}, {"$set": {"isActive": not d.get("isActive", True), "updatedAt": utcnow()}})
    await audit(actor, request, "DEADLINE_CHANGED", metadata={"id": id_, "isActive": not d.get("isActive", True)})
    return await list_deadlines(actor)


# ── límites ───────────────────────────────────────────────────────────────────
@router.get("/limits")
async def list_limits(_: AuthUser = Depends(require_superadmin)):
    rows = await get_database()["request_limits"].find({}).sort([("requestType", 1), ("scope", 1)]).to_list(100)
    return to_json({"limits": [{"id": str(l["_id"]), "requestType": l["requestType"], "scope": l["scope"],
                                "scopeLabel": limits.SCOPE_LABELS[l["scope"]], "maxRequests": l["maxRequests"],
                                "isActive": l.get("isActive", True), "updatedAt": l.get("updatedAt")} for l in rows],
                    "subjectQuota": {"basica": 4, "bachillerato": 6,
                                     "note": "Cupo de materias por aporte del sistema original (4 hasta 10mo, 6 en Bachillerato)."}})


@router.post("/limits/preview")
async def preview_limit(body: PreviewIn, _: AuthUser = Depends(require_superadmin)):
    return to_json(await limits.limit_preview(body.requestType, body.scope, body.maxRequests))


@router.put("/limits")
async def put_limit(body: LimitIn, request: Request, actor: AuthUser = Depends(require_superadmin)):
    col = get_database()["request_limits"]
    prev = await col.find_one({"requestType": body.requestType, "scope": body.scope})
    await col.update_one({"requestType": body.requestType, "scope": body.scope},
                         {"$set": {"maxRequests": body.maxRequests, "isActive": body.isActive, "updatedAt": utcnow(),
                                   "updatedBy": actor.user_id}, "$setOnInsert": {"createdAt": utcnow()}}, upsert=True)
    await audit(actor, request, "LIMIT_CHANGED", metadata={**body.model_dump(),
                                                           "previous": prev and {"maxRequests": prev["maxRequests"], "isActive": prev.get("isActive")}})
    return await list_limits(actor)


# ── excepciones ───────────────────────────────────────────────────────────────
@router.get("/limit-exceptions")
async def list_exceptions(_: AuthUser = Depends(require_superadmin)):
    db = get_database()
    rows = await db["limit_exceptions"].find({}).sort("createdAt", -1).to_list(500)
    profs = {p["userId"]: p async for p in db["student_profiles"].find({"userId": {"$in": [r["studentUserId"] for r in rows]}})}
    return to_json([{"id": str(r["_id"]), "studentUserId": str(r["studentUserId"]),
                     "studentName": st.dec(profs.get(r["studentUserId"], {}).get("namesEnc")), "requestType": r["requestType"],
                     "windowKey": r["windowKey"], "extra": r["extra"], "reason": r["reason"], "isActive": r.get("isActive", True),
                     "createdAt": r["createdAt"], "revokedAt": r.get("revokedAt"), "revokeReason": r.get("revokeReason")} for r in rows])


@router.post("/limit-exceptions", status_code=201)
async def grant_exception(body: ExceptionIn, request: Request, actor: AuthUser = Depends(require_superadmin)):
    sid = parse_object_id(body.studentUserId)
    if not await get_database()["users"].find_one({"_id": sid, "role": "STUDENT"}, {"_id": 1}):
        raise NotFound("Estudiante no encontrado")
    period = await limits.find_period(body.periodKey) if body.periodKey else await limits.current_period()
    if not period:
        raise BadRequest("No hay un periodo activo; indica el periodo de la excepción")
    key = f"aporte:{period['key']}"
    try:
        res = await get_database()["limit_exceptions"].insert_one({
            "studentUserId": sid, "requestType": body.requestType, "windowKey": key, "extra": body.extra,
            "reason": body.reason.strip(), "isActive": True, "createdBy": actor.user_id, "createdAt": utcnow()})
    except DuplicateKeyError as exc:
        raise Conflict("Ese estudiante ya tiene una excepción activa para ese periodo. Revócala antes de crear otra.") from exc
    await audit(actor, request, "LIMIT_EXCEPTION_GRANTED", metadata={"id": str(res.inserted_id), "studentUserId": body.studentUserId,
                                                                     "extra": body.extra, "windowKey": key, "reason": body.reason})
    return await list_exceptions(actor)


@router.post("/limit-exceptions/{id_}/revoke")
async def revoke_exception(id_: str, body: ReasonIn, request: Request, actor: AuthUser = Depends(require_superadmin)):
    oid = parse_object_id(id_)
    res = await get_database()["limit_exceptions"].update_one(
        {"_id": oid, "isActive": True},
        {"$set": {"isActive": False, "revokedAt": utcnow(), "revokedBy": actor.user_id, "revokeReason": body.reason.strip()}})
    if res.matched_count == 0:
        raise NotFound("Excepción no encontrada o ya revocada")
    await audit(actor, request, "LIMIT_EXCEPTION_REVOKED", metadata={"id": id_, "reason": body.reason})
    return await list_exceptions(actor)


# ── configuración ─────────────────────────────────────────────────────────────
async def _retention() -> dict[str, int]:
    s = await system.get_settings_doc()
    cfg = get_settings()
    return {"userAuditRetentionDays": int(s.get("userAuditRetentionDays") or cfg.user_audit_retention_days),
            "errorRetentionDays": int(s.get("errorRetentionDays") or cfg.error_log_retention_days),
            "rateLimitRetentionHours": cfg.rate_limit_retention_s // 3600}


@router.get("/settings")
async def get_cfg(_: AuthUser = Depends(require_superadmin)):
    s = await system.get_settings_doc()
    m = await system.get_maintenance()
    by = None
    if m.get("enabledBy"):
        u = await get_database()["users"].find_one({"_id": parse_object_id(m["enabledBy"])}, {"name": 1, "email": 1})
        by = u and (u.get("name") or u.get("email"))
    return to_json({"maintenance": {**m, "enabledByName": by}, "dueDaysDefault": s.get("dueDaysDefault"),
                    "retention": await _retention(), "activePeriod": await limits.current_period(),
                    "emailDomain": get_settings().institutional_email_domain, "timezone": "America/Guayaquil"})


@router.put("/settings/maintenance")
async def put_maintenance(body: MaintenanceIn, request: Request, actor: AuthUser = Depends(require_superadmin)):
    msg = (body.message or "").strip() or None
    await system.set_maintenance(body.enabled, msg, actor.user_id)
    await audit(actor, request, "MAINTENANCE_MODE_ENABLED" if body.enabled else "MAINTENANCE_MODE_DISABLED", metadata={"message": msg})
    return await get_cfg(actor)


@router.put("/settings/params")
async def put_params(body: ParamsIn, request: Request, actor: AuthUser = Depends(require_superadmin)):
    data = body.model_dump(exclude_unset=True)
    if not data:
        raise BadRequest("No hay cambios")
    await system.update_settings(data)
    from app.database.indexes import apply_retention
    await apply_retention(get_database())
    await audit(actor, request, "SETTINGS_CHANGED", metadata=data)
    return await get_cfg(actor)
