"""Fixtures: app real + MongoDB real en una base de pruebas aislada (se borra al final)."""
from __future__ import annotations

import os
from datetime import date

import pymongo
import pytest

TEST_DB = os.environ.get("TEST_MONGODB_DB", "uets_pytest")
MONGO_HOST = os.environ.get("TEST_MONGODB_HOST", "mongodb://127.0.0.1:27017")
os.environ.update({
    "MONGODB_URI": f"{MONGO_HOST}/{TEST_DB}",
    "JWT_SECRET": "clave-de-prueba-para-pytest-min-32-caracteres",
    "PADRON_ENCRYPTION_KEY": "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff",
    "CORS_ORIGINS": "http://localhost:5173",
    # Las pruebas usan cuentas con contraseña; el ingreso solo con Google se prueba aparte (test_google.py).
    "ALLOW_PASSWORD_LOGIN": "true",
    "STUDENT_CAN_CANCEL": "true",
    "STAFF_TEMP_PASSWORD": "true",  # las pruebas antiguas cubren la contraseña temporal; test_acceso prueba Google  # las pruebas existentes cubren la cancelación; test_docentes prueba que se puede apagar
})
os.environ.pop("ADMIN_SEED_PASSWORD", None)
os.environ.pop("NODE_ENV", None)

from fastapi.testclient import TestClient  # noqa: E402

from app.core.rate_limit import storage  # noqa: E402
from app.main import app  # noqa: E402

VALID_REP_DNI = "0102030400"
VALID_STUDENT_DNI = "0105566772"
OTHER_STUDENT_DNI = "0912345675"


@pytest.fixture(scope="session")
def mongo():
    client = pymongo.MongoClient(MONGO_HOST)
    client.drop_database(TEST_DB)
    yield client[TEST_DB]
    client.drop_database(TEST_DB)


class _Client(TestClient):
    """TestClient que, con la cabecera de prueba X-Test-Actor, envía la petición como ese actor."""

    actors: dict = {}

    def request(self, method, url, **kw):
        headers = kw.get("headers") or {}
        who = headers.get("X-Test-Actor") if isinstance(headers, dict) else None
        if who:
            kw["headers"] = {k: v for k, v in headers.items() if k != "X-Test-Actor"}
            actor = self.actors.get(who)
            if actor is None:
                actor = Actor(self)
                actor.login("admin", "admin123")
                self.actors[who] = actor
            return actor.request(method, url, **kw)
        return super().request(method, url, **kw)


@pytest.fixture(scope="session")
def client(mongo):
    with _Client(app, client=("10.0.0.1", 5000)) as c:
        yield c


@pytest.fixture(autouse=True)
def _reset_actors(client):
    _Client.actors = {}
    yield


# Colecciones que cada prueba deja limpias (base de pruebas aislada; nunca la base real).
PER_TEST = ("requests", "counters", "aniolectivos", "solicitudconfigs", "students", "request_events", "user_events",
            "rate_limits", "limit_locks", "request_limits", "limit_exceptions", "request_deadlines", "system_errors",
            "student_profiles", "representatives", "student_representatives", "access_requests",
            "teachers", "teacher_assignments", "teacher_tracking", "teacher_imports", "notifications", "email_outbox",
            "teacher_tracking_events")


@pytest.fixture(autouse=True)
def clean(mongo):
    storage.reset()
    for col in PER_TEST:
        mongo[col].delete_many({})
    mongo.system_settings.delete_many({})
    mongo.users.delete_many({"role": "STUDENT"})
    mongo.users.delete_many({"email": {"$regex": "^(sa2|admin2|nuevo)"}})
    mongo.users.delete_many({"role": {"$ne": "SUPERADMIN"}, "email": {"$regex": "@uets\\.edu\\.ec$"}, "username": None})
    mongo.users.update_many({}, {"$set": {"isActive": True}})
    from app.modules.system.service import reset_settings_cache
    reset_settings_cache()
    yield


class Actor:
    """Cliente con su propia cookie de sesión y token CSRF (varias personas sobre la misma app de pruebas)."""

    def __init__(self, client: TestClient, ip: str = "10.0.0.1"):
        self.client = client
        self.jar: dict[str, str] = {}
        self.ip = ip
        self.user: dict | None = None

    def _absorb(self, r) -> None:
        for raw in r.headers.get_list("set-cookie"):
            first, *attrs = [a.strip() for a in raw.split(";")]
            name, _, value = first.partition("=")
            dead = any(a.lower() in ("max-age=0",) or a.lower().startswith("expires=thu, 01 jan 1970") for a in attrs)
            if dead or value in ("", '""'):
                self.jar.pop(name, None)
            else:
                self.jar[name] = value.strip('"')

    def request(self, method: str, url: str, csrf: bool = True, **kw):
        if "uets_csrf" not in self.jar and method.upper() not in ("GET", "HEAD", "OPTIONS"):
            self._absorb(self.client.get("/auth/session", headers=self._headers({})))
        headers = self._headers(kw.pop("headers", None) or {})
        if csrf and "uets_csrf" in self.jar:
            headers.setdefault("X-CSRF-Token", self.jar["uets_csrf"])
        r = self.client.request(method, url, headers=headers, **kw)
        self._absorb(r)
        self.client.cookies.clear()
        return r

    def _headers(self, h: dict) -> dict:
        out = dict(h)
        if self.jar:
            out["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.jar.items())
        return out

    def get(self, url, **kw): return self.request("GET", url, **kw)
    def post(self, url, **kw): return self.request("POST", url, **kw)
    def put(self, url, **kw): return self.request("PUT", url, **kw)
    def patch(self, url, **kw): return self.request("PATCH", url, **kw)
    def delete(self, url, **kw): return self.request("DELETE", url, **kw)

    def login(self, username: str, password: str):
        r = self.post("/auth/login", json={"username": username, "password": password})
        assert r.status_code == 201, r.text
        self.user = r.json()["user"]
        return r


SA_EMAIL, SA_DEV_PASSWORD, SA_PASSWORD = "superadmin@uets.edu.ec", "superadmin123", "Clave-Super-Admin-2026"


@pytest.fixture
def admin(client):
    a = Actor(client)
    a.login("admin", "admin123")
    return a


@pytest.fixture
def auth(admin):
    """Compatibilidad con las pruebas originales: `headers=auth` → la petición se hace como ADMIN."""
    return {"X-Test-Actor": "admin"}


@pytest.fixture
def superadmin(client, mongo):
    a = Actor(client)
    u = mongo.users.find_one({"email": SA_EMAIL})
    if u.get("forcePasswordChange"):
        a.login(SA_EMAIL, SA_DEV_PASSWORD)
        r = a.post("/auth/change-password", json={"currentPassword": "", "newPassword": SA_PASSWORD, "confirmPassword": SA_PASSWORD})
        assert r.status_code == 200, r.text
    a.login(SA_EMAIL, SA_PASSWORD)
    return a


REP_NAME = "Juan Carlos Perez Lopez"


def split_name(full: str) -> dict:
    """"Maria Jose Perez Lopez" → nombres "Maria Jose", apellidos "Perez Lopez" (formato del perfil)."""
    w = full.split()
    return {"firstNames": " ".join(w[:-2]), "lastNames": " ".join(w[-2:])}


def register_student(client, dni: str = VALID_STUDENT_DNI, name: str = "Maria Jose Perez Lopez",
                     rep_dni: str = VALID_REP_DNI, course: str = "5to", paralelo: str = "B", ip: str = "10.0.0.1") -> Actor:
    a = Actor(client, ip)
    if rep_dni == dni:
        rep_dni = "0912345675" if dni != "0912345675" else "0105566772"
    email = f"est{dni}.est@uets.edu.ec"
    r = a.post("/auth/register", json={"email": email, "password": "clave-estudiante-1", "confirmPassword": "clave-estudiante-1"})
    assert r.status_code == 201, r.text
    a.user = r.json()["user"]
    r = a.put("/me/profile", json={**split_name(name), "cedula": dni, "course": course, "paralelo": paralelo})
    assert r.status_code == 200, r.text
    r = a.post("/me/representatives", json={**split_name(REP_NAME), "cedula": rep_dni, "phone": "0998765432",
                                            "email": f"rep{rep_dni}@uets.edu.ec", "relationship": "Padre", "isPrimary": True})
    assert r.status_code == 201, r.text
    a.link_id = r.json()["representatives"][0]["linkId"]
    return a


class StudentRouter:
    """Para las pruebas originales del formulario: usa (o crea) la cuenta del estudiante de cada solicitud."""

    def __init__(self, client):
        self.client = client
        self.by_dni: dict[str, Actor] = {}

    def actor(self, dni: str) -> Actor:
        if dni not in self.by_dni:
            self.by_dni[dni] = register_student(self.client, dni)
        return self.by_dni[dni]

    def post(self, url, json=None, **kw):
        dni = (json or {}).get("studentDni") if isinstance(json, dict) else None
        from app.shared.validators.cedula import validate_ecuadorian_dni
        dni = dni if isinstance(dni, str) and validate_ecuadorian_dni(dni) else VALID_STUDENT_DNI
        return self.actor(dni).post(url, json=json, **kw)


@pytest.fixture
def student(client):
    return StudentRouter(client)


@pytest.fixture
def aporte_activo(client, auth):
    y = date.today().year
    body = {"name": "Año test", "startDate": f"{y - 1}-01-01", "endDate": f"{y + 1}-12-31",
            "trimestres": [{"name": "T", "startDate": f"{y - 1}-01-01", "endDate": f"{y + 1}-12-31",
                            "aportes": [{"name": "Aporte test", "startDate": f"{y - 1}-01-01", "endDate": f"{y + 1}-12-31"}]}]}
    r = client.post("/plazos", headers=auth, json=body)
    assert r.status_code == 201, r.text
    return r.json()


def make_request(**overrides):
    body = {"requestType": "proceso_mejora", "representativeName": "Juan Carlos Perez Lopez",
            "representativeDni": VALID_REP_DNI, "studentName": "Maria Jose Perez Lopez",
            "studentDni": VALID_STUDENT_DNI, "course": "5to", "paralelo": "B",
            "payload": {"subjects": ["Matemática", "Inglés"]}}
    body.update(overrides)
    return body
