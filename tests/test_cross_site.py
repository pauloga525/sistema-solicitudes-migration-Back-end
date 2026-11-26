"""Frontend en otro dominio (COOKIE_SAMESITE=none): cookies entre sitios y token CSRF en /auth/session."""
from __future__ import annotations

from dataclasses import replace

import pytest

from tests.conftest import Actor


def _cookie(r, name: str) -> str:
    return next(c for c in r.headers.get_list("set-cookie") if c.startswith(f"{name}="))


@pytest.fixture
def cross_site(monkeypatch):
    from app.core import config, middleware
    from app.modules.auth import router as auth_router

    s = replace(config.get_settings(), cookie_samesite="none", cookie_secure=True)
    monkeypatch.setattr(middleware, "get_settings", lambda: s)
    monkeypatch.setattr(auth_router, "get_settings", lambda: s)


def test_session_devuelve_el_token_de_la_cookie(client):
    a = Actor(client)
    r = a.get("/auth/session")
    assert r.json()["csrfToken"] == a.jar["uets_csrf"]
    # Con la cookie ya emitida, devuelve la misma (no la rota).
    assert a.get("/auth/session").json()["csrfToken"] == a.jar["uets_csrf"]


def test_mismo_sitio_mantiene_samesite_estricto(client):
    r = Actor(client).get("/auth/session")
    assert "SameSite=strict" in _cookie(r, "uets_csrf")


def test_otro_dominio_cookies_samesite_none_y_secure(client, cross_site):
    a = Actor(client)
    r = a.get("/auth/session")
    csrf = _cookie(r, "uets_csrf")
    assert "SameSite=none" in csrf and "Secure" in csrf
    # El frontend envía en la cabecera el token recibido en el JSON, no el leído de la cookie.
    token = r.json()["csrfToken"]
    r = a.post("/auth/login", json={"username": "admin", "password": "admin123"}, headers={"X-CSRF-Token": token})
    assert r.status_code == 201
    session = _cookie(r, "uets_session")
    assert "SameSite=none" in session and "Secure" in session and "HttpOnly" in session


def test_samesite_none_fuerza_secure(monkeypatch):
    from app.core import config

    monkeypatch.setenv("COOKIE_SAMESITE", "none")
    monkeypatch.setenv("COOKIE_SECURE", "false")
    config.get_settings.cache_clear()
    try:
        s = config.get_settings()
        assert s.cookie_samesite == "none" and s.cookie_secure is True
    finally:
        monkeypatch.delenv("COOKIE_SAMESITE")
        monkeypatch.delenv("COOKIE_SECURE")
        config.get_settings.cache_clear()
