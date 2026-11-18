"""Verificación del ID token de "Iniciar sesión con Google" (Google Identity Services).

Se valida en el servidor, sin confiar en el navegador:
- firma RS256 con las claves públicas de Google (JWKS, en caché),
- `aud` = GOOGLE_CLIENT_ID, `iss` = accounts.google.com, vigencia (`exp`/`iat`),
- `email_verified` verdadero y cuenta del dominio institucional: `hd` y el correo deben ser @<dominio>.
Una cuenta personal de Gmail no pasa aunque tenga el mismo nombre.
"""
from __future__ import annotations

from typing import Any, Callable

import jwt

GOOGLE_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
GOOGLE_ISSUERS = ("accounts.google.com", "https://accounts.google.com")
_jwks_client: jwt.PyJWKClient | None = None


class GoogleTokenError(Exception):
    """El token no es válido o no pertenece al dominio institucional (mensaje apto para el usuario)."""


def _google_key(token: str) -> Any:
    global _jwks_client
    if _jwks_client is None:
        _jwks_client = jwt.PyJWKClient(GOOGLE_JWKS_URL, cache_keys=True, lifespan=6 * 3600, timeout=10)
    return _jwks_client.get_signing_key_from_jwt(token).key


def verify_google_id_token(token: str, client_id: str, domain: str,
                           key_resolver: Callable[[str], Any] | None = None) -> dict[str, Any]:
    if not token or len(token) > 4096:
        raise GoogleTokenError("Token de Google inválido")
    try:
        key = (key_resolver or _google_key)(token)
        claims = jwt.decode(token, key, algorithms=["RS256"], audience=client_id, issuer=GOOGLE_ISSUERS,
                            options={"require": ["exp", "iat", "aud", "iss", "sub", "email"]}, leeway=30)
    except jwt.ExpiredSignatureError as exc:
        raise GoogleTokenError("La sesión de Google expiró. Vuelve a intentarlo.") from exc
    except (jwt.PyJWTError, ValueError) as exc:
        raise GoogleTokenError("No pudimos verificar tu cuenta de Google. Vuelve a intentarlo.") from exc
    email = str(claims.get("email", "")).strip().lower()
    if claims.get("email_verified") not in (True, "true"):
        raise GoogleTokenError("Tu correo de Google no está verificado.")
    if claims.get("hd") != domain or not email.endswith("@" + domain):
        raise GoogleTokenError(f"Ingresa con tu cuenta institucional (@{domain}). Las cuentas personales de Gmail no tienen acceso.")
    return {"email": email, "sub": str(claims["sub"]), "name": claims.get("name")}
