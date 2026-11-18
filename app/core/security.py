"""Contraseñas (bcrypt, costo 10) y JWT (HS256, 8 horas) compatibles con el original."""
from __future__ import annotations

import time

import bcrypt
import jwt

BCRYPT_ROUNDS = 10
JWT_ALGORITHM = "HS256"
JWT_EXPIRES_SECONDS = 8 * 60 * 60


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=BCRYPT_ROUNDS)).decode("ascii")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("ascii"))
    except (ValueError, TypeError):
        return False


def create_access_token(payload: dict, secret: str) -> str:
    now = int(time.time())
    return jwt.encode({**payload, "iat": now, "exp": now + JWT_EXPIRES_SECONDS}, secret, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str, secret: str) -> dict:
    """Lanza jwt.PyJWTError si la firma o la expiración no son válidas."""
    return jwt.decode(token, secret, algorithms=[JWT_ALGORITHM], options={"require": ["exp"]})


# ── Sesión por cookie (patrón PUM Web) ────────────────────────────────────────
import base64  # noqa: E402
import hashlib  # noqa: E402
import secrets  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402

SESSION_COOKIE = "uets_session"
CSRF_COOKIE = "uets_csrf"
CSRF_HEADER = "X-CSRF-Token"
BCRYPT_COST_ADMIN = 12  # altas y restablecimientos (como PUM)
TEMP_PASSWORD_TTL = timedelta(days=7)
_ALPHABET = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # sin caracteres ambiguos
_DUMMY_HASH = bcrypt.hashpw(b"uets-dummy-password-not-used", bcrypt.gensalt(10))


def hash_password_cost(password: str, cost: int) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=cost)).decode("ascii")


def burn_dummy(password: str) -> None:
    """Iguala el tiempo de respuesta cuando el usuario no existe (no revela si existe)."""
    bcrypt.checkpw(password.encode("utf-8"), _DUMMY_HASH)


def generate_temp_password() -> str:
    """12 caracteres `xxxx-xxxx-xxxx` con CSPRNG."""
    return "-".join("".join(secrets.choice(_ALPHABET) for _ in range(4)) for _ in range(3))


def issue_temp_password() -> tuple[str, dict]:
    temp = generate_temp_password()
    return temp, {
        "passwordHash": hash_password_cost(temp, BCRYPT_COST_ADMIN),
        "forcePasswordChange": True,
        "tempPasswordExpiresAt": datetime.now(timezone.utc) + TEMP_PASSWORD_TTL,
    }


def password_fingerprint(password_hash: str) -> str:
    """Huella corta del hash: si la contraseña cambia, las sesiones previas quedan inválidas."""
    digest = hashlib.sha256(password_hash.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")[:16]


def encode_session(claims: dict, secret: str, max_age: int) -> str:
    now = int(time.time())
    return jwt.encode({**claims, "iat": now, "exp": now + max_age, "typ": "session"}, secret, algorithm=JWT_ALGORITHM)


def decode_session(token: str, secret: str) -> dict | None:
    try:
        claims = jwt.decode(token, secret, algorithms=[JWT_ALGORITHM])
    except jwt.PyJWTError:
        return None
    return claims if claims.get("typ") == "session" else None


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def csrf_matches(cookie_value: str | None, header_value: str | None) -> bool:
    import hmac
    return bool(cookie_value) and bool(header_value) and hmac.compare_digest(cookie_value, header_value)
