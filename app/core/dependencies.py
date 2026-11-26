"""Dependencias compartidas: base de datos, sesión por cookie, autorización y lectura del body/query.

La sesión sigue el patrón de PUM Web: JWT firmado en cookie HttpOnly, expiración deslizante,
duración absoluta desde el login y revalidación periódica contra la base (usuario activo, rol y
huella de la contraseña). La autorización SIEMPRE se decide aquí, en el servidor.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from bson import ObjectId
from fastapi import Depends, Request
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.config import get_settings
from app.core.exceptions import AppError, BadRequest, Unauthorized
from app.core.security import SESSION_COOKIE, decode_session, encode_session, password_fingerprint
from app.database.connection import get_database

JSON_BODY_LIMIT_BYTES = 100 * 1024  # mismo límite por defecto que body-parser (Express)


@dataclass
class AuthUser:
    """Usuario de la sesión actual."""

    user_id: str
    role: str
    email: str | None = None
    username_: str | None = None
    name: str | None = None
    force_password_change: bool = False
    claims: dict[str, Any] = field(default_factory=dict)

    @property
    def username(self) -> str:
        """Nombre que se registra como actor (rejectedBy, auditoría): usuario, o correo si no tiene."""
        return self.username_ or self.email or self.user_id

    @property
    def roles(self) -> list[str]:
        """Rol principal más los adicionales (multirol), según la base en la última revalidación."""
        return list(self.claims.get("roles") or [self.role])

    @property
    def oid(self) -> ObjectId:
        return ObjectId(self.user_id)


def get_db() -> AsyncIOMotorDatabase:
    return get_database()


def client_ip(request: Request) -> str:
    if get_settings().trust_proxy:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def user_fingerprint(user: dict[str, Any]) -> str:
    """Huella de la credencial: cambia si cambia la contraseña. Las cuentas que solo usan Google no tienen contraseña."""
    return password_fingerprint(user.get("passwordHash") or f"sso:{user['_id']}")


def build_claims(user: dict[str, Any], login_at: float | None = None) -> dict[str, Any]:
    now = time.time()
    from app.modules.users.repository import effective_roles
    return {"sub": str(user["_id"]), "role": user["role"], "roles": effective_roles(user), "email": user.get("email"), "username": user.get("username"),
            "name": user.get("name"), "fpc": bool(user.get("forcePasswordChange")),
            "fp": user_fingerprint(user), "sv": int(user.get("sessionVersion") or 0), "loginAt": login_at or now, "checkedAt": now}


def _user_from_claims(c: dict[str, Any]) -> AuthUser:
    return AuthUser(c["sub"], c["role"], c.get("email"), c.get("username"), c.get("name"), bool(c.get("fpc")), c)


async def resolve_session(request: Request) -> AuthUser | None:
    if hasattr(request.state, "session_user"):
        return request.state.session_user
    s = get_settings()
    token = request.cookies.get(SESSION_COOKIE)
    claims = decode_session(token, s.jwt_secret) if token else None
    user: AuthUser | None = None
    if claims and claims.get("sub"):
        now = time.time()
        if not claims.get("loginAt") or now - claims["loginAt"] > s.session_absolute_max_s:
            request.state.clear_session = True  # duración absoluta superada
        elif (request.method in ("GET", "HEAD", "OPTIONS") and claims.get("role") == "STUDENT" and claims.get("checkedAt")
              and now - claims["checkedAt"] < s.session_recheck_s):
            # Estudiantes en lecturas: revalidación cada 60 s. Personal y escrituras: siempre contra la base
            # (un cambio de rol, baja o restablecimiento surte efecto en la petición siguiente).
            user = _user_from_claims(claims)
        else:
            # Revalidación contra la base; si la consulta falla, la excepción descarta la petición (falla cerrado).
            try:
                oid = ObjectId(claims["sub"])
            except Exception:  # noqa: BLE001
                oid = None
            db_user = await get_database()["users"].find_one({"_id": oid}) if oid else None
            if (not db_user or not db_user.get("isActive")
                    or user_fingerprint(db_user) != claims.get("fp")
                    or int(db_user.get("sessionVersion") or 0) != int(claims.get("sv") or 0)):
                request.state.clear_session = True
            else:
                new_claims = build_claims(db_user, login_at=claims["loginAt"])
                request.state.new_session_token = encode_session(new_claims, s.jwt_secret, s.session_max_age)
                user = _user_from_claims(new_claims)
    request.state.session_user = user
    return user


def try_authenticate(request: Request) -> AuthUser | None:
    """Versión síncrona para código que ya resolvió la sesión (la consulta la hace `resolve_session`)."""
    return getattr(request.state, "session_user", None)


async def optional_user(request: Request) -> AuthUser | None:
    return await resolve_session(request)


async def current_user(request: Request) -> AuthUser:
    user = await resolve_session(request)
    if user is None:
        raise Unauthorized()
    return user


async def _check_restrictions(user: AuthUser, allow_password_change: bool) -> None:
    from app.modules.system.service import get_maintenance

    if user.force_password_change and not allow_password_change:
        raise AppError(403, "Debes cambiar tu contraseña antes de continuar", code="PASSWORD_CHANGE_REQUIRED")
    if user.role == "STUDENT" and (await get_maintenance())["enabled"]:
        raise AppError(503, "El sistema está en mantenimiento. Intenta más tarde.", code="MAINTENANCE")


async def active_user(user: AuthUser = Depends(current_user)) -> AuthUser:
    await _check_restrictions(user, False)
    return user


async def active_user_allow_password_change(user: AuthUser = Depends(current_user)) -> AuthUser:
    await _check_restrictions(user, True)
    return user


def require_role(*roles: str):
    """Rol exacto verificado en el servidor (el frontend solo oculta opciones)."""

    async def dep(request: Request, user: AuthUser = Depends(active_user)) -> AuthUser:
        if not set(user.roles) & set(roles):  # rol principal o adicional (multirol)
            raise AppError(403, "No autorizado", code="AUTH_FORBIDDEN")
        request.state.user = user
        return user

    return dep


require_superadmin = require_role("SUPERADMIN")
require_student = require_role("STUDENT")
require_teacher = require_role("TEACHER")


async def require_admin(request: Request) -> AuthUser:
    """Panel administrativo: ADMIN o SUPERADMIN. Sin sesión → 401 como el original."""
    user = await current_user(request)
    await _check_restrictions(user, False)
    if not set(user.roles) & {"ADMIN", "SUPERADMIN"}:
        raise AppError(403, "No autorizado", code="AUTH_FORBIDDEN")
    request.state.user = user
    return user


def require_not_self(actor_id: str, target_id: str) -> None:
    if actor_id == target_id:
        raise AppError(403, "No puedes modificar tus propios privilegios ni tu propio estado", code="AUTH_FORBIDDEN")


async def json_body(request: Request) -> Any:
    """Lee el body JSON como body-parser de Express (sin JSON → `{}`; malformado → 400; >100 KB → 413)."""
    content_type = request.headers.get("content-type", "")
    raw = await request.body()
    if len(raw) > JSON_BODY_LIMIT_BYTES:
        raise AppError(413, "request entity too large")
    if "json" not in content_type.lower() or not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BadRequest(f"Unexpected token in JSON at position {getattr(exc, 'pos', 0)}") from exc


def query_dict(request: Request) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in request.query_params.multi_items():
        if key in out:
            prev = out[key]
            out[key] = prev + [value] if isinstance(prev, list) else [prev, value]
        else:
            out[key] = value
    return out
