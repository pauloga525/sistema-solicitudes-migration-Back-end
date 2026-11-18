"""Compatibilidad con una COPIA de la base del sistema original (NestJS/Mongoose).

Se ejecuta solo si se indica la base de origen:
    COMPAT_SOURCE_DB=sistema_solicitudes pytest tests/test_compat_original_db.py
La base de origen nunca se modifica: se copia a `<origen>_compat_copy` y se trabaja sobre la copia.
"""
import os

import pymongo
import pytest
from fastapi.testclient import TestClient

SOURCE = os.environ.get("COMPAT_SOURCE_DB")
pytestmark = pytest.mark.skipif(not SOURCE, reason="COMPAT_SOURCE_DB no definido")


@pytest.fixture(scope="module")
def copy_client():
    host = os.environ.get("TEST_MONGODB_HOST", "mongodb://127.0.0.1:27017")
    cli = pymongo.MongoClient(host)
    target = f"{SOURCE}_compat_copy"
    cli.drop_database(target)
    for name in cli[SOURCE].list_collection_names():
        docs = list(cli[SOURCE][name].find())
        if docs:
            cli[target][name].insert_many(docs)
    from app.core import config
    from app.main import app
    old = os.environ["MONGODB_URI"]
    os.environ["MONGODB_URI"] = f"{host}/{target}"
    config.get_settings.cache_clear()
    with TestClient(app, client=("10.9.9.9", 1)) as c:
        yield c, cli[SOURCE], cli[target]
    os.environ["MONGODB_URI"] = old
    config.get_settings.cache_clear()
    cli.drop_database(target)


def test_lee_y_gestiona_datos_del_original(copy_client):
    c, source, target = copy_client
    before = source.requests.count_documents({})
    login = c.post("/auth/login", json={"username": "admin", "password": os.environ.get("COMPAT_ADMIN_PASSWORD", "admin123")})
    assert login.status_code == 201, "el hash bcrypt creado por NestJS debe validar en Python"
    h = {"Authorization": f"Bearer {login.json()['access_token']}"}
    page = c.get("/requests", headers=h, params={"pageSize": 200}).json()
    assert page["total"] == before
    for r in page["data"][:20]:
        d = c.get(f"/requests/{r['_id']}", headers=h)
        assert d.status_code == 200
        pdf = c.get(f"/requests/{r['_id']}/pdf", headers=h)
        assert pdf.status_code == 200 and pdf.content[:5] == b"%PDF-"
        if r.get("pdfAccessToken"):
            assert c.get(f"/requests/{r['_id']}/pdf", params={"token": r["pdfAccessToken"]}).status_code == 200
    assert c.get("/plazos", headers=h).status_code == 200
    assert c.get("/solicitud-config/proceso_mejora").status_code == 200
    # la base original no se tocó
    assert source.requests.count_documents({}) == before
    # la copia conserva __v y tipos
    doc = target.requests.find_one()
    if doc:
        assert "__v" in doc and type(doc["createdAt"]).__name__ == "datetime"
