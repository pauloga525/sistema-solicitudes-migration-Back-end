"""Pruebas de integración de la API contra MongoDB real."""
import io
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import jwt
import openpyxl
import pytest
from bson import ObjectId

from app.modules.curriculum import CURRICULUM_SUBJECTS
from tests.conftest import OTHER_STUDENT_DNI, VALID_STUDENT_DNI, Actor, make_request

FAKE_ID = "0123456789abcdef01234567"


# ── auth (sesión por cookie HttpOnly + CSRF; decisión C2) ─────────────────────
def test_login_ok_cookie_httponly(client):
    a = Actor(client)
    r = a.post("/auth/login", json={"username": "admin", "password": "admin123"})
    assert r.status_code == 201
    body = r.json()
    assert body["user"]["username"] == "admin" and body["user"]["role"] == "ADMIN" and body["redirectTo"] == "/admin/dashboard"
    assert "access_token" not in body  # el token nunca llega a JavaScript
    cookie = next(c for c in r.headers.get_list("set-cookie") if c.startswith("uets_session="))
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie
    payload = jwt.decode(a.jar["uets_session"], options={"verify_signature": False})
    assert {"sub", "role", "fp", "loginAt", "checkedAt", "exp", "typ"} <= payload.keys()
    assert a.get("/auth/session").json()["user"]["role"] == "ADMIN"


def test_login_sin_csrf_403(client):
    r = client.post("/auth/login", json={"username": "admin", "password": "admin123"})
    assert r.status_code == 403 and r.json()["code"] == "AUTH_CSRF"
    a = Actor(client)
    a.get("/auth/session")
    r = a.post("/auth/login", json={"username": "admin", "password": "admin123"}, headers={"X-CSRF-Token": "otro"})
    assert r.status_code == 403


def test_login_incorrecto(client):
    a = Actor(client)
    r = a.post("/auth/login", json={"username": "admin", "password": "mala"})
    assert r.status_code == 401
    assert r.json() == {"message": "Usuario o contraseña incorrectos", "error": "Unauthorized", "statusCode": 401}
    r = a.post("/auth/login", json={"username": "noexiste@uets.edu.ec", "password": "mala"})
    assert r.json()["message"] == "Usuario o contraseña incorrectos"


def test_login_validacion_estricta(client):
    a = Actor(client)
    r = a.post("/auth/login", json={"username": "admin", "password": "admin123", "x": 1})
    assert r.status_code == 400 and r.json()["message"] == ["property x should not exist"]
    r = a.post("/auth/login", json={"username": ""})
    assert r.json()["message"] == ["username should not be empty", "password should not be empty", "password must be a string"]


@pytest.mark.parametrize("header", [None, "Bearer abc.def.ghi", "Basic xyz"])
def test_rutas_protegidas_sin_sesion(client, header):
    a = Actor(client)
    headers = {"Authorization": header} if header else {}
    for method, url in [("GET", "/requests"), ("GET", f"/requests/{FAKE_ID}"), ("PATCH", f"/requests/{FAKE_ID}/status"),
                        ("GET", "/plazos"), ("POST", "/plazos"), ("PUT", f"/plazos/{FAKE_ID}"),
                        ("DELETE", f"/plazos/{FAKE_ID}"), ("PATCH", "/solicitud-config/proceso_mejora/courses"),
                        ("PATCH", "/solicitud-config/proceso_mejora/subjects"), ("POST", "/padron/import"),
                        ("POST", "/requests"), ("POST", "/requests/preview")]:
        r = a.request(method, url, headers=headers)
        assert r.status_code == 401, (method, url)
        assert r.json() == {"message": "Unauthorized", "statusCode": 401}


def test_cookie_manipulada_o_expirada(client):
    a = Actor(client)
    a.get("/auth/session")
    a.jar["uets_session"] = jwt.encode({"sub": "x", "role": "SUPERADMIN", "typ": "session", "exp": int(datetime.now().timestamp()) - 1},
                                       "clave-de-prueba-para-pytest-min-32-caracteres", algorithm="HS256")
    assert a.get("/requests").status_code == 401
    a.jar["uets_session"] = jwt.encode({"sub": "x", "role": "SUPERADMIN", "typ": "session", "exp": int(datetime.now().timestamp()) + 99},
                                       "otra-clave-cualquiera-de-32-caracteres-xx", algorithm="HS256")
    assert a.get("/superadmin/dashboard").status_code == 401


def _run_seed(dbname, settings):
    import asyncio
    from motor.motor_asyncio import AsyncIOMotorClient
    from app.seeds.admin_seed import seed_admin

    async def go():
        cli = AsyncIOMotorClient("mongodb://127.0.0.1:27017")
        try:
            return await seed_admin(cli[dbname], settings)
        finally:
            cli.close()
    return asyncio.run(go())


def test_seed_admin_no_sobrescribe(mongo, client):
    from app.core.config import get_settings
    admin = mongo.admins.find_one({"username": "admin"})
    assert admin["passwordHash"].startswith("$2b$10$") and admin["__v"] == 0 and admin["role"] == "admin"
    assert _run_seed(mongo.name, get_settings()) == "exists"
    assert mongo.admins.find_one({"username": "admin"})["passwordHash"] == admin["passwordHash"]


def test_seed_en_produccion_sin_password(mongo):
    from dataclasses import replace
    from app.core.config import get_settings
    s = replace(get_settings(), admin_seed_username="nuevo_admin", node_env="production", admin_seed_password=None)
    assert _run_seed(mongo.name, s) == "skipped"
    assert mongo.admins.find_one({"username": "nuevo_admin"}) is None
    assert _run_seed(mongo.name, replace(s, admin_seed_password="Prod-Segura-123")) == "created"
    mongo.admins.delete_one({"username": "nuevo_admin"})


# ── creación de solicitudes ─────────────────────────────────────────────────
def test_sin_aporte_activo_bloquea(client, student):
    r = student.post("/requests", json=make_request())
    assert r.status_code == 400 and r.json()["message"] == "No hay un periodo de aportes activo actualmente."
    assert client.get("/plazos/aporte-actual").content == b""


def test_crear_valida(client, student, aporte_activo, mongo):
    r = student.post("/requests", json=make_request())
    assert r.status_code == 201
    body = r.json()
    assert body["requestNumber"] == f"PM-{datetime.now().year}-0001"
    assert body["status"] == "en_revision" and body["campus"] == "Maria_Auxiliadora"
    assert len(body["pdfAccessToken"]) == 48
    doc = mongo.requests.find_one({"_id": ObjectId(body["_id"])})
    assert list(doc.keys()) == ["_id", "requestNumber", "requestType", "status", "representativeName", "representativeDni",
                                "studentName", "studentDni", "course", "paralelo", "campus", "payload", "pdfAccessToken",
                                # campos aditivos (decisión C7): dueño, responsable, periodo y plazo de atención
                                "studentUserId", "assignedTo", "periodKey", "dueAt",
                                "createdAt", "updatedAt", "__v"]
    assert isinstance(doc["createdAt"], datetime)


def test_campus_del_cliente_rechazado(client, student, aporte_activo):
    r = student.post("/requests", json=make_request(campus="Yanuncay"))
    assert r.status_code == 400 and r.json()["message"] == ["property campus should not exist"]


@pytest.mark.parametrize("overrides,expected", [
    ({"studentDni": "0102030405"}, "studentDni debe ser una cédula ecuatoriana válida."),
    ({"representativeDni": "123"}, "representativeDni debe ser una cédula ecuatoriana válida."),
    ({"studentName": "Ana Mora"}, "studentName debe contener al menos un nombre y dos apellidos (solo letras)."),
    ({"course": "x"}, "course must be one of the following values: 1ro_Basica, 2do_Basica, 3ro_Basica, 4to_Basica, 5to, 6to, 7mo, 8vo, 9no, 10mo, 1ro_Bach, 2do_Bach, 3ro_Bach"),
    ({"paralelo": "Z"}, "paralelo must be one of the following values: A, B, C, D, E, F, G, H, I, J, E1, E2, D1, D2, C1, C2, F1, F2, F3, A1, A2"),
    ({"payload": {"subjects": []}}, "payload.subjects must contain at least 1 elements"),
    ({"payload": {"subjects": ["Astrología"]}}, "payload.each value in subjects must be one of the following values: "),
    ({"payload": {"subjects": ["Química"]}}, "Las siguientes materias no corresponden a tu curso: Química"),
    ({"payload": "x"}, "nested property payload must be either object or array"),
])
def test_datos_invalidos(client, student, aporte_activo, overrides, expected):
    r = student.post("/requests", json=make_request(**overrides))
    assert r.status_code == 400 and expected in r.json()["message"]


def test_catalogo_inactivo_rechazado(client, student, aporte_activo, auth):
    client.patch("/solicitud-config/proceso_mejora/courses", headers=auth, json={"name": "8vo", "active": 0})
    r = student.post("/requests", json=make_request(course="8vo"))
    assert r.status_code == 400 and r.json()["message"] == 'El curso "8vo" no está disponible actualmente para este trámite.'
    client.patch("/solicitud-config/proceso_mejora/subjects", headers=auth, json={"name": "Computación", "active": 0})
    r = student.post("/requests", json=make_request(payload={"subjects": ["Computación"]}))
    assert r.json()["message"] == "Las siguientes materias no están disponibles actualmente: Computación"
    client.patch("/solicitud-config/proceso_mejora/courses", headers=auth, json={"name": "8vo", "active": 1})
    r = student.post("/requests", json=make_request(course="8vo", payload={"subjects": ["Matemática"]}))
    assert r.status_code == 201 and r.json()["campus"] == "Yanuncay"


def test_duplicados_activos_409(client, student, aporte_activo):
    assert student.post("/requests", json=make_request()).status_code == 201
    r = student.post("/requests", json=make_request(payload={"subjects": ["Inglés", "Computación"]}))
    assert r.status_code == 409
    assert r.json()["message"] == "Ya existe una solicitud activa para las materias: Inglés"


def test_cupo_por_aporte(client, student, aporte_activo, auth):
    assert student.post("/requests", json=make_request()).status_code == 201
    r = client.get("/plazos/cupo", params={"studentDni": VALID_STUDENT_DNI, "course": "5to"})
    assert (r.json()["limite"], r.json()["consumido"], r.json()["disponible"]) == (4, 2, 2)
    r = student.post("/requests", json=make_request(studentName="Otra Persona Distinta", payload={"subjects": ["Computación", "Enseñanza Religiosa Escolar", "Ciencias Naturales"]}))
    assert r.status_code == 400
    assert r.json()["message"] == "El cupo de materias para este aporte es 4. Ya se han solicitado 2, quedan 2 disponibles."
    # Bachillerato: 6
    r = client.get("/plazos/cupo", params={"studentDni": OTHER_STUDENT_DNI, "course": "1ro_Bach"})
    assert r.json()["limite"] == 6


def test_cupo_ignora_rechazadas_y_canceladas(client, student, aporte_activo, auth):
    rid = student.post("/requests", json=make_request()).json()["_id"]
    client.patch(f"/requests/{rid}/status", headers=auth, json={"status": "cancelado"})
    r = client.get("/plazos/cupo", params={"studentDni": VALID_STUDENT_DNI, "course": "5to"})
    assert r.json()["consumido"] == 0


def test_numeracion_concurrente_sin_huecos(client, student, aporte_activo, mongo):
    # 10 solicitudes simultáneas (límite de 10/min) de alumnos distintos
    from tests.conftest import VALID_REP_DNI  # noqa
    dnis = ["0912345675", "0102030400", "0105566772", "0103030300", "1710034065",
            "0101010101", "0201020103", "0301030105", "0401040107", "0501050109"]
    from app.shared.validators.cedula import validate_ecuadorian_dni
    dnis = [d if validate_ecuadorian_dni(d) else next(d[:9] + str(k) for k in range(10) if validate_ecuadorian_dni(d[:9] + str(k))) for d in dnis]

    for d in dnis:  # cuentas creadas antes de disparar en paralelo
        student.actor(d)

    def post(i):
        return student.post("/requests", json=make_request(studentDni=dnis[i], studentName=f"Alumno Numero {chr(65 + i)}{chr(65 + i)}", payload={"subjects": ["Enseñanza Religiosa Escolar"]},
                                                       representativeDni="0912345675" if dnis[i] == VALID_REP_DNI else VALID_REP_DNI))

    with ThreadPoolExecutor(max_workers=10) as ex:
        results = list(ex.map(post, range(10)))
    assert all(r.status_code == 201 for r in results), [(r.status_code, r.text[:200]) for r in results if r.status_code != 201]
    numbers = sorted(r.json()["requestNumber"] for r in results)
    year = datetime.now().year
    assert numbers == [f"PM-{year}-{i:04d}" for i in range(1, 11)]
    assert mongo.counters.find_one({"name": f"PM-{year}"})["seq"] == 10


def test_rate_limit_crear_10_por_minuto(client, student, aporte_activo):
    codes = [student.post("/requests", json={}).status_code for _ in range(11)]
    assert codes[:10] == [400] * 10 and codes[10] == 429
    r = student.post("/requests", json={})
    assert r.json() == {"statusCode": 429, "message": "ThrottlerException: Too Many Requests"} and "retry-after" in r.headers


def test_rate_limit_login_5(client):
    a = Actor(client)
    codes = [a.post("/auth/login", json={"username": "a", "password": "b"}).status_code for _ in range(6)]
    assert codes == [401] * 5 + [429]


def test_rate_limit_global_60_por_ruta(client):
    codes = [client.get("/request-types").status_code for _ in range(61)]
    assert codes[:60] == [200] * 60 and codes[60] == 429
    # otra ruta tiene su propio contador (como ThrottlerGuard: clave = ruta + IP)
    r = client.get("/solicitud-config/proceso_mejora")
    assert r.status_code == 200 and r.headers["x-ratelimit-limit"] == "60"


# ── consultas y estados ──────────────────────────────────────────────────────
def test_listado_filtros_busqueda_paginacion(client, student, aporte_activo, auth):
    student.post("/requests", json=make_request())
    student.post("/requests", json=make_request(course="4to_Basica", paralelo="A", studentDni=OTHER_STUDENT_DNI,
                                               studentName="Pedro Pablo Ruiz Diaz", payload={"subjects": ["Ciencias Naturales"]}))
    r = client.get("/requests", headers=auth).json()
    assert r["total"] == 2 and r["page"] == 1 and r["pageSize"] == 20 and r["totalPages"] == 1
    assert r["data"][0]["studentName"] == "Pedro Pablo Ruiz Diaz"  # más reciente primero
    assert client.get("/requests", headers=auth, params={"course": "4to_Basica"}).json()["total"] == 1
    assert client.get("/requests", headers=auth, params={"subject": "Inglés"}).json()["total"] == 1
    assert client.get("/requests", headers=auth, params={"paralelo": "A", "status": "en_revision"}).json()["total"] == 1
    assert client.get("/requests", headers=auth, params={"search": "PEDRO"}).json()["total"] == 1
    assert client.get("/requests", headers=auth, params={"search": "(.*"}).json()["total"] == 0
    p2 = client.get("/requests", headers=auth, params={"page": 2, "pageSize": 1}).json()
    assert p2["totalPages"] == 2 and len(p2["data"]) == 1
    bad = client.get("/requests", headers=auth, params={"pageSize": 500})
    assert bad.status_code == 400 and bad.json()["message"] == ["pageSize must not be greater than 200"]
    assert client.get("/requests", headers=auth, params={"page": "abc"}).json()["message"] == [
        "page must not be less than 1", "page must be an integer number"]
    assert client.get("/requests", headers=auth, params={"foo": 1}).json()["message"] == ["property foo should not exist"]


def test_object_id_invalido_400(client, auth):
    for method, url in [("GET", "/requests/abc"), ("PATCH", "/requests/abc/status"), ("GET", "/requests/abc/pdf"),
                        ("PUT", "/plazos/abc"), ("DELETE", "/plazos/abc")]:
        r = client.request(method, url, headers=auth, json={})
        assert r.status_code == 400 and r.json()["message"] == '"abc" no es un id válido.', (method, url)
    assert client.get(f"/requests/{FAKE_ID}", headers=auth).status_code == 404


def test_autoaprobacion_al_ver_detalle(client, student, aporte_activo, auth):
    rid = student.post("/requests", json=make_request()).json()["_id"]
    r = client.get(f"/requests/{rid}", headers=auth)
    assert r.json()["status"] == "aprobado"
    # Máquina de estados (C5): un ADMIN ya no puede devolver una aprobada a pendiente; solo el SUPERADMIN corrige.
    r = client.patch(f"/requests/{rid}/status", headers=auth, json={"status": "pendiente"})
    assert r.status_code == 409 and r.json()["code"] == "INVALID_TRANSITION"
    assert client.get(f"/requests/{rid}", headers=auth).json()["status"] == "aprobado"


def test_motivo_de_rechazo(client, student, aporte_activo, auth, mongo):
    rid = student.post("/requests", json=make_request()).json()["_id"]
    r = client.patch(f"/requests/{rid}/status", headers=auth, json={"status": "rechazado"}).json()
    assert r["status"] == "rechazado" and "rejectionReason" not in r
    r = client.patch(f"/requests/{rid}/status", headers=auth, json={"status": "rechazado", "reason": "Fuera de plazo"}).json()
    assert r["rejectionReason"] == "Fuera de plazo" and r["rejectedBy"] == "admin" and r["rejectedAt"].endswith("Z")
    # Negada es final para el ADMIN (C5): no puede pasar a aprobada; el motivo se conserva.
    r = client.patch(f"/requests/{rid}/status", headers=auth, json={"status": "aprobado"})
    assert r.status_code == 409 and r.json()["code"] == "INVALID_TRANSITION"
    assert mongo.requests.find_one({"_id": ObjectId(rid)})["rejectionReason"] == "Fuera de plazo"
    events = [e["eventType"] for e in mongo.request_events.find({"requestId": ObjectId(rid)}).sort("createdAt", 1)]
    assert events == ["REQUEST_CREATED", "STATUS_CHANGED", "STATUS_CHANGED"]
    bad = client.patch(f"/requests/{rid}/status", headers=auth, json={"status": "nope"})
    assert bad.status_code == 400


# ── PDF ──────────────────────────────────────────────────────────────────────
def test_pdf_preview(client, student):
    r = student.post("/requests/preview", json=make_request())
    assert r.status_code == 201 and r.content[:5] == b"%PDF-"
    assert r.headers["content-disposition"] == 'inline; filename="preview.pdf"'


def test_pdf_acceso_por_token_y_admin(client, student, aporte_activo, auth):
    created = student.post("/requests", json=make_request()).json()
    other = student.post("/requests", json=make_request(studentDni=OTHER_STUDENT_DNI, studentName="Pedro Pablo Ruiz Diaz")).json()
    rid, tok = created["_id"], created["pdfAccessToken"]
    assert client.get(f"/requests/{rid}/pdf").status_code == 401
    assert client.get(f"/requests/{rid}/pdf", params={"token": "x" * 48}).status_code == 401
    r = client.get(f"/requests/{rid}/pdf", params={"token": other["pdfAccessToken"]})
    assert r.status_code == 401 and r.json()["message"] == "No autorizado para acceder a este documento."
    r = client.get(f"/requests/{rid}/pdf", params={"token": tok})
    assert r.status_code == 200 and r.content[:5] == b"%PDF-"
    assert r.headers["content-disposition"] == f'attachment; filename="solicitud-{created["requestNumber"]}.pdf"'
    assert client.get(f"/requests/{other['_id']}/pdf", headers=auth).status_code == 200
    # descargar el PDF también auto-aprueba (comportamiento del original)
    assert client.get(f"/requests/{rid}", headers=auth).json()["status"] == "aprobado"


def test_pdf_contenido(client, student):
    from pypdf import PdfReader
    r = student.post("/requests/preview", json=make_request(course="9no"))
    text = PdfReader(io.BytesIO(r.content)).pages[0].extract_text()
    for fragment in ["N° BORRADOR", "Juan Carlos Perez Lopez", "Maria Jose Perez Lopez", "9no", "JUAN CARLOS PEREZ LOPEZ",
                     "CI: 0102030400", "Matemática", "Inglés"]:
        assert fragment in text, fragment
    # con la malla por curso se listan solo las materias elegidas
    assert "Acompañamiento integral en el Aula" not in text and "Ciencias Naturales" not in text


# ── catálogo ────────────────────────────────────────────────────────────────
def test_config_get_or_create(client, mongo):
    r = client.get("/solicitud-config/proceso_mejora").json()
    # todos los cursos habilitados por defecto (pedido de la institución)
    assert all(c["active"] == 1 for c in r["courses"]) and len(r["courses"]) == 13
    assert "courseSubjects" not in r and "allCoursesActivatedAt" not in r
    assert all(s["active"] == 1 for s in r["subjects"]) and len(r["subjects"]) == len(CURRICULUM_SUBJECTS)
    assert mongo.solicitudconfigs.count_documents({}) == 1
    client.get("/solicitud-config/proceso_mejora")
    assert mongo.solicitudconfigs.count_documents({}) == 1
    bad = client.get("/solicitud-config/otro")
    assert bad.status_code == 400 and bad.json()["message"] == "Validation failed (enum string is expected)"


def test_config_patch(client, auth):
    r = client.patch("/solicitud-config/proceso_mejora/courses", headers=auth, json={"name": "11vo", "active": 1})
    assert r.status_code == 404 and r.json()["message"] == 'Curso "11vo" no encontrado'
    r = client.patch("/solicitud-config/proceso_mejora/subjects", headers=auth, json={"name": "ECA", "active": 2})
    assert r.json()["message"] == ["active must be one of the following values: 0, 1"]
    r = client.patch("/solicitud-config/proceso_mejora/subjects", headers=auth, json={"name": "ECA", "active": True})
    assert r.status_code == 400
    r = client.patch("/solicitud-config/proceso_mejora/subjects", headers=auth, json={"name": "ECA", "active": 0}).json()
    assert next(s for s in r["subjects"] if s["name"] == "ECA")["active"] == 0


# ── plazos ──────────────────────────────────────────────────────────────────
def _anio(**kw):
    base = {"name": "2026-2027", "startDate": "2026-09-01", "endDate": "2027-07-31", "trimestres": []}
    base.update(kw)
    return base


@pytest.mark.parametrize("body,msg", [
    (_anio(startDate="2027-09-01"), '"2026-2027": la fecha de inicio no puede ser posterior a la de fin.'),
    (_anio(trimestres=[{"name": "T1", "startDate": "2026-08-01", "endDate": "2026-12-01"}]),
     '"T1" (trimestre) debe estar dentro de las fechas de el año "2026-2027".'),
    (_anio(trimestres=[{"name": "T1", "startDate": "2026-09-01", "endDate": "2026-12-01"},
                       {"name": "T2", "startDate": "2026-12-01", "endDate": "2027-03-01"}]),
     'Las fechas de "T1" y "T2" se solapan (los trimestres de "2026-2027").'),
    (_anio(trimestres=[{"name": "T1", "startDate": "2026-09-01", "endDate": "2026-12-01", "aportes": [
        {"name": "A1", "startDate": "2026-09-01", "endDate": "2026-10-15"},
        {"name": "A2", "startDate": "2026-10-10", "endDate": "2026-11-30"}]}]),
     'Las fechas de "A1" y "A2" se solapan (los aportes del trimestre "T1").'),
    (_anio(trimestres=[{"name": "T1", "startDate": "2026-09-01", "endDate": "2026-12-01", "aportes": [
        {"name": "A1", "startDate": "2026-09-01", "endDate": "2026-12-15"}]}]),
     '"A1" (aporte) debe estar dentro de las fechas de el trimestre "T1".'),
    (_anio(startDate="hola"), ["startDate must be a valid ISO 8601 date string"]),
])
def test_plazos_validaciones(client, auth, body, msg):
    r = client.post("/plazos", headers=auth, json=body)
    assert r.status_code == 400 and r.json()["message"] == msg


def test_plazos_crud_y_nombre_unico(client, auth, mongo):
    created = client.post("/plazos", headers=auth, json=_anio(trimestres=[{"name": "T1", "startDate": "2026-09-01",
                          "endDate": "2026-12-01", "aportes": [{"name": "A1", "startDate": "2026-09-01", "endDate": "2026-10-15"}]}])).json()
    doc = mongo.aniolectivos.find_one({"_id": ObjectId(created["_id"])})
    assert list(doc["trimestres"][0].keys()) == ["name", "startDate", "endDate", "aportes", "_id"]
    assert doc["startDate"] == datetime(2026, 9, 1)
    assert client.post("/plazos", headers=auth, json=_anio()).status_code == 409
    upd = client.put(f"/plazos/{created['_id']}", headers=auth, json=_anio(name="2026-2027 B")).json()
    assert upd["name"] == "2026-2027 B" and upd["trimestres"] == []
    assert len(client.get("/plazos", headers=auth).json()) == 1
    assert client.put(f"/plazos/{FAKE_ID}", headers=auth, json=_anio(name="Otro")).status_code == 404
    assert client.delete(f"/plazos/{created['_id']}", headers=auth).json()["_id"] == created["_id"]
    assert client.delete(f"/plazos/{created['_id']}", headers=auth).status_code == 404


def test_aporte_actual(client, aporte_activo):
    r = client.get("/plazos/aporte-actual").json()
    assert r["aporte"]["name"] == "Aporte test" and r["trimestre"]["name"] == "T" and r["anioLectivo"]["name"] == "Año test"


# ── padrón ──────────────────────────────────────────────────────────────────
def _xlsx(rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["IDCApellidos", "IDCEspecialidad", "IDCCurso", "IDCCedula"])
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_padron_importacion(client, auth, mongo):
    xl = _xlsx([["PEREZ LOPEZ MARIA", "", "Quinto de Básica B", "0105566770"],
                ["RUIZ DIAZ PEDRO", "Informática", "Primero Bach. F1", 912345675],
                ["", "", "5to B", "0101"], ["SIN CURSO", "", "", "0101010101"], ["MALA", "", "8vo A", "12ab"]])
    r = client.post("/padron/import", headers=auth, files={"file": ("p.xlsx", xl)})
    assert r.status_code == 201
    assert r.json() == {"total": 5, "creados": 2, "actualizados": 0, "errores": [
        {"fila": 4, "motivo": "IDCApellidos vacío."}, {"fila": 5, "motivo": "IDCCurso vacío."},
        {"fila": 6, "motivo": 'IDCCedula con formato inválido: "12ab".'}]}
    s = mongo.students.find_one({"fullName": "PEREZ LOPEZ MARIA"})
    assert "0105566770" not in str(s) and s["course"] == "5to" and "especialidad" not in s
    from app.shared.crypto.field_encryption import decrypt_field, hash_field
    assert decrypt_field(s["cedulaCifrada"]) == "0105566770" and s["cedulaHash"] == hash_field("0105566770")
    r = client.post("/padron/import", headers=auth, files={"file": ("p.XLSX", xl)})
    assert r.json()["actualizados"] == 2 and mongo.students.count_documents({}) == 2


def test_padron_errores_de_archivo(client, auth):
    assert client.post("/padron/import", headers=auth).json()["message"] == "No se recibió ningún archivo."
    r = client.post("/padron/import", headers=auth, files={"file": ("p.csv", b"a,b")})
    assert r.status_code == 400 and r.json()["message"] == "Solo se aceptan archivos .xlsx o .xls."
    r = client.post("/padron/import", headers=auth, files={"archivo": ("p.xlsx", b"x")})
    assert r.json()["message"] == "Unexpected field - archivo"
    r = client.post("/padron/import", headers=auth, files={"file": ("p.xlsx", b"0" * (5 * 1024 * 1024 + 1))})
    assert r.status_code == 413 and r.json()["message"] == "File too large"


def test_padron_sin_endpoint_de_lectura(client, auth):
    for url in ("/padron", "/padron/import", "/students"):
        assert client.get(url, headers=auth).status_code == 404


def test_padron_no_escribe_en_disco(client, auth, monkeypatch):
    import tempfile
    def boom(*a, **k):
        raise AssertionError("se intentó crear un archivo temporal")
    monkeypatch.setattr(tempfile, "SpooledTemporaryFile", boom)
    monkeypatch.setattr(tempfile, "NamedTemporaryFile", boom)
    monkeypatch.setattr(tempfile, "TemporaryFile", boom)
    big_rows = [[f"ALUMNO {i}", "", "5to A", f"01{i:08d}"] for i in range(3000)]
    r = client.post("/padron/import", headers=auth, files={"file": ("p.xlsx", _xlsx(big_rows))})
    assert r.status_code == 201 and r.json()["creados"] == 3000


# ── generales ───────────────────────────────────────────────────────────────
def test_404_estilo_nest(client):
    assert client.get("/nada").json() == {"message": "Cannot GET /nada", "error": "Not Found", "statusCode": 404}
    # Con token CSRF válido el 404 conserva el formato de Nest (sin token, el middleware responde 403 antes).
    assert Actor(client).post("/request-types").json()["message"] == "Cannot POST /request-types"


def test_cors_allowlist(client):
    ok = client.options("/request-types", headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "GET"})
    assert ok.headers.get("access-control-allow-origin") == "http://localhost:5173"
    bad = client.get("/request-types", headers={"Origin": "https://malicioso.example"})
    assert "access-control-allow-origin" not in bad.headers


def test_padron_excel_danado_400(client, auth):
    r = client.post("/padron/import", headers=auth, files={"file": ("p.xlsx", b"PK\x03\x04basura-no-es-zip")})
    assert r.status_code == 400 and r.json()["message"] == "No se pudo leer el archivo Excel."
