"""Plazos de envío, límites de solicitudes y excepciones, con validación atómica en el servidor.

Atomicidad: cada creación toma un candado por estudiante y tipo (`limit_locks`, documento con `_id`
único y expiración). Mientras lo tiene, cuenta las solicitudes reales (fuente de verdad), valida
cupo de materias, límites y plazo, e inserta. Dos envíos simultáneos del mismo estudiante se
serializan, así que no pueden superar un límite. No depende de transacciones ni deja contadores
que se desincronicen al cancelar o negar.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator
from zoneinfo import ZoneInfo

from bson import ObjectId
from pymongo.errors import DuplicateKeyError

from app.core.exceptions import AppError, BadRequest
from app.database.connection import get_database
from app.shared.utilities.dates import utcnow

TZ = ZoneInfo("America/Guayaquil")
LOCK_TTL = timedelta(seconds=30)
SCOPES = ("aporte", "day", "week")
SCOPE_LABELS = {"aporte": "por aporte", "day": "por día", "week": "por semana"}
COUNTED_STATUSES_EXCLUDED = ["rechazado", "cancelado"]


def _utc(d: datetime) -> datetime:
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def to_local(d: datetime | None) -> str | None:
    return _utc(d).astimezone(TZ).strftime("%Y-%m-%dT%H:%M") if d else None


def parse_local(value: str) -> datetime:
    """'AAAA-MM-DDTHH:MM' en hora de Ecuador → UTC. Con zona explícita se respeta."""
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise BadRequest("Fecha y hora inválidas. Use el formato AAAA-MM-DDTHH:MM.") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(timezone.utc).replace(microsecond=0)


def fmt_local(d: datetime) -> str:
    return _utc(d).astimezone(TZ).strftime("%d/%m/%Y %H:%M")


# ── periodos (aportes del calendario existente) ───────────────────────────────
def aporte_key(anio_id: Any, trimestre: str, aporte: str) -> str:
    # El _id de los aportes se regenera al editar el año; año + nombres es estable.
    return f"{anio_id}|{trimestre}|{aporte}"


async def list_periods() -> list[dict[str, Any]]:
    now = utcnow()
    out = []
    async for anio in get_database()["aniolectivos"].find({}).sort("startDate", -1):
        for t in anio.get("trimestres", []):
            for a in t.get("aportes", []):
                out.append({"key": aporte_key(anio["_id"], t["name"], a["name"]), "anio": anio["name"],
                            "trimestre": t["name"], "aporte": a["name"], "startDate": _utc(a["startDate"]),
                            "endDate": _utc(a["endDate"]),
                            "isCurrent": _utc(a["startDate"]) <= now <= _utc(a["endDate"])})
    return out


async def current_period(at: datetime | None = None) -> dict[str, Any] | None:
    at = at or utcnow()
    for p in await list_periods():
        if p["startDate"] <= at <= p["endDate"]:
            return p
    return None


async def period_for_request(request: dict[str, Any]) -> dict[str, Any] | None:
    """Aporte de una solicitud: el registrado al crearla, el que cubría su fecha de creación o,
    si aún no existe (vista previa), el vigente."""
    periods = await list_periods()
    key = request.get("periodKey")
    if key:
        found = next((p for p in periods if p["key"] == key), None)
        if found:
            return found
    created = request.get("createdAt")
    at = _utc(created) if isinstance(created, datetime) else utcnow()
    return next((p for p in periods if p["startDate"] <= at <= p["endDate"]), None)


async def find_period(key: str) -> dict[str, Any] | None:
    return next((p for p in await list_periods() if p["key"] == key), None)


def window_for(scope: str, period: dict[str, Any] | None, at: datetime) -> tuple[str, datetime, datetime] | None:
    local = _utc(at).astimezone(TZ)
    if scope == "aporte":
        if not period:
            return None
        return f"aporte:{period['key']}", period["startDate"], period["endDate"]
    if scope == "day":
        start = local.replace(hour=0, minute=0, second=0, microsecond=0)
        return f"day:{start.date().isoformat()}", start.astimezone(timezone.utc), (start + timedelta(days=1)).astimezone(timezone.utc)
    start = (local - timedelta(days=local.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    iso = start.isocalendar()
    return f"week:{iso.year}-W{iso.week:02d}", start.astimezone(timezone.utc), (start + timedelta(days=7)).astimezone(timezone.utc)


# ── candado por estudiante ────────────────────────────────────────────────────
@asynccontextmanager
async def student_lock(student_dni: str, request_type: str) -> AsyncIterator[None]:
    col = get_database()["limit_locks"]
    key = f"{student_dni}:{request_type}"
    token = ObjectId()
    for attempt in range(40):  # hasta ~4 s esperando a la otra petición
        now = utcnow()
        try:
            await col.find_one_and_update({"_id": key, "expiresAt": {"$lt": now}},
                                          {"$set": {"token": token, "expiresAt": now + LOCK_TTL}}, upsert=True)
            break
        except DuplicateKeyError:
            await asyncio.sleep(0.1)
    else:
        raise AppError(409, "Hay otra solicitud de este estudiante en proceso. Espera unos segundos e inténtalo de nuevo.",
                       code="LOCKED")
    try:
        yield
    finally:
        await col.update_one({"_id": key, "token": token}, {"$set": {"expiresAt": datetime(1970, 1, 1, tzinfo=timezone.utc)}})


# ── plazos de envío ───────────────────────────────────────────────────────────
async def assert_within_deadline(request_type: str, period: dict[str, Any] | None, now: datetime | None = None) -> None:
    now = now or utcnow()
    col = get_database()["request_deadlines"]
    dl = None
    if period:
        dl = await col.find_one({"requestType": request_type, "periodKey": period["key"], "isActive": True})
    dl = dl or await col.find_one({"requestType": request_type, "periodKey": None, "isActive": True})
    if not dl:
        return
    if now < _utc(dl["opensAt"]):
        raise AppError(400, f"El plazo para enviar esta solicitud abre el {fmt_local(dl['opensAt'])} (hora de Ecuador).",
                       code="DEADLINE_NOT_OPEN")
    if now > _utc(dl["closesAt"]):
        raise AppError(400, f"El plazo para enviar esta solicitud cerró el {fmt_local(dl['closesAt'])} (hora de Ecuador).",
                       code="DEADLINE_CLOSED")


# ── límites ───────────────────────────────────────────────────────────────────
async def count_in_window(student_dni: str, request_type: str, start: datetime, end: datetime) -> int:
    return await get_database()["requests"].count_documents({
        "studentDni": student_dni, "requestType": request_type,
        "status": {"$nin": COUNTED_STATUSES_EXCLUDED}, "createdAt": {"$gte": start, "$lt": end}})


async def exception_extra(student_user_id: ObjectId | None, request_type: str, window_key: str) -> int:
    if not student_user_id:
        return 0
    ex = await get_database()["limit_exceptions"].find_one({"studentUserId": student_user_id, "requestType": request_type,
                                                            "windowKey": window_key, "isActive": True})
    return int(ex["extra"]) if ex else 0


async def assert_within_limits(student_dni: str, student_user_id: ObjectId | None, request_type: str,
                               period: dict[str, Any] | None, now: datetime | None = None) -> None:
    now = now or utcnow()
    async for lim in get_database()["request_limits"].find({"requestType": request_type, "isActive": True}):
        win = window_for(lim["scope"], period, now)
        if not win:
            continue
        key, start, end = win
        allowed = int(lim["maxRequests"]) + await exception_extra(student_user_id, request_type, key)
        used = await count_in_window(student_dni, request_type, start, end)
        if used >= allowed:
            raise AppError(400, f"Alcanzaste el máximo de {allowed} solicitud(es) {SCOPE_LABELS[lim['scope']]} para este trámite.",
                           code="LIMIT_REACHED")


async def limit_preview(request_type: str, scope: str, max_requests: int) -> dict[str, Any]:
    """Impacto de un límite en la ventana vigente: cuántos estudiantes quedarían por encima, en el límite o debajo."""
    period = await current_period()
    win = window_for(scope, period, utcnow())
    if not win:
        return {"windowKey": None, "over": 0, "atLimit": 0, "below": 0, "students": 0,
                "note": "No hay un aporte activo; el límite por aporte no tiene efecto hoy."}
    key, start, end = win
    rows = await get_database()["requests"].aggregate([
        {"$match": {"requestType": request_type, "status": {"$nin": COUNTED_STATUSES_EXCLUDED},
                    "createdAt": {"$gte": start, "$lt": end}}},
        {"$group": {"_id": "$studentDni", "n": {"$sum": 1}}}]).to_list(None)
    over = sum(1 for r in rows if r["n"] > max_requests)
    at = sum(1 for r in rows if r["n"] == max_requests)
    return {"windowKey": key, "over": over, "atLimit": at, "below": len(rows) - over - at, "students": len(rows),
            "note": None}
