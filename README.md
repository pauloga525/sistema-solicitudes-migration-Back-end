# Sistema de Solicitudes UETS — Backend (FastAPI)

API en FastAPI + MongoDB (Motor). Conserva los contratos del sistema NestJS original y agrega sesión por cookie, área del estudiante, panel SUPERADMIN e ingreso con Google institucional.

Frontend: repositorio `sistema-solicitudes-migration-Front` (Angular).

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # JWT_SECRET (≥32) y PADRON_ENCRYPTION_KEY (64 hex) obligatorias
uvicorn app.main:app --port 3001 --reload
pytest                             # requiere MongoDB en localhost:27017 (usa la base uets_pytest y la borra)
```

> Use una base de **pruebas** para probar (`MONGODB_URI=.../uets_pruebas`). Al arrancar, el backend crea colecciones e índices nuevos, copia `admins` → `users` y crea el SUPERADMIN inicial. No borra nada, pero sí escribe.

Variables de entorno: ver `.env.example` (incluye `GOOGLE_CLIENT_ID`, `SUPERADMIN_SEED_*`, `COOKIE_SECURE`, `INSTITUTIONAL_EMAIL_DOMAIN`, retención y opciones de registro).

El ingreso es **solo con Google** (`GOOGLE_CLIENT_ID` obligatorio). Para usar cuentas de prueba con contraseña en tu equipo, agrega `ALLOW_PASSWORD_LOGIN=true` al `.env` (nunca en producción).
MongoDB como *replica set* (aunque sea de un nodo) permite que la corrección de estado y su auditoría sean una transacción; sin él, se usa compensación.

Herramientas (solo con bases de prueba): `tools/crear_estudiante_prueba.py` (cuentas de estudiante de prueba) y `tools/seed_demo.py` (datos de demostración).

## Estructura

```
app/
├── main.py                 arranque: valida entorno → conecta → índices → seeds (admin, copia a users, SUPERADMIN) → middleware → routers
├── core/                   config, security (bcrypt, sesión JWT en cookie), middleware (CSRF, cabeceras), exceptions, dependencies (sesión y roles), rate_limit
├── database/               connection (Motor), indexes (idénticos a Mongoose)
├── shared/
│   ├── validators/         class_validator (mensajes iguales a NestJS), cedula (módulo 10), object_id
│   ├── crypto/             AES-256-GCM + HMAC/HKDF compatible con Node
│   ├── pdf/                pdfkit_compat (tipografía y kerning de pdfkit sobre ReportLab), merger (pypdf), afm/
│   ├── middleware/         memory_multipart (subidas 100 % en memoria)
│   └── utilities/          fechas como JavaScript, escape de regex
├── modules/
│   ├── catalog.py          enums, materias, campus por curso, cupos
│   ├── auth/ (contraseña y Google) requests/ request_types/ solicitud_config/ plazos/ padron/
│   ├── students/ (/me) superadmin/ users/ audit/ system/ limits/
│   │     router.py (HTTP) · schemas.py (Pydantic + reglas) · service.py (negocio) · repository.py (MongoDB)
│   └── requests/request_type_config.py   metadata por tipo de trámite (fuente única de verdad)
└── seeds/admin_seed.py
```

### Agregar un nuevo tipo de trámite

1. Valor en `RequestType` (`modules/catalog.py`).
2. Entrada en `REQUEST_TYPE_METADATA` (label, prefix, initial_status, auto_approve_on_view, uses_academic_catalog, uses_aporte_quota, build_pdf_content).
3. Esquema de su `payload` en `PAYLOAD_SCHEMA_BY_TYPE` (`modules/requests/schemas.py`) y su plantilla PDF en `modules/requests/templates/`.
