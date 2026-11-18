"""Solo Google, correo de estudiante (.est), colaboradores sin acceso y activación del sistema (fechas de envío)."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from tests.conftest import SA_EMAIL, Actor, register_student
from tests.test_google import CLIENT, KEY, token


@pytest.fixture
def alumno(client):
    return register_student(client)


@pytest.fixture
def solo_google(client, monkeypatch):
    """Configuración de producción: Google configurado y sin ingreso por contraseña."""
    from app.core import config
    from app.modules.auth import router as auth_router
    from app.modules.superadmin import users as sa_users

    s = replace(config.get_settings(), google_client_id=CLIENT, allow_password_login=False)
    monkeypatch.setattr(auth_router, "get_settings", lambda: s)
    monkeypatch.setattr(sa_users, "get_settings", lambda: s)
    client.app.state.google_key_resolver = lambda _t: KEY.public_key()
    yield s
    del client.app.state.google_key_resolver


def test_correo_de_estudiante():
    from app.core.config import get_settings
    s = get_settings()
    assert s.is_student_email("mariaperez.est@uets.edu.ec")
    assert s.is_student_email("Maria.Perez.EST@UETS.edu.ec")
    for e in ("jperezl@uets.edu.ec", ".est@uets.edu.ec", "mariaperez.est@gmail.com", "estudiante@uets.edu.ec", None):
        assert not s.is_student_email(e), e


def test_sin_contrasena_solo_google(client, superadmin, solo_google):
    a = Actor(client)
    sess = a.get("/auth/session").json()
    assert sess["passwordLogin"] is False and sess["passwordRegistration"] is False
    r = a.post("/auth/login", json={"username": SA_EMAIL, "password": "lo-que-sea"})
    assert r.status_code == 403 and r.json()["code"] == "PASSWORD_LOGIN_DISABLED"
    r = a.post("/auth/register", json={"email": "x.est@uets.edu.ec", "password": "clave-larga-1", "confirmPassword": "clave-larga-1"})
    assert r.status_code == 403
    # el SUPERADMIN crea personal sin contraseña temporal: ingresa con Google
    r = superadmin.post("/superadmin/users", json={"email": "jvera@uets.edu.ec", "name": "Jorge Luis Vera Ochoa",
                                                   "cedula": "0912345675", "role": "ADMIN"})
    assert r.status_code == 201 and r.json()["tempPassword"] is None
    uid = r.json()["user"]["id"]
    assert superadmin.post(f"/superadmin/users/{uid}/reset-password").status_code == 400
    r = Actor(client).post("/auth/google", json={"credential": token(email="jvera@uets.edu.ec", sub="g-jv")})
    assert r.status_code == 200 and r.json()["user"]["role"] == "ADMIN"


def test_estudiante_est_se_registra_con_google(client, solo_google, mongo):
    r = Actor(client).post("/auth/google", json={"credential": token(email="anaperez.est@uets.edu.ec", sub="g-ana")})
    assert r.status_code == 200 and r.json()["user"]["role"] == "STUDENT"
    assert r.json()["redirectTo"] == "/estudiante/perfil"


def test_colaborador_queda_registrado_y_superadmin_le_da_acceso(client, superadmin, solo_google, mongo):
    t = token(email="mlopezc@uets.edu.ec", sub="g-doc", name="Maria Lopez Cardenas")
    r = Actor(client).post("/auth/google", json={"credential": t})
    assert r.status_code == 403 and r.json()["code"] == "STAFF_NOT_ENABLED"
    assert "Departamento de Sistemas" in r.json()["message"]
    assert mongo.users.count_documents({"email": "mlopezc@uets.edu.ec"}) == 0  # no se crea cuenta
    Actor(client).post("/auth/google", json={"credential": t})  # segundo intento: no duplica
    req = mongo.access_requests.find_one({"email": "mlopezc@uets.edu.ec"})
    assert req["status"] == "pending" and req["attempts"] == 2 and req["name"] == "Maria Lopez Cardenas"
    assert mongo.user_events.find_one({"eventType": "LOGIN_FAILED", "metadata.reason": "staff_not_enabled"})

    lst = superadmin.get("/superadmin/access-requests").json()
    assert lst["pending"] == 1 and lst["rows"][0]["email"] == "mlopezc@uets.edu.ec"
    rid = lst["rows"][0]["id"]
    assert superadmin.post(f"/superadmin/access-requests/{rid}/grant", json={"role": "ADMIN", "reason": "corto"}).status_code == 400
    r = superadmin.post(f"/superadmin/access-requests/{rid}/grant", json={"role": "ADMIN", "reason": "Autoridad que revisa solicitudes"})
    assert r.status_code == 201
    assert superadmin.post(f"/superadmin/access-requests/{rid}/grant", json={"role": "ADMIN", "reason": "Autoridad que revisa solicitudes"}).status_code == 409
    u = mongo.users.find_one({"email": "mlopezc@uets.edu.ec"})
    assert u["role"] == "ADMIN" and u["passwordHash"] is None
    # ahora ingresa con Google y ve la bandeja
    a = Actor(client)
    r = a.post("/auth/google", json={"credential": t})
    assert r.status_code == 200 and r.json()["user"]["role"] == "ADMIN"
    assert a.get("/requests").status_code == 200
    assert superadmin.get("/superadmin/access-requests").json()["pending"] == 0


def test_descartar_y_permisos(client, superadmin, alumno, solo_google, mongo):
    Actor(client).post("/auth/google", json={"credential": token(email="otro.docente@uets.edu.ec", sub="g-x")})
    rid = superadmin.get("/superadmin/access-requests").json()["rows"][0]["id"]
    st = alumno
    assert st.get("/superadmin/access-requests").status_code == 403
    r = superadmin.post(f"/superadmin/access-requests/{rid}/dismiss", json={"reason": "No corresponde"})
    assert r.status_code == 200
    assert mongo.access_requests.find_one({"email": "otro.docente@uets.edu.ec"})["status"] == "dismissed"
    assert superadmin.get("/superadmin/access-requests", params={"status": "dismissed"}).json()["total"] == 1
    assert superadmin.get("/superadmin/access-requests", params={"status": "x"}).status_code == 400


# ── activación del sistema ───────────────────────────────────────────────────
def _fmt(d: datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M")


def test_fechas_de_envio(client, auth, aporte_activo, mongo):
    from app.modules.system.service import reset_settings_cache
    assert client.get("/system/submission-window").json()["state"] == "sin_fechas"
    st = register_student(client)
    body = {"requestType": "proceso_mejora", "representativeLinkId": st.link_id, "payload": {"subjects": ["Matemática"]}}
    now = datetime.now()
    # validaciones
    r = client.put("/system/submission-window", headers=auth, json={"start": _fmt(now), "end": None})
    assert r.status_code == 400
    r = client.put("/system/submission-window", headers=auth, json={"start": _fmt(now), "end": _fmt(now - timedelta(days=1))})
    assert r.status_code == 400 and "posterior" in r.json()["message"]
    assert client.put("/system/submission-window", json={"start": None, "end": None}).status_code in (401, 403)
    assert st.put("/system/submission-window", json={"start": None, "end": None}).status_code == 403
    # ya cerrado
    r = client.put("/system/submission-window", headers=auth,
                   json={"start": _fmt(now - timedelta(days=10)), "end": _fmt(now - timedelta(days=1))})
    assert r.status_code == 200 and r.json()["state"] == "cerrado"
    w = client.get("/system/submission-window").json()
    assert w["open"] is False and "cerrado" in w["message"]
    r = st.post("/me/requests", json=body)
    assert r.status_code == 400 and r.json()["code"] == "SUBMISSION_CLOSED"
    assert st.post("/me/requests/preview", json=body).status_code == 400
    # todavía no abre
    client.put("/system/submission-window", headers=auth,
               json={"start": _fmt(now + timedelta(days=1)), "end": _fmt(now + timedelta(days=5))})
    assert "todavía no" in client.get("/system/submission-window").json()["message"]
    assert st.post("/me/requests", json=body).status_code == 400
    # abierto
    client.put("/system/submission-window", headers=auth,
               json={"start": _fmt(now - timedelta(days=1)), "end": _fmt(now + timedelta(days=5))})
    assert client.get("/system/submission-window").json()["open"] is True
    assert st.post("/me/requests", json=body).status_code == 201
    assert mongo.user_events.count_documents({"eventType": "SUBMISSION_WINDOW_UPDATED"}) == 3
    # sin fechas: sin restricción
    client.put("/system/submission-window", headers=auth, json={"start": None, "end": None})
    assert client.get("/system/submission-window").json()["state"] == "sin_fechas"
    mongo.system_settings.update_many({}, {"$unset": {"submissionStart": "", "submissionEnd": ""}})
    reset_settings_cache()
