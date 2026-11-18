"""Servicios de sistema: límites de uso persistentes, modo mantenimiento, errores, configuración y retención."""
from __future__ import annotations

import logging
import re
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from bson import ObjectId

from app.core.exceptions import AppError, BadRequest, NotFound
from app.database.connection import get_database
from app.shared.utilities.dates import utcnow

log = logging.getLogger("app.system")

# ── Límites de uso (ventana fija, atómicos, en MongoDB; fallan abiertos si la base no responde) ──
RATE_LIMITS: dict[str, tuple[int, int]] = {
    "LOGIN_EMAIL_IP": (10, 15 * 60),
    "LOGIN_IP": (50, 15 * 60),
    "LOGIN_EMAIL": (50, 15 * 60),
    "REGISTER_IP": (10, 60 * 60),
    "SA_CREATE_USER": (20, 60 * 60),
    "SA_RESET_PASS": (5, 60 * 60),
    "CHANGE_PASSWORD": (5, 15 * 60),
    "EXPORT": (10, 5 * 60),
    "SENSITIVE_VIEW": (120, 60 * 60),
}


@dataclass
class RateLimitResult:
    allowed: bool
    remaining: int
    reset_at: datetime

    @property
    def retry_after(self) -> int:
        return max(0, int((self.reset_at - utcnow()).total_seconds()) + 1)


async def rate_limit_check(key: str, limit: int, window_s: int) -> RateLimitResult:
    now = utcnow()
    cutoff = now - timedelta(seconds=window_s)
    expired = {"$or": [{"$eq": [{"$ifNull": ["$windowStart", None]}, None]}, {"$lt": ["$windowStart", cutoff]}]}
    try:
        doc = await get_database()["rate_limits"].find_one_and_update(
            {"_id": key},
            [{"$set": {"count": {"$cond": [expired, 1, {"$add": ["$count", 1]}]},
                       "windowStart": {"$cond": [expired, now, "$windowStart"]}}}],
            upsert=True, return_document=True,
        )
        ws = doc["windowStart"] if doc["windowStart"].tzinfo else doc["windowStart"].replace(tzinfo=timezone.utc)
        count = int(doc["count"])
        return RateLimitResult(count <= limit, max(0, limit - count), ws + timedelta(seconds=window_s))
    except Exception as exc:  # noqa: BLE001 — fail-open
        log.warning("Límite de uso no disponible (fail-open) %s: %s", key, exc)
        return RateLimitResult(True, 1, now + timedelta(seconds=window_s))


async def rate_limit_named(name: str, key: str) -> RateLimitResult:
    limit, window = RATE_LIMITS[name]
    return await rate_limit_check(f"{name}:{key}", limit, window)


async def enforce_rate_limit(name: str, key: str, message: str) -> None:
    rl = await rate_limit_named(name, key)
    if not rl.allowed:
        raise AppError(429, message, headers={"Retry-After": str(rl.retry_after)})


# ── Configuración (documento único) y modo mantenimiento ──────────────────────
SETTINGS_ID = "singleton"
_CACHE_TTL = 5.0
_cache: tuple[float, dict[str, Any]] | None = None

DEFAULT_SETTINGS = {"maintenanceEnabled": False, "maintenanceMessage": None, "maintenanceEnabledAt": None,
                    "maintenanceEnabledBy": None, "activeAcademicYearId": None, "dueDaysDefault": None,
                    "submissionStart": None, "submissionEnd": None}


async def get_settings_doc() -> dict[str, Any]:
    global _cache
    if _cache and _cache[0] > time.monotonic():
        return _cache[1]
    row = await get_database()["system_settings"].find_one({"_id": SETTINGS_ID}) or {}
    value = {**DEFAULT_SETTINGS, **{k: v for k, v in row.items() if k != "_id"}}
    _cache = (time.monotonic() + _CACHE_TTL, value)
    return value


def reset_settings_cache() -> None:
    global _cache
    _cache = None


async def update_settings(fields: dict[str, Any]) -> dict[str, Any]:
    await get_database()["system_settings"].update_one({"_id": SETTINGS_ID}, {"$set": {**fields, "updatedAt": utcnow()}},
                                                        upsert=True)
    reset_settings_cache()
    return await get_settings_doc()


async def get_maintenance() -> dict[str, Any]:
    s = await get_settings_doc()
    return {"enabled": bool(s["maintenanceEnabled"]), "message": s.get("maintenanceMessage"),
            "enabledAt": s.get("maintenanceEnabledAt"), "enabledBy": s.get("maintenanceEnabledBy")}


async def set_maintenance(enabled: bool, message: str | None, actor_id: str) -> dict[str, Any]:
    await update_settings({"maintenanceEnabled": enabled, "maintenanceMessage": message,
                           "maintenanceEnabledAt": utcnow() if enabled else None, "maintenanceEnabledBy": actor_id})
    return await get_maintenance()


# ── Registro de errores (la traza nunca sale al navegador) ────────────────────
ERRORS_PAGE = 25


async def log_error(level: str, source: str, message: str, *, exc: BaseException | None = None,
                    path: str | None = None, method: str | None = None, user_id: str | None = None,
                    metadata: dict[str, Any] | None = None) -> None:
    try:
        stack = "".join(traceback.format_exception(exc))[-20000:] if exc else None
        await get_database()["system_errors"].insert_one({
            "level": level, "source": source, "message": message[:5000], "stack": stack, "path": path, "method": method,
            "userId": user_id, "metadata": metadata, "resolved": False, "resolvedAt": None, "resolvedBy": None,
            "resolveNote": None, "createdAt": utcnow()})
    except Exception:  # noqa: BLE001 — el monitoreo debe ser transparente
        log.exception("No se pudo guardar el error en system_errors")


def date_range(from_: str | None, to: str | None) -> dict[str, Any] | None:
    if not from_ and not to:
        return None
    rng: dict[str, Any] = {}
    try:
        if from_:
            rng["$gte"] = datetime.fromisoformat(from_).replace(tzinfo=timezone.utc)
        if to:
            rng["$lte"] = datetime.fromisoformat(to).replace(tzinfo=timezone.utc) + timedelta(days=1) - timedelta(milliseconds=1)
    except ValueError as exc:
        raise BadRequest("Fecha inválida. Use el formato AAAA-MM-DD.") from exc
    return rng


def _error_out(e: dict[str, Any], detail: bool = False) -> dict[str, Any]:
    out = {"id": str(e["_id"]), "createdAt": e["createdAt"], "level": e["level"], "source": e["source"],
           "message": e["message"], "path": e.get("path"), "resolved": e.get("resolved", False),
           "resolvedAt": e.get("resolvedAt")}
    if detail:
        out.update({"stack": e.get("stack"), "method": e.get("method"), "userId": e.get("userId"),
                    "metadata": e.get("metadata"), "resolvedBy": e.get("resolvedBy"), "resolveNote": e.get("resolveNote")})
    return out


async def list_errors(level: str | None, resolved: str | None, source: str | None, from_: str | None, to: str | None,
                      page: int) -> dict[str, Any]:
    q: dict[str, Any] = {}
    if level:
        q["level"] = level
    if source:
        q["source"] = {"$regex": re.escape(source.strip()), "$options": "i"}
    if resolved in ("true", "false"):
        q["resolved"] = resolved == "true"
    rng = date_range(from_, to)
    if rng:
        q["createdAt"] = rng
    col = get_database()["system_errors"]
    total = await col.count_documents(q)
    rows = await col.find(q, {"stack": 0, "metadata": 0}).sort("createdAt", -1).skip((page - 1) * ERRORS_PAGE).limit(ERRORS_PAGE).to_list(ERRORS_PAGE)
    return {"rows": [_error_out(e) for e in rows], "total": total, "page": page, "totalPages": max(1, -(-total // ERRORS_PAGE))}


async def error_stats() -> dict[str, int]:
    col = get_database()["system_errors"]
    return {"total": await col.count_documents({}), "unresolved": await col.count_documents({"resolved": False}),
            "errors": await col.count_documents({"level": "ERROR", "resolved": False}),
            "warnings": await col.count_documents({"level": "WARN", "resolved": False})}


async def error_sources() -> list[str]:
    return sorted(await get_database()["system_errors"].distinct("source"))


async def get_error(oid: ObjectId) -> dict[str, Any]:
    e = await get_database()["system_errors"].find_one({"_id": oid})
    if not e:
        raise NotFound("Error no encontrado")
    return _error_out(e, detail=True)


async def resolve_error(oid: ObjectId, actor_id: str, note: str | None) -> None:
    res = await get_database()["system_errors"].update_one(
        {"_id": oid, "resolved": False},
        {"$set": {"resolved": True, "resolvedAt": utcnow(), "resolvedBy": actor_id, "resolveNote": (note or "").strip() or None}})
    if res.matched_count == 0:
        await get_error(oid)
        raise BadRequest("El error ya estaba resuelto")


async def reopen_error(oid: ObjectId) -> None:
    res = await get_database()["system_errors"].update_one(
        {"_id": oid}, {"$set": {"resolved": False, "resolvedAt": None, "resolvedBy": None, "resolveNote": None}})
    if res.matched_count == 0:
        raise NotFound("Error no encontrado")
