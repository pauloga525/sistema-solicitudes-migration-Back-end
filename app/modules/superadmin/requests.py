"""Gestión global de solicitudes (SUPERADMIN): filtros, detalle con auditoría, corrección de estado,
responsable, plazos de atención y exportación."""
from __future__ import annotations

import io
import re
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response
from pydantic import Field

from app.core.config import get_settings
from app.core.dependencies import AuthUser, client_ip, require_superadmin
from app.core.exceptions import AppError, BadRequest, NotFound
from app.database.connection import get_database
from app.modules.audit.service import log_request_event, request_history
from app.modules.auth.schemas import StrictModel
from app.modules.catalog import REQUEST_STATUSES, REQUEST_TYPES
from app.modules.limits.service import fmt_local, parse_local, to_local
from app.modules.requests import workflow
from app.modules.requests.pdf_service import PdfService
from app.modules.requests.state_machine import (ACTIVE, MIN_CORRECTION_REASON, RECHAZADO, STATUS_LABELS,
                                                assert_superadmin_transition, superadmin_targets)
from app.modules.superadmin.common import audit, page_args, paged
from app.modules.system.service import date_range, enforce_rate_limit
from app.shared.serialization import to_json
from app.shared.utilities.dates import utcnow
from app.shared.validators.object_id import parse_object_id

router = APIRouter(prefix="/requests")
HIDDEN = {"pdfAccessToken": 0}
EXPORT_MAX = 20000


class CorrectIn(StrictModel):
    expectedStatus: str
    status: str
    reason: str = Field(min_length=MIN_CORRECTION_REASON, max_length=1000)


class AssignIn(StrictModel):
    userId: str | None = Field(None, min_length=24, max_length=24)
    reason: str | None = Field(None, max_length=500)


class DueIn(StrictModel):
    dueAt: str | None = Field(None, max_length=40)
    reason: str | None = Field(None, max_length=500)


class BulkDueIn(StrictModel):
    requestType: str
    periodKey: str | None = Field(None, max_length=200)
    status: str | None = None
    dueAt: str | None = Field(None, max_length=40)
    onlyWithoutDue: bool = False
    reason: str = Field(min_length=MIN_CORRECTION_REASON, max_length=500)


def build_query(status: str | None = None, requestType: str | None = None, course: str | None = None,
                paralelo: str | None = None, campus: str | None = None, from_: str | None = None, to: str | None = None,
                search: str | None = None, due: str | None = None, assignedTo: str | None = None,
                origin: str | None = None, periodKey: str | None = None) -> dict[str, Any]:
    q: dict[str, Any] = {}
    if status:
        if status not in REQUEST_STATUSES:
            raise BadRequest("Estado inválido")
        q["status"] = status
    if requestType:
        if requestType not in REQUEST_TYPES:
            raise BadRequest("Tipo inválido")
        q["requestType"] = requestType
    for k, v in (("course", course), ("paralelo", paralelo), ("campus", campus), ("periodKey", periodKey)):
        if v:
            q[k] = str(v)[:200]
    rng = date_range(from_, to)
    if rng:
        q["createdAt"] = rng
    now = utcnow()
    if due == "overdue":
        q.update({"dueAt": {"$lt": now}, "status": {"$in": list(ACTIVE)} if "status" not in q else q["status"]})
    elif due == "with":
        q["dueAt"] = {"$ne": None, "$exists": True}
    elif due == "without":
        q["$or"] = [{"dueAt": None}, {"dueAt": {"$exists": False}}]
    if assignedTo == "none":
        q["assignedTo"] = {"$in": [None]}
    elif assignedTo:
        q["assignedTo"] = parse_object_id(assignedTo)
    if origin == "legacy":
        q["studentUserId"] = {"$exists": False}
    elif origin == "student":
        q["studentUserId"] = {"$exists": True, "$ne": None}
    if search and search.strip():
        rx = {"$regex": re.escape(search.strip()[:80]), "$options": "i"}
        ors = [{"studentName": rx}, {"representativeName": rx}, {"requestNumber": rx}, {"studentDni": rx},
               {"representativeDni": rx}]
        q = {"$and": [q, {"$or": ors}]} if q else {"$or": ors}
    return q


async def _staff_names(ids: set) -> dict[Any, str]:
    ids = {i for i in ids if i}
    if not ids:
        return {}
    return {u["_id"]: (u.get("name") or u.get("email") or u.get("username"))
            async for u in get_database()["users"].find({"_id": {"$in": list(ids)}}, {"name": 1, "email": 1, "username": 1})}


def row_out(r: dict[str, Any], names: dict[Any, str]) -> dict[str, Any]:
    now = utcnow()
    due = r.get("dueAt")
    return {"id": str(r["_id"]), "requestNumber": r["requestNumber"], "requestType": r["requestType"], "status": r["status"],
            "studentName": r.get("studentName"), "studentDni": r.get("studentDni"), "representativeName": r.get("representativeName"),
            "course": r.get("course"), "paralelo": r.get("paralelo"), "campus": r.get("campus"),
            "subjects": (r.get("payload") or {}).get("subjects", []), "createdAt": r.get("createdAt"), "dueAt": due,
            "overdue": bool(due and due < now and r["status"] in ACTIVE),
            "assignedTo": str(r["assignedTo"]) if r.get("assignedTo") else None,
            "assignedName": names.get(r.get("assignedTo")), "legacy": "studentUserId" not in r}


def _filters(status: str | None = None, requestType: str | None = None, course: str | None = None,
             paralelo: str | None = None, campus: str | None = None, from_: str | None = Query(None, alias="from"),
             to: str | None = None, search: str | None = None, due: str | None = None, assignedTo: str | None = None,
             origin: str | None = None, periodKey: str | None = None) -> dict[str, Any]:
    return build_query(status, requestType, course, paralelo, campus, from_, to, search, due, assignedTo, origin, periodKey)


@router.get("")
async def list_requests(page: int = 1, pageSize: int | None = None, q: dict = Depends(_filters),
                        _: AuthUser = Depends(require_superadmin)):
    p, size = page_args(page, pageSize)
    col = get_database()["requests"]
    total = await col.count_documents(q)
    rows = await col.find(q, HIDDEN).sort("createdAt", -1).skip((p - 1) * size).limit(size).to_list(size)
    names = await _staff_names({r.get("assignedTo") for r in rows})
    by_status = {s: 0 for s in REQUEST_STATUSES}
    # Las cifras por estado ignoran el filtro de estado: la franja muestra todas las etapas del conjunto filtrado.
    q_all = {k: v for k, v in q.items() if k != "status"}
    if "$and" in q_all:
        q_all = {"$and": [{k: v for k, v in q_all["$and"][0].items() if k != "status"}, q_all["$and"][1]]}
    async for g in col.aggregate([{"$match": q_all}, {"$group": {"_id": "$status", "n": {"$sum": 1}}}]):
        by_status[g["_id"]] = g["n"]
    return to_json({**paged([row_out(r, names) for r in rows], total, p, size), "byStatus": by_status})


@router.get("/staff")
async def staff(_: AuthUser = Depends(require_superadmin)):
    rows = await get_database()["users"].find({"role": {"$in": ["ADMIN", "SUPERADMIN"]}, "isActive": True},
                                              {"name": 1, "email": 1, "username": 1, "role": 1}).sort("name", 1).to_list(500)
    return to_json([{"id": str(u["_id"]), "name": u.get("name") or u.get("email") or u.get("username"), "role": u["role"]} for u in rows])


@router.get("/export")
async def export(request: Request, q: dict = Depends(_filters), actor: AuthUser = Depends(require_superadmin)):
    await enforce_rate_limit("EXPORT", actor.user_id, "Demasiadas exportaciones seguidas. Espera unos minutos.")
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter

    col = get_database()["requests"]
    total = await col.count_documents(q)
    if total > EXPORT_MAX:
        raise BadRequest(f"La exportación supera {EXPORT_MAX} filas. Ajusta los filtros.")
    rows = await col.find(q, HIDDEN).sort("createdAt", -1).to_list(EXPORT_MAX)
    names = await _staff_names({r.get("assignedTo") for r in rows})
    wb = Workbook()
    ws = wb.active
    ws.title = "Solicitudes"
    head = ["Número", "Tipo", "Estado", "Fecha (Ecuador)", "Estudiante", "Cédula estudiante", "Representante",
            "Cédula representante", "Curso", "Paralelo", "Campus", "Materias", "Plazo de atención", "Vencida",
            "Responsable", "Motivo de negación", "Origen"]
    ws.append(head)
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="002753")
    for r in rows:
        o = row_out(r, names)
        ws.append([o["requestNumber"], o["requestType"], STATUS_LABELS.get(o["status"], o["status"]),
                   fmt_local(r["createdAt"]) if r.get("createdAt") else "", o["studentName"], o["studentDni"],
                   o["representativeName"], r.get("representativeDni"), o["course"], o["paralelo"], o["campus"],
                   ", ".join(o["subjects"]), fmt_local(o["dueAt"]) if o["dueAt"] else "", "Sí" if o["overdue"] else "No",
                   o["assignedName"] or "", r.get("rejectionReason") or "", "Anterior al registro" if o["legacy"] else "Estudiante"])
    for i, w in enumerate([14, 16, 12, 17, 32, 13, 32, 13, 11, 9, 18, 40, 17, 8, 24, 40, 18], start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    buf = io.BytesIO()
    wb.save(buf)
    await audit(actor, request, "EXPORT", metadata={"what": "requests", "rows": len(rows)})
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    return Response(buf.getvalue(), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="solicitudes-{stamp}.xlsx"'})


async def _get(id_: str) -> dict[str, Any]:
    r = await get_database()["requests"].find_one({"_id": parse_object_id(id_)}, HIDDEN)
    if not r:
        raise NotFound(f"Solicitud con id {id_} no encontrada")
    return r


@router.get("/{id_}")
async def detail(id_: str, _: AuthUser = Depends(require_superadmin)):
    # Vista de supervisión: NO dispara la auto-aprobación (esa regla es de la bandeja del ADMIN).
    r = await _get(id_)
    names = await _staff_names({r.get("assignedTo")})
    return to_json({"request": {**r, **row_out(r, names), "rejectionReason": r.get("rejectionReason"),
                                "rejectedBy": r.get("rejectedBy"), "rejectedAt": r.get("rejectedAt"),
                                "representativeDni": r.get("representativeDni"), "dueAtLocal": to_local(r.get("dueAt"))},
                    "history": await request_history(r["_id"]),
                    "allowedCorrections": superadmin_targets(r["status"])})


@router.post("/{id_}/correct-status")
async def correct_status(id_: str, body: CorrectIn, request: Request, actor: AuthUser = Depends(require_superadmin)):
    r = await _get(id_)
    if body.expectedStatus != r["status"]:
        raise AppError(409, workflow.CONFLICT_MSG, code="STALE_STATE")
    if body.status not in REQUEST_STATUSES:
        raise BadRequest("Estado inválido")
    reason = body.reason.strip()
    if len(reason) < MIN_CORRECTION_REASON:
        raise BadRequest(f"El motivo debe tener al menos {MIN_CORRECTION_REASON} caracteres")
    assert_superadmin_transition(r["status"], body.status)
    fields: dict[str, Any] = {"status": body.status, "lastCorrection": {"by": actor.user_id, "at": utcnow(), "reason": reason}}
    if body.status == RECHAZADO:
        fields.update({"rejectionReason": reason, "rejectedAt": utcnow(), "rejectedBy": actor.username})
    ev = workflow.status_event("STATUS_CORRECTED", r["status"], body.status, actor, client_ip(request), reason)
    out = await workflow.update_with_event(r["_id"], {"status": r["status"]}, fields, ev)
    out.pop("pdfAccessToken", None)
    return to_json({"request": out, "history": await request_history(r["_id"])})


@router.post("/{id_}/assign")
async def assign(id_: str, body: AssignIn, request: Request, actor: AuthUser = Depends(require_superadmin)):
    r = await _get(id_)
    new = None
    label = "sin responsable"
    if body.userId:
        u = await get_database()["users"].find_one({"_id": parse_object_id(body.userId), "role": {"$in": ["ADMIN", "SUPERADMIN"]},
                                                    "isActive": True})
        if not u:
            raise BadRequest("El responsable debe ser un ADMIN o SUPERADMIN activo")
        new, label = u["_id"], u.get("name") or u.get("email") or u.get("username")
    ev = {"event_type": "ASSIGNED", "actor_id": actor.user_id, "actor_name": workflow.actor_name(actor),
          "actor_role": actor.role, "actor_ip": client_ip(request),
          "comment": f"Responsable: {label}" + (f". {body.reason.strip()}" if body.reason and body.reason.strip() else "")}
    await workflow.update_with_event(r["_id"], {"updatedAt": r["updatedAt"]} if r.get("updatedAt") else {},
                                     {"assignedTo": new}, ev)
    return await detail(id_, actor)


@router.post("/{id_}/due")
async def set_due(id_: str, body: DueIn, request: Request, actor: AuthUser = Depends(require_superadmin)):
    r = await _get(id_)
    due = parse_local(body.dueAt) if body.dueAt else None
    ev = {"event_type": "DUE_SET" if due else "DUE_CLEARED", "actor_id": actor.user_id, "actor_name": workflow.actor_name(actor),
          "actor_role": actor.role, "actor_ip": client_ip(request),
          "comment": (f"Plazo: {fmt_local(due)}" if due else "Plazo retirado") + (f". {body.reason.strip()}" if body.reason else "")}
    await workflow.update_with_event(r["_id"], {}, {"dueAt": due}, ev)
    return await detail(id_, actor)


@router.post("/due-bulk")
async def bulk_due(body: BulkDueIn, request: Request, actor: AuthUser = Depends(require_superadmin)):
    if body.requestType not in REQUEST_TYPES:
        raise BadRequest("Tipo inválido")
    due = parse_local(body.dueAt) if body.dueAt else None
    q: dict[str, Any] = {"requestType": body.requestType, "status": {"$in": [body.status] if body.status else list(ACTIVE)}}
    if body.status and body.status not in REQUEST_STATUSES:
        raise BadRequest("Estado inválido")
    if body.periodKey:
        q["periodKey"] = body.periodKey
    if body.onlyWithoutDue:
        q["$or"] = [{"dueAt": None}, {"dueAt": {"$exists": False}}]
    db = get_database()
    ids = [d["_id"] async for d in db["requests"].find(q, {"_id": 1}).limit(5000)]
    if not ids:
        return {"updated": 0}
    await db["requests"].update_many({"_id": {"$in": ids}}, {"$set": {"dueAt": due, "updatedAt": utcnow()}})
    comment = (f"Plazo masivo: {fmt_local(due)}" if due else "Plazo retirado (masivo)") + f". {body.reason.strip()}"
    for oid in ids:
        await log_request_event(oid, "DUE_SET" if due else "DUE_CLEARED", actor_id=actor.user_id,
                                actor_name=workflow.actor_name(actor), actor_role=actor.role, actor_ip=client_ip(request),
                                comment=comment)
    await audit(actor, request, "DEADLINE_CHANGED", metadata={"bulk": True, "requests": len(ids), "dueAt": body.dueAt})
    return {"updated": len(ids)}


@router.get("/{id_}/pdf")
async def pdf(id_: str, _: AuthUser = Depends(require_superadmin)):
    r = await _get(id_)
    data = await PdfService(get_settings().assets_dir).generate_request_pdf(r)  # en memoria, sin archivos temporales
    return Response(data, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="solicitud-{r["requestNumber"]}.pdf"'})


