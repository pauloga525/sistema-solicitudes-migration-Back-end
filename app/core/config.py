"""Configuración de la aplicación a partir de variables de entorno.

Equivalente a `ConfigModule.forRoot()` + `src/config/env.validation.ts` del
backend NestJS original. Las variables de seguridad son obligatorias: si faltan
o son inválidas el proceso termina con un mensaje claro, antes de aceptar
peticiones (mismo texto que el original).
"""
from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parents[2]

MIN_JWT_SECRET_LENGTH = 32
PADRON_ENCRYPTION_KEY_HEX_LENGTH = 64
DEFAULT_CORS_ORIGINS = ["http://localhost:5173", "http://localhost:5174"]


@dataclass(frozen=True)
class Settings:
    port: int
    mongodb_uri: str
    jwt_secret: str
    padron_encryption_key: str
    admin_seed_username: str
    admin_seed_password: str | None
    cors_origins: list[str] = field(default_factory=list)
    node_env: str = ""
    trust_proxy: bool = False
    assets_dir: Path = BACKEND_DIR / "assets"
    # ── Sesión (patrón PUM Web) ──
    session_max_age: int = 8 * 60 * 60          # expiración deslizante
    session_absolute_max_s: int = 12 * 60 * 60  # duración absoluta desde el login
    session_recheck_s: int = 60                 # revalidación contra la base
    cookie_secure: bool = False
    # ── Estudiantes / SUPERADMIN ──
    institutional_email_domain: str = "uets.edu.ec"
    superadmin_seed_email: str = "superadmin@uets.edu.ec"
    superadmin_seed_password: str | None = None
    timezone: str = "America/Guayaquil"
    # ── Google (correo institucional) ──
    google_client_id: str | None = None
    allow_password_registration: bool = True
    # Ingreso con usuario y contraseña. Apagado por defecto: el ingreso es solo con Google.
    # Encenderlo (ALLOW_PASSWORD_LOGIN=true) sirve para cuentas de prueba o de emergencia.
    allow_password_login: bool = False
    # Los correos de estudiante terminan en "<sufijo>@dominio" (nombreapellido.est@uets.edu.ec).
    student_email_suffix: str = ".est"
    # ── Retención ──
    user_audit_retention_days: int = 730
    error_log_retention_days: int = 180
    rate_limit_retention_s: int = 24 * 60 * 60

    def is_student_email(self, email: str | None) -> bool:
        """nombreapellido.est@uets.edu.ec → estudiante; sin ".est" → personal de la institución."""
        local, _, domain = (email or "").strip().lower().partition("@")
        return domain == self.institutional_email_domain and local.endswith(self.student_email_suffix) \
            and len(local) > len(self.student_email_suffix)

    @property
    def is_production(self) -> bool:
        return self.node_env == "production"

    @property
    def database_name(self) -> str:
        # Mismo criterio que Mongoose: el nombre de la base es la ruta de la URI.
        match = re.search(r"/([^/?]+)(\?.*)?$", self.mongodb_uri.split("@")[-1])
        return match.group(1) if match else "test"


def load_env_file() -> None:
    """Carga backend/.env si existe (no sobrescribe variables ya definidas)."""
    load_dotenv(BACKEND_DIR / ".env", override=False)


def collect_env_errors(env: dict[str, str] | os._Environ) -> list[str]:
    errors: list[str] = []
    jwt_secret = env.get("JWT_SECRET")
    if not jwt_secret:
        errors.append("JWT_SECRET no está definida.")
    elif len(jwt_secret) < MIN_JWT_SECRET_LENGTH:
        errors.append(
            f"JWT_SECRET debe tener al menos {MIN_JWT_SECRET_LENGTH} caracteres (tiene {len(jwt_secret)})."
        )

    padron_key = env.get("PADRON_ENCRYPTION_KEY")
    if not padron_key:
        errors.append("PADRON_ENCRYPTION_KEY no está definida.")
    elif len(padron_key) != PADRON_ENCRYPTION_KEY_HEX_LENGTH or not re.fullmatch(r"[0-9a-fA-F]+", padron_key):
        errors.append(
            f"PADRON_ENCRYPTION_KEY debe ser una cadena hexadecimal de {PADRON_ENCRYPTION_KEY_HEX_LENGTH} caracteres (32 bytes)."
        )
    return errors


def validate_env() -> None:
    """Falla el arranque con un mensaje claro si falta configuración obligatoria."""
    errors = collect_env_errors(os.environ)
    if errors:
        print(
            "❌ Configuración inválida, no se puede iniciar el servidor:\n"
            + "\n".join(f"   - {e}" for e in errors)
            + "\n   Defina las variables requeridas en backend/.env (ver .env.example).",
            file=sys.stderr,
        )
        sys.exit(1)


@lru_cache
def get_settings() -> Settings:
    origins = [o.strip() for o in os.environ.get("CORS_ORIGINS", "").split(",") if o.strip()]
    return Settings(
        port=int(os.environ.get("PORT") or 3000),
        mongodb_uri=os.environ.get("MONGODB_URI") or "mongodb://localhost:27017/sistema_solicitudes",
        jwt_secret=os.environ["JWT_SECRET"],
        padron_encryption_key=os.environ["PADRON_ENCRYPTION_KEY"],
        admin_seed_username=os.environ.get("ADMIN_SEED_USERNAME") or "admin",
        admin_seed_password=os.environ.get("ADMIN_SEED_PASSWORD") or None,
        cors_origins=origins or list(DEFAULT_CORS_ORIGINS),
        node_env=os.environ.get("NODE_ENV", ""),
        trust_proxy=os.environ.get("TRUST_PROXY", "").lower() in ("1", "true", "yes"),
        cookie_secure=(os.environ.get("COOKIE_SECURE") or ("true" if os.environ.get("NODE_ENV") == "production" else "false")).lower() == "true",
        institutional_email_domain=(os.environ.get("INSTITUTIONAL_EMAIL_DOMAIN") or "uets.edu.ec").lower().lstrip("@"),
        superadmin_seed_email=(os.environ.get("SUPERADMIN_SEED_EMAIL") or "superadmin@uets.edu.ec").lower(),
        superadmin_seed_password=os.environ.get("SUPERADMIN_SEED_PASSWORD") or None,
        google_client_id=(os.environ.get("GOOGLE_CLIENT_ID") or "").strip() or None,
        allow_password_registration=(os.environ.get("ALLOW_PASSWORD_REGISTRATION") or "true").lower() != "false",
        allow_password_login=(os.environ.get("ALLOW_PASSWORD_LOGIN") or "false").lower() in ("1", "true", "yes"),
        student_email_suffix=(os.environ.get("STUDENT_EMAIL_SUFFIX") or ".est").strip().lower(),
        user_audit_retention_days=int(os.environ.get("USER_AUDIT_RETENTION_DAYS") or 730),
        error_log_retention_days=int(os.environ.get("ERROR_LOG_RETENTION_DAYS") or 180),
    )
