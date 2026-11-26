"""Autenticación por cookie: sesión, ingreso, registro de estudiantes, salida y cambio de contraseña."""
from __future__ import annotations

import logging
import re

from fastapi import APIRouter, Depends, Request, Response
from pymongo.errors import DuplicateKeyError

from app.core.config import get_settings
from app.core.dependencies import (AuthUser, active_user_allow_password_change, build_claims, client_ip, json_body,
                                   optional_user)
from app.core.exceptions import AppError, BadRequest, Conflict, Unauthorized
from app.core.middleware import clear_session_cookie, set_csrf_cookie, set_session_cookie
from app.core.rate_limit import Throttle
from app.core.security import (CSRF_COOKIE, burn_dummy, encode_session, hash_password_cost, new_csrf_token,
                               verify_password)
from app.modules.audit.service import log_user_event
from app.modules.auth.google import GoogleTokenError, verify_google_id_token
from app.modules.auth.schemas import LOGIN_SCHEMA, ChangePasswordIn, GoogleLoginIn, LoginDTO, RegisterIn
from app.modules.system.service import enforce_rate_limit, get_maintenance, rate_limit_named
from app.modules.users import repository as users
from app.shared.utilities.dates import utcnow
from app.shared.validators.class_validator import validate_dto

router = APIRouter(prefix="/auth", tags=["auth"])
log = logging.getLogger("app.auth")

INVALID_CREDENTIALS = "Usuario o contraseña incorrectos"
GOOGLE_ONLY = "El ingreso es solo con tu cuenta institucional de Google."
STAFF_MESSAGE = ("Tu correo corresponde a personal de la institución (docente o administrativo). Este sistema es para que "
                 "los estudiantes envíen sus solicitudes, por eso tu cuenta no tiene acceso todavía. Si necesitas revisar "
                 "las solicitudes como administrador, comunícate con el Departamento de Sistemas: ya registramos tu "
                 "correo para que puedan habilitarte.")
MIN_PASSWORD, MAX_PASSWORD = 10, 128
BCRYPT_COST_CHANGE = 10


def ensure_csrf(request: Request, response: Response) -> str:
    """Devuelve el token CSRF vigente; si el navegador no lo tiene, emite uno nuevo."""
    token = request.cookies.get(CSRF_COOKIE)
    if not token:
        token = new_csrf_token()
        set_csrf_cookie(response, token)
    return token


def user_out(u: dict | AuthUser | None) -> dict | None:
    if u is None:
        return None
    if isinstance(u, AuthUser):
        return {"id": u.user_id, "role": u.role, "email": u.email, "username": u.username_, "name": u.name,
                "forcePasswordChange": u.force_password_change, "home": users.ROLE_HOME[u.role]}
    return {"id": str(u["_id"]), "role": u["role"], "email": u.get("email"), "username": u.get("username"),
            "name": u.get("name"), "forcePasswordChange": bool(u.get("forcePasswordChange")),
            "home": users.ROLE_HOME[u["role"]]}


def _start_session(response: Response, request: Request, user: dict) -> None:
    s = get_settings()
    set_session_cookie(response, encode_session(build_claims(user), s.jwt_secret, s.session_max_age))
    ensure_csrf(request, response)


def validate_new_password(new: str, confirm: str, email: str | None = None, cedula: str | None = None) -> None:
    if len(new) < MIN_PASSWORD:
        raise BadRequest(f"La contraseña debe tener al menos {MIN_PASSWORD} caracteres")
    if len(new) > MAX_PASSWORD:
        raise BadRequest(f"La contraseña no puede superar {MAX_PASSWORD} caracteres")
    if new != confirm:
        raise BadRequest("Las contraseñas no coinciden")
    if email and new.lower() == email.lower():
        raise BadRequest("La contraseña no puede ser tu correo")
    if cedula and cedula in new:
        raise BadRequest("La contraseña no puede contener tu número de cédula")


@router.get("/session")
async def session(request: Request, response: Response, user: AuthUser | None = Depends(optional_user)):
    csrf = ensure_csrf(request, response)
    m = await get_maintenance()
    out = user_out(user)
    if out and user and user.role == "STUDENT":
        from app.database.connection import get_database
        out["profileCompleted"] = bool(await get_database()["student_profiles"].find_one({"userId": user.oid, "completed": True}, {"_id": 1}))
    s = get_settings()
    return {"user": out, "maintenance": {"enabled": m["enabled"], "message": m["message"]},
            "emailDomain": s.institutional_email_domain, "googleClientId": s.google_client_id,
            "passwordRegistration": s.allow_password_registration and s.allow_password_login,
            "passwordLogin": s.allow_password_login, "studentEmailSuffix": s.student_email_suffix,
            # Para frontends en otro dominio, que no pueden leer la cookie uets_csrf (CORS impide que
            # un origen no autorizado lea esta respuesta).
            "csrfToken": csrf}


@router.post("/login", status_code=201, dependencies=[Depends(Throttle("AuthController.login", 5))])
async def login(request: Request, response: Response, body=Depends(json_body)):
    if not get_settings().allow_password_login:
        raise AppError(403, GOOGLE_ONLY, code="PASSWORD_LOGIN_DISABLED")
    dto: LoginDTO = validate_dto(LOGIN_SCHEMA, body, LoginDTO)  # type: ignore[assignment]
    ident, ip = dto.username.strip(), client_ip(request)
    key = ident.lower()
    checks = [await rate_limit_named("LOGIN_EMAIL_IP", f"{key}:{ip}"), await rate_limit_named("LOGIN_IP", ip),
              await rate_limit_named("LOGIN_EMAIL", key)]
    blocked = next((c for c in checks if not c.allowed), None)
    if blocked:
        await log_user_event("LOGIN_FAILED", result="FAILURE", actor_email=ident, actor_ip=ip, metadata={"reason": "rate_limited"})
        raise AppError(429, "Demasiados intentos. Espera unos minutos e inténtalo de nuevo.",
                       headers={"Retry-After": str(blocked.retry_after)})

    user = await users.find_by_login(ident)
    if not user or not user.get("passwordHash"):
        burn_dummy(dto.password)  # mismo tiempo de respuesta: no revela si la cuenta existe
        await log_user_event("LOGIN_FAILED", result="FAILURE", actor_email=ident, actor_ip=ip, metadata={"reason": "user_not_found"})
        raise Unauthorized(INVALID_CREDENTIALS)
    base = {"actor_id": str(user["_id"]), "actor_email": user.get("email") or user.get("username"),
            "actor_role": user["role"], "actor_ip": ip}
    # La contraseña se verifica antes que isActive: el tiempo no distingue cuentas desactivadas.
    if not verify_password(dto.password, user["passwordHash"]):
        await log_user_event("LOGIN_FAILED", result="FAILURE", metadata={"reason": "wrong_password"}, **base)
        raise Unauthorized(INVALID_CREDENTIALS)
    if not user.get("isActive", True):
        await log_user_event("LOGIN_FAILED", result="FAILURE", metadata={"reason": "user_inactive"}, **base)
        raise Unauthorized(INVALID_CREDENTIALS)
    exp = user.get("tempPasswordExpiresAt")
    if user.get("forcePasswordChange") and exp and exp < utcnow():
        await log_user_event("LOGIN_FAILED", result="FAILURE", metadata={"reason": "temp_password_expired"}, **base)
        raise Unauthorized("Tu contraseña temporal venció. Solicita una nueva al administrador.")

    await users.set_fields(user["_id"], {"lastLoginAt": utcnow()})
    await log_user_event("LOGIN_SUCCESS", **base)
    _start_session(response, request, user)
    out = user_out(user)
    redirect = "/cambiar-contrasena" if user.get("forcePasswordChange") else out["home"]
    response.status_code = 201
    return {"user": out, "redirectTo": redirect}


@router.post("/google", dependencies=[Depends(Throttle("AuthController.google", 10))])
async def google_login(body: GoogleLoginIn, request: Request, response: Response):
    """Ingreso con Google: solo cuentas verificadas del dominio institucional.

    - Si el correo ya tiene cuenta (estudiante, ADMIN o SUPERADMIN) se ingresa con ella.
    - Si no existe y es de estudiante (nombreapellido.est@dominio), se crea una cuenta de ESTUDIANTE
      (sin contraseña) que luego completa sus datos.
    - Si no existe y no es de estudiante (docente o administrativo), no se crea cuenta: el correo queda
      registrado en `access_requests` para que el SUPERADMIN le dé acceso de administrador si corresponde.
    """
    s = get_settings()
    ip = client_ip(request)
    if not s.google_client_id:
        raise AppError(404, "El ingreso con Google no está configurado")
    await enforce_rate_limit("LOGIN_IP", ip, "Demasiados intentos. Espera unos minutos e inténtalo de nuevo.")
    try:
        info = verify_google_id_token(body.credential, s.google_client_id, s.institutional_email_domain,
                                      key_resolver=getattr(request.app.state, "google_key_resolver", None))
    except GoogleTokenError as exc:
        await log_user_event("LOGIN_FAILED", result="FAILURE", actor_ip=ip, metadata={"reason": "google_token", "method": "google"})
        raise AppError(401, str(exc), code="GOOGLE_REJECTED") from exc
    email = info["email"]
    user = await users.find_by_login(email)
    created = False
    if user is None and not s.is_student_email(email):
        await _record_access_request(info, ip)
        raise AppError(403, STAFF_MESSAGE, code="STAFF_NOT_ENABLED")
    if user is None:
        if (await get_maintenance())["enabled"]:
            raise AppError(503, "El sistema está en mantenimiento. Intenta más tarde.", code="MAINTENANCE")
        try:
            user = await users.insert({"role": "STUDENT", "email": email, "username": None, "name": info.get("name"),
                                       "passwordHash": None, "googleSub": info["sub"]})
            created = True
        except DuplicateKeyError:
            user = await users.find_by_login(email)
    base = {"actor_id": str(user["_id"]), "actor_email": email, "actor_role": user["role"], "actor_ip": ip}
    if user.get("googleSub") and user["googleSub"] != info["sub"]:
        await log_user_event("LOGIN_FAILED", result="FAILURE", metadata={"reason": "google_sub_mismatch", "method": "google"}, **base)
        raise AppError(401, "Esta cuenta está vinculada a otra identidad de Google. Pide ayuda a la institución.", code="GOOGLE_REJECTED")
    if not user.get("isActive", True):
        await log_user_event("LOGIN_FAILED", result="FAILURE", metadata={"reason": "user_inactive", "method": "google"}, **base)
        raise AppError(401, "Tu cuenta está desactivada. Pide ayuda a la institución.", code="GOOGLE_REJECTED")
    fields: dict = {"lastLoginAt": utcnow(), "googleSub": info["sub"]}
    if user.get("forcePasswordChange"):
        # Google ya probó la identidad: la contraseña temporal pendiente deja de servir y no se exige cambiarla.
        fields.update({"forcePasswordChange": False, "tempPasswordExpiresAt": None, "passwordHash": None})
    if not user.get("name") and info.get("name"):
        fields["name"] = info["name"]
    await users.set_fields(user["_id"], fields)
    user = await users.get(user["_id"])
    if created:
        await log_user_event("STUDENT_REGISTERED", metadata={"method": "google"}, **base)
    await log_user_event("LOGIN_SUCCESS", metadata={"method": "google"}, **base)
    _start_session(response, request, user)
    out = user_out(user)
    if user["role"] == "STUDENT":
        from app.database.connection import get_database
        done = bool(await get_database()["student_profiles"].find_one({"userId": user["_id"], "completed": True}, {"_id": 1}))
        out["profileCompleted"] = done
        redirect = out["home"] if done else "/estudiante/perfil"
    else:
        redirect = out["home"]
    return {"user": out, "redirectTo": redirect}


async def _record_access_request(info: dict, ip: str | None) -> None:
    """Guarda (o actualiza) el intento de un colaborador. No crea cuenta ni da permisos."""
    from app.database.connection import get_database
    now = utcnow()
    email = info["email"]
    await get_database()["access_requests"].update_one(
        {"email": email},
        {"$set": {"name": info.get("name"), "googleSub": info.get("sub"), "lastSeenAt": now, "lastIp": ip},
         "$setOnInsert": {"email": email, "status": "pending", "firstSeenAt": now},
         "$inc": {"attempts": 1}},
        upsert=True)
    await log_user_event("LOGIN_FAILED", result="FAILURE", actor_email=email, actor_ip=ip,
                         metadata={"reason": "staff_not_enabled", "method": "google"})


@router.post("/register", status_code=201)
async def register(body: RegisterIn, request: Request, response: Response):
    s = get_settings()
    if not (s.allow_password_registration and s.allow_password_login):
        raise AppError(403, "El registro es solo con tu cuenta institucional de Google.", code="PASSWORD_REGISTRATION_DISABLED")
    if (await get_maintenance())["enabled"]:
        raise AppError(503, "El sistema está en mantenimiento. Intenta más tarde.", code="MAINTENANCE")
    ip = client_ip(request)
    await enforce_rate_limit("REGISTER_IP", ip, "Demasiados registros desde esta conexión. Intenta más tarde.")
    email = users.normalize_email(body.email)
    domain = s.institutional_email_domain
    if not re.fullmatch(r"[a-z0-9._%+-]+@" + re.escape(domain), email):
        raise BadRequest(f"Usa tu correo institucional (@{domain}).")
    if not s.is_student_email(email):
        raise AppError(403, STAFF_MESSAGE, code="STAFF_NOT_ENABLED")
    validate_new_password(body.password.strip(), body.confirmPassword.strip(), email)
    try:
        user = await users.insert({"role": "STUDENT", "email": email, "username": None, "name": None,
                                   "passwordHash": hash_password_cost(body.password.strip(), BCRYPT_COST_CHANGE)})
    except DuplicateKeyError as exc:
        raise Conflict("Ya existe una cuenta con ese correo. Inicia sesión o pide ayuda a la institución.") from exc
    await log_user_event("STUDENT_REGISTERED", actor_id=str(user["_id"]), actor_email=email, actor_role="STUDENT", actor_ip=ip)
    _start_session(response, request, user)
    response.status_code = 201
    return {"user": {**user_out(user), "profileCompleted": False}, "redirectTo": "/estudiante/perfil"}


@router.post("/logout")
async def logout(request: Request, response: Response, user: AuthUser | None = Depends(optional_user)):
    if user:
        await log_user_event("LOGOUT", actor_id=user.user_id, actor_email=user.email or user.username,
                             actor_role=user.role, actor_ip=client_ip(request))
    request.state.clear_session = True
    clear_session_cookie(response)
    return {"ok": True}


@router.post("/change-password")
async def change_password(body: ChangePasswordIn, request: Request, response: Response,
                          session: AuthUser = Depends(active_user_allow_password_change)):
    new, confirm = body.newPassword.strip(), body.confirmPassword.strip()
    user = await users.get(session.oid)
    if not user:
        raise Unauthorized()
    if not user.get("forcePasswordChange") and user.get("passwordHash"):
        await enforce_rate_limit("CHANGE_PASSWORD", session.user_id, "Demasiados intentos. Espera unos minutos.")
        if not body.currentPassword or not verify_password(body.currentPassword, user["passwordHash"]):
            raise BadRequest("La contraseña actual no es correcta")
    validate_new_password(new, confirm, user.get("email"))
    if user.get("passwordHash") and verify_password(new, user["passwordHash"]):
        raise BadRequest("La nueva contraseña debe ser distinta de la actual")
    await users.set_fields(user["_id"], {"passwordHash": hash_password_cost(new, BCRYPT_COST_CHANGE),
                                         "forcePasswordChange": False, "tempPasswordExpiresAt": None})
    await log_user_event("PASSWORD_CHANGED", actor_id=session.user_id, actor_email=session.email or session.username,
                         actor_role=session.role, actor_ip=client_ip(request))
    # La huella cambió: todas las sesiones abiertas quedan inválidas; se pide ingresar de nuevo.
    request.state.clear_session = True
    clear_session_cookie(response)
    return {"redirectTo": "/ingresar"}
