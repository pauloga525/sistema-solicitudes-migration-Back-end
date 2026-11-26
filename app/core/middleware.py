"""Middleware: CSRF (doble envío), renovación/limpieza de la cookie de sesión y cabeceras de seguridad."""
from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.core.config import get_settings
from app.core.security import CSRF_COOKIE, CSRF_HEADER, SESSION_COOKIE, csrf_matches

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Cross-Origin-Opener-Policy": "same-origin",
}


def set_session_cookie(response: Response, token: str) -> None:
    s = get_settings()
    response.set_cookie(SESSION_COOKIE, token, max_age=s.session_max_age, httponly=True, secure=s.cookie_secure,
                        samesite=s.cookie_samesite, path="/")


def clear_session_cookie(response: Response) -> None:
    s = get_settings()
    response.delete_cookie(SESSION_COOKIE, path="/", secure=s.cookie_secure, httponly=True, samesite=s.cookie_samesite)


def set_csrf_cookie(response: Response, token: str) -> None:
    s = get_settings()
    # Patrón de doble envío. Mismo sitio: SameSite estricto y el frontend lee la cookie.
    # Frontend en otro dominio (COOKIE_SAMESITE=none): la cookie debe viajar entre sitios y el
    # frontend recibe el valor en `csrfToken` de GET /auth/session (no puede leer cookies ajenas).
    samesite = "none" if s.cookie_samesite == "none" else "strict"
    response.set_cookie(CSRF_COOKIE, token, httponly=False, secure=s.cookie_secure, samesite=samesite, path="/")


class SecurityMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.method not in SAFE_METHODS:
            if not csrf_matches(request.cookies.get(CSRF_COOKIE), request.headers.get(CSRF_HEADER)):
                return JSONResponse({"message": "Token de seguridad inválido. Recarga la página e inténtalo de nuevo.",
                                     "error": "Forbidden", "statusCode": 403, "code": "AUTH_CSRF"}, status_code=403)
        response = await call_next(request)
        if getattr(request.state, "clear_session", False):
            clear_session_cookie(response)
        elif token := getattr(request.state, "new_session_token", None):
            set_session_cookie(response, token)
        for k, v in SECURITY_HEADERS.items():
            response.headers.setdefault(k, v)
        if get_settings().cookie_secure:
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        if request.url.path.startswith(("/superadmin", "/me", "/auth")):
            response.headers.setdefault("Cache-Control", "no-store")
        return response
