"""Auditoría, errores del sistema y tablero (SUPERADMIN)."""
from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import Field

from app.core.dependencies import AuthUser, require_superadmin
from app.database.connection import get_database
from app.modules.audit.service import REQUEST_EVENT_LABELS, ROLE_LABELS, USER_EVENT_LABELS, request_history
from app.modules.auth.schemas import StrictModel
from app.modules.catalog import REQUEST_STATUSES
from app.modules.limits import service as limits
from app.modules.requests.state_machine import ACTIVE, STATUS_LABELS
from app.modules.superadmin.common import audit, page_args, paged
from app.modules.system import service as system
from app.shared.serialization import to_json
from app.shared.utilities.dates import utcnow
from app.shared.validators.object_id import parse_object_id

router = APIRouter()


class NoteIn(StrictModel):
    note: str | None = Field(None, max_length=500)


# ── auditoría ─────────────────────────────────────────────────────────────────
@router.get("/audit/request-events")
async def request_events(eventType: str | None = None, actorRole: str | None = None, requestNumber: str | None = None,
                         from_: str | None = Query(None, alias="from"), to: str | None = None, page: int = 1,
                         _: AuthUser = Depends(require_superadmin)):
    p, size = page_args(page)
    db = get_database()
    q: dict[str, Any] = {}
    if eventType in REQUEST_EVENT_LABELS:
        q["eventType"] = eventType
    if actorRole in ROLE_LABELS:
        q["actorRole"] = actorRole
    rng = system.date_range(from_, to)
    if rng:
        q["createdAt"] = rng
    if requestNumber and requestNumber.strip():
        ids = [r["_id"] async for r in db["requests"].find({"requestNumber": {"$regex": re.escape(requestNumber.strip()[:30]), "$options": "i"}}, {"_id": 1}).limit(200)]
        q["requestId"] = {"$in": ids}
    total = await db["request_events"].count_documents(q)
    rows = await db["request_events"].find(q).sort("createdAt", -1).skip((p - 1) * size).limit(size).to_list(size)
    nums = {r["_id"]: r["requestNumber"] async for r in db["requests"].find({"_id": {"$in": [e["requestId"] for e in rows]}}, {"requestNumber": 1})}
    return to_json(paged([{"id": str(e["_id"]), "requestId": str(e["requestId"]), "requestNumber": nums.get(e["requestId"]),
                           "eventType": e["eventType"], "eventLabel": REQUEST_EVENT_LABELS.get(e["eventType"], e["eventType"]),
                           "fromStatus": e.get("fromStatus"), "toStatus": e.get("toStatus"), "actorName": e.get("actorName"),
                           "actorRole": e.get("actorRole"), "actorRoleLabel": ROLE_LABELS.get(e.get("actorRole") or "", ""),
                           "actorIp": e.get("actorIp"), "comment": e.get("comment"), "createdAt": e["createdAt"]} for e in rows],
                         total, p, size))


@router.get("/audit/requests/{id_}")
async def request_summary(id_: str, _: AuthUser = Depends(require_superadmin)):
    oid = parse_object_id(id_)
    r = await get_database()["requests"].find_one({"_id": oid}, {"requestNumber": 1, "status": 1, "createdAt": 1})
    hist = await request_history(oid)
    counts: dict[str, int] = {}
    for e in hist:
        counts[e["eventType"]] = counts.get(e["eventType"], 0) + 1
    return to_json({"requestNumber": r and r.get("requestNumber"), "status": r and r.get("status"),
                    "events": hist, "counts": counts,
                    "corrections": counts.get("STATUS_CORRECTED", 0),
                    "lastEventAt": hist[-1]["createdAt"] if hist else None})


@router.get("/audit/users")
async def user_events(eventType: str | None = None, result: str | None = None, actor: str | None = None,
                      from_: str | None = Query(None, alias="from"), to: str | None = None, page: int = 1,
                      _: AuthUser = Depends(require_superadmin)):
    p, size = page_args(page)
    q: dict[str, Any] = {}
    if eventType in USER_EVENT_LABELS:
        q["eventType"] = eventType
    if result in ("SUCCESS", "FAILURE"):
        q["result"] = result
    if actor and actor.strip():
        rx = {"$regex": re.escape(actor.strip()[:80]), "$options": "i"}
        q["$or"] = [{"actorEmail": rx}, {"targetEmail": rx}, {"actorIp": rx}]
    rng = system.date_range(from_, to)
    if rng:
        q["createdAt"] = rng
    col = get_database()["user_events"]
    total = await col.count_documents(q)
    rows = await col.find(q).sort("createdAt", -1).skip((p - 1) * size).limit(size).to_list(size)
    return to_json(paged([{"id": str(e["_id"]), "eventType": e["eventType"], "eventLabel": USER_EVENT_LABELS.get(e["eventType"], e["eventType"]),
                           "result": e.get("result"), "actorEmail": e.get("actorEmail"), "actorRole": e.get("actorRole"),
                           "actorIp": e.get("actorIp"), "targetEmail": e.get("targetEmail"), "targetRole": e.get("targetRole"),
                           "metadata": e.get("metadata"), "createdAt": e["createdAt"]} for e in rows], total, p, size))


@router.get("/audit/labels")
async def labels(_: AuthUser = Depends(require_superadmin)):
    return {"request": REQUEST_EVENT_LABELS, "user": USER_EVENT_LABELS, "roles": ROLE_LABELS, "status": STATUS_LABELS}


# ── errores ───────────────────────────────────────────────────────────────────
@router.get("/errors")
async def errors(level: str | None = None, resolved: str | None = None, source: str | None = None,
                 from_: str | None = Query(None, alias="from"), to: str | None = None, page: int = 1,
                 _: AuthUser = Depends(require_superadmin)):
    p, _s = page_args(page)
    data = await system.list_errors(level if level in ("ERROR", "WARN") else None, resolved, source, from_, to, p)
    return to_json({**data, "stats": await system.error_stats(), "sources": await system.error_sources()})


@router.get("/errors/{id_}")
async def error_detail(id_: str, _: AuthUser = Depends(require_superadmin)):
    # La traza solo se muestra aquí, al SUPERADMIN autenticado; nunca en respuestas de error.
    return to_json(await system.get_error(parse_object_id(id_)))


@router.post("/errors/{id_}/resolve")
async def error_resolve(id_: str, body: NoteIn, request: Request, actor: AuthUser = Depends(require_superadmin)):
    await system.resolve_error(parse_object_id(id_), actor.user_id, body.note)
    await audit(actor, request, "ERROR_RESOLVED", metadata={"id": id_})
    return to_json(await system.get_error(parse_object_id(id_)))


@router.post("/errors/{id_}/reopen")
async def error_reopen(id_: str, request: Request, actor: AuthUser = Depends(require_superadmin)):
    await system.reopen_error(parse_object_id(id_))
    await audit(actor, request, "ERROR_REOPENED", metadata={"id": id_})
    return to_json(await system.get_error(parse_object_id(id_)))


# ── tablero ───────────────────────────────────────────────────────────────────
@router.get("/dashboard")
async def dashboard(_: AuthUser = Depends(require_superadmin)):
    db = get_database()
    now = utcnow()
    by_status = {s: 0 for s in REQUEST_STATUSES}
    async for g in db["requests"].aggregate([{"$group": {"_id": "$status", "n": {"$sum": 1}}}]):
        by_status[g["_id"]] = g["n"]
    users_by_role: dict[str, dict[str, int]] = {r: {"active": 0, "inactive": 0, "pendingPassword": 0} for r in ("STUDENT", "ADMIN", "SUPERADMIN")}
    async for g in db["users"].aggregate([{"$group": {"_id": {"role": "$role", "active": "$isActive",
                                                              "fpc": "$forcePasswordChange"}, "n": {"$sum": 1}}}]):
        r = users_by_role.setdefault(g["_id"]["role"], {"active": 0, "inactive": 0, "pendingPassword": 0})
        r["active" if g["_id"].get("active", True) else "inactive"] += g["n"]
        if g["_id"].get("fpc"):
            r["pendingPassword"] += g["n"]
    period = await limits.current_period(now)
    students_total = await db["users"].count_documents({"role": "STUDENT"})
    profiles_done = await db["student_profiles"].count_documents({"completed": True})
    in_padron = await db["student_profiles"].count_documents({"padronMatch": True})
    with_requests_period = len(await db["requests"].distinct("studentDni", {"periodKey": period["key"]})) if period else 0
    linked_students = len(await db["student_representatives"].distinct("studentUserId", {"active": True}))
    reps_total = await db["representatives"].count_documents({})
    linked_reps = await db["student_representatives"].distinct("representativeId", {"active": True})
    multi = await db["student_representatives"].aggregate([{"$match": {"active": True}},
                                                           {"$group": {"_id": "$representativeId", "n": {"$sum": 1}}},
                                                           {"$match": {"n": {"$gt": 1}}}, {"$count": "n"}]).to_list(1)
    overdue = await db["requests"].count_documents({"status": {"$in": list(ACTIVE)}, "dueAt": {"$lt": now}})
    soon = await db["requests"].count_documents({"status": {"$in": list(ACTIVE)}, "dueAt": {"$gte": now, "$lt": now + timedelta(days=3)}})
    upcoming_deadlines = await db["request_deadlines"].find({"isActive": True, "closesAt": {"$gte": now, "$lt": now + timedelta(days=7)}}).to_list(10)
    recent = await db["request_events"].find({}).sort("createdAt", -1).limit(8).to_list(8)
    nums = {r["_id"]: r["requestNumber"] async for r in db["requests"].find({"_id": {"$in": [e["requestId"] for e in recent]}}, {"requestNumber": 1})}
    recent_users = await db["user_events"].find({"eventType": {"$nin": ["LOGIN_SUCCESS", "LOGOUT", "SENSITIVE_DATA_VIEWED"]}}).sort("createdAt", -1).limit(6).to_list(6)
    failed_logins = await db["user_events"].count_documents({"eventType": "LOGIN_FAILED", "createdAt": {"$gte": now - timedelta(hours=24)}})
    m = await system.get_maintenance()
    return to_json({
        "generatedAt": now,
        "requests": {"byStatus": by_status, "total": sum(by_status.values()),
                     "needsAction": by_status.get("pendiente", 0) + by_status.get("en_revision", 0),
                     "today": await db["requests"].count_documents({"createdAt": {"$gte": now - timedelta(hours=24)}})},
        "users": users_by_role,
        "students": {"total": students_total, "profileCompleted": profiles_done, "inPadron": in_padron,
                     "withoutRepresentative": max(0, students_total - linked_students), "withRequestsThisPeriod": with_requests_period},
        "representatives": {"total": reps_total, "linked": len(linked_reps), "orphans": max(0, reps_total - len(linked_reps)),
                            "withSeveralStudents": multi[0]["n"] if multi else 0},
        "deadlines": {"overdue": overdue, "dueSoon": soon, "period": period,
                      "closingSoon": [{"requestType": d["requestType"], "closesAt": d["closesAt"]} for d in upcoming_deadlines]},
        "errors": await system.error_stats(),
        "security": {"failedLogins24h": failed_logins},
        "maintenance": {"enabled": m["enabled"], "message": m["message"]},
        "recentActivity": [{"id": str(e["_id"]), "requestId": str(e["requestId"]), "requestNumber": nums.get(e["requestId"]),
                            "eventLabel": REQUEST_EVENT_LABELS.get(e["eventType"], e["eventType"]), "toStatus": e.get("toStatus"),
                            "actorName": e.get("actorName"), "actorRole": e.get("actorRole"), "createdAt": e["createdAt"]} for e in recent],
        "recentAdmin": [{"id": str(e["_id"]), "eventLabel": USER_EVENT_LABELS.get(e["eventType"], e["eventType"]),
                         "result": e.get("result"), "actorEmail": e.get("actorEmail"), "targetEmail": e.get("targetEmail"),
                         "createdAt": e["createdAt"]} for e in recent_users],
    })
