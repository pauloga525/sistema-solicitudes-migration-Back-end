"""Ingreso con Google: el ID token se verifica en el servidor (firma, audiencia, emisor, dominio institucional)."""
from __future__ import annotations

import time
from dataclasses import replace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from tests.conftest import Actor

CLIENT = "123-prueba.apps.googleusercontent.com"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def token(email="alumna.google.est@uets.edu.ec", hd="uets.edu.ec", aud=CLIENT, key=KEY, sub="g-111", verified=True, exp=600, **extra):
    now = int(time.time())
    claims = {"iss": "https://accounts.google.com", "aud": aud, "sub": sub, "email": email, "email_verified": verified,
              "iat": now, "exp": now + exp, "name": "Alumna Google Prueba", **extra}
    if hd:
        claims["hd"] = hd
    return jwt.encode(claims, key, algorithm="RS256")


@pytest.fixture
def google(client, monkeypatch):
    from app.core import config
    from app.modules.auth import router as auth_router

    s = replace(config.get_settings(), google_client_id=CLIENT)
    monkeypatch.setattr(auth_router, "get_settings", lambda: s)
    client.app.state.google_key_resolver = lambda _t: KEY.public_key()
    yield
    del client.app.state.google_key_resolver


def test_sin_configurar_404(client):
    r = Actor(client).post("/auth/google", json={"credential": token()})
    assert r.status_code == 404
    assert Actor(client).get("/auth/session").json()["googleClientId"] is None


def test_cuenta_institucional_crea_estudiante(client, google, mongo):
    a = Actor(client)
    assert a.get("/auth/session").json()["googleClientId"] == CLIENT
    r = a.post("/auth/google", json={"credential": token()})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["user"]["role"] == "STUDENT" and body["redirectTo"] == "/estudiante/perfil"
    u = mongo.users.find_one({"email": "alumna.google.est@uets.edu.ec"})
    assert u["passwordHash"] is None and u["googleSub"] == "g-111"
    assert a.get("/me/profile").status_code == 200
    # sin contraseña: el ingreso por contraseña no funciona, pero el mensaje no revela nada
    r = Actor(client).post("/auth/login", json={"username": "alumna.google.est@uets.edu.ec", "password": "cualquiera"})
    assert r.status_code == 401 and r.json()["message"] == "Usuario o contraseña incorrectos"
    assert mongo.user_events.find_one({"eventType": "STUDENT_REGISTERED", "metadata.method": "google"})


@pytest.mark.parametrize("kw,motivo", [
    ({"email": "alguien@gmail.com", "hd": None}, "cuenta personal"),
    ({"hd": "otrocolegio.edu.ec", "email": "x@otrocolegio.edu.ec"}, "otro dominio"),
    ({"hd": "uets.edu.ec", "email": "x@gmail.com"}, "hd falso con correo externo"),
    ({"aud": "otro-cliente"}, "otra aplicación"),
    ({"key": OTHER_KEY}, "firma falsa"),
    ({"verified": False}, "correo no verificado"),
    ({"exp": -120}, "vencido"),
])
def test_tokens_rechazados(client, google, mongo, kw, motivo):
    r = Actor(client).post("/auth/google", json={"credential": token(**kw)})
    assert r.status_code == 401 and r.json()["code"] == "GOOGLE_REJECTED", motivo
    assert mongo.users.count_documents({"role": "STUDENT"}) == 0


def test_personal_existente_ingresa_con_su_rol_y_temporal_queda_anulada(client, google, superadmin, mongo):
    r = superadmin.post("/superadmin/users", json={"email": "nuevo.admin@uets.edu.ec", "name": "Pedro Pablo Ruiz Diaz",
                                                   "cedula": "0912345675", "role": "ADMIN"})
    assert r.status_code == 201
    a = Actor(client)
    r = a.post("/auth/google", json={"credential": token(email="nuevo.admin@uets.edu.ec", sub="g-222")})
    assert r.status_code == 200 and r.json()["user"]["role"] == "ADMIN" and r.json()["redirectTo"] == "/admin/dashboard"
    u = mongo.users.find_one({"email": "nuevo.admin@uets.edu.ec"})
    assert u["forcePasswordChange"] is False and u["passwordHash"] is None
    assert a.get("/requests").status_code == 200
    # la cuenta queda atada a esa identidad de Google
    r = Actor(client).post("/auth/google", json={"credential": token(email="nuevo.admin@uets.edu.ec", sub="g-otro")})
    assert r.status_code == 401
    mongo.users.delete_one({"email": "nuevo.admin@uets.edu.ec"})


def test_desactivado_no_entra_con_google(client, google, mongo):
    Actor(client).post("/auth/google", json={"credential": token()})
    mongo.users.update_one({"email": "alumna.google.est@uets.edu.ec"}, {"$set": {"isActive": False}})
    r = Actor(client).post("/auth/google", json={"credential": token()})
    assert r.status_code == 401 and "desactivada" in r.json()["message"]


def test_registro_con_contrasena_desactivable(client, monkeypatch):
    from app.core import config
    from app.modules.auth import router as auth_router

    s = replace(config.get_settings(), allow_password_registration=False)
    monkeypatch.setattr(auth_router, "get_settings", lambda: s)
    r = Actor(client).post("/auth/register", json={"email": "a.b@uets.edu.ec", "password": "clave-larga-1", "confirmPassword": "clave-larga-1"})
    assert r.status_code == 403 and r.json()["code"] == "PASSWORD_REGISTRATION_DISABLED"
