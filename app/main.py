"""Punto de entrada FastAPI — Sistema de Solicitudes Académicas UETS.

Arranque (equivalente a main.ts + AppModule de NestJS):
1. Carga backend/.env y valida variables de seguridad (sale con código 1 si faltan).
2. Conecta a MongoDB, asegura índices (idempotente), seed del admin, copia admins → users y seed del SUPERADMIN.
3. CORS por lista blanca y errores con formato NestJS.

Ejecutar:  uvicorn app.main:app --port 3001
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import get_settings, load_env_file, validate_env
from app.core.exceptions import register_exception_handlers
from app.database.connection import connect, disconnect
from app.database.indexes import ensure_indexes, ensure_new_indexes
from app.core.middleware import SecurityMiddleware
from app.modules.auth.router import router as auth_router
from app.modules.padron.router import router as padron_router
from app.modules.plazos.router import router as plazos_router
from app.modules.request_types.router import router as request_types_router
from app.modules.requests.router import router as requests_router
from app.modules.solicitud_config.router import router as solicitud_config_router
from app.modules.students.router import router as students_router
from app.modules.superadmin.router import router as superadmin_router
from app.modules.system.router import router as system_router
from app.modules.teachers.oversight import router as oversight_router
from app.modules.teachers.router import router as teacher_router
from app.modules.users.bootstrap import migrate_legacy_admins, seed_superadmin
from app.seeds.admin_seed import seed_admin

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    db = await connect(settings)
    await ensure_indexes(db)
    await ensure_new_indexes(db)
    await seed_admin(db, settings)
    await migrate_legacy_admins(db)  # copia admins → users sin modificar `admins`
    await seed_superadmin(db, settings)
    yield
    await disconnect()


def create_app() -> FastAPI:
    load_env_file()
    validate_env()
    settings = get_settings()

    app = FastAPI(title="UETS — Sistema de Solicitudes", version="2.0.0", lifespan=lifespan,
                  docs_url="/docs" if not settings.is_production else None, redoc_url=None, openapi_url=
                  "/openapi.json" if not settings.is_production else None)

    @app.middleware("http")
    async def rate_limit_headers(request: Request, call_next):
        response = await call_next(request)
        for k, v in (getattr(request.state, "rate_limit_headers", None) or {}).items():
            response.headers.setdefault(k, v)
        return response

    app.add_middleware(SecurityMiddleware)
    # Cookies de sesión: CORS con credenciales solo para los orígenes de la lista blanca.
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_methods=["*"],
                       allow_headers=["*"], allow_credentials=True)
    register_exception_handlers(app)

    for router in (auth_router, requests_router, request_types_router, solicitud_config_router,
                   plazos_router, padron_router, students_router, superadmin_router, system_router, teacher_router,
                   oversight_router):
        app.include_router(router)
    return app


app = create_app()
