"""Panel SUPERADMIN, área del estudiante y seguridad de sesión (base de pruebas aislada)."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import openpyxl
import io
import pytest
from bson import ObjectId

from tests.conftest import (SA_EMAIL, OTHER_STUDENT_DNI, VALID_REP_DNI, VALID_STUDENT_DNI, Actor,
                            make_request, register_student)

FAKE = "0123456789abcdef01234567"


def _sa_paths(client):
    spec = client.app.openapi()
    out = []
    for path, ops in spec["paths"].items():
        if path.startswith("/superadmin"):
            for method in ops:
                out.append((method.upper(), path.replace("{id_}", FAKE)))
    return out


# ── autorización ──────────────────────────────────────────────────────────────
def test_barrido_autorizacion_superadmin(client, admin):
    paths = _sa_paths(client)
    assert len(paths) >= 40, "deben estar todas las rutas del panel"
    anon = Actor(client)
    stud = register_student(client)
    for method, url in paths:
        r = anon.request(method, url, json={})
        assert r.status_code == 401, (method, url, r.status_code)
        for who in (stud, admin):
            r = who.request(method, url, json={})
            assert r.status_code == 403 and r.json()["code"] == "AUTH_FORBIDDEN", (method, url, r.status_code, r.text)


def test_rol_inferior_sin_acceso_y_estudiante_fuera_del_panel_admin(client):
    stud = register_student(client)
    for url in ("/requests", "/plazos", "/superadmin/dashboard"):
        assert stud.get(url).status_code == 403
    assert stud.patch(f"/requests/{FAKE}/status", json={"status": "aprobado"}).status_code == 403


def test_cabeceras_seguridad_y_no_store(superadmin):
    r = superadmin.get("/superadmin/dashboard")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store" and r.headers["x-frame-options"] == "DENY"
    assert r.headers["x-content-type-options"] == "nosniff"


# ── usuarios ──────────────────────────────────────────────────────────────────
def _create(sa, role="ADMIN", email="admin2@uets.edu.ec", cedula="0104004009", name="Laura Elena Vintimilla Ortiz"):
    from app.shared.validators.cedula import validate_ecuadorian_dni
    if not validate_ecuadorian_dni(cedula):
        cedula = next(cedula[:9] + str(k) for k in range(10) if validate_ecuadorian_dni(cedula[:9] + str(k)))
    return sa.post("/superadmin/users", json={"email": email, "name": name, "cedula": cedula, "role": role})


def test_crear_usuario_validaciones_y_password_temporal(client, superadmin, mongo):
    r = superadmin.post("/superadmin/users", json={"email": "x@gmail.com", "name": "Ana Maria Lopez Vera", "cedula": VALID_REP_DNI, "role": "ADMIN"})
    assert r.status_code == 400 and "@uets.edu.ec" in r.json()["message"]
    r = superadmin.post("/superadmin/users", json={"email": "admin2@uets.edu.ec", "name": "Ana Maria Lopez Vera", "cedula": "0102030405", "role": "ADMIN"})
    assert r.status_code == 400 and "cédula" in r.json()["message"]
    r = superadmin.post("/superadmin/users", json={"email": "admin2@uets.edu.ec", "name": "Ana", "cedula": VALID_REP_DNI, "role": "ADMIN", "x": 1})
    assert r.status_code == 400
    r = _create(superadmin)
    assert r.status_code == 201, r.text
    body = r.json()
    temp = body["tempPassword"]
    assert len(temp) == 14 and temp.count("-") == 2
    doc = mongo.users.find_one({"email": "admin2@uets.edu.ec"})
    assert doc["forcePasswordChange"] and doc["passwordHash"].startswith("$2b$12$") and "cedula" not in doc
    assert temp not in str(doc) and temp not in str(list(mongo.user_events.find()))
    assert _create(superadmin, email="admin2@uets.edu.ec", cedula="0911111110").status_code == 409  # correo repetido
    assert _create(superadmin, email="otro2@uets.edu.ec").status_code == 409  # cédula repetida

    # contraseña temporal: login → cambio obligatorio; el resto de la API queda bloqueada
    a = Actor(client)
    r = a.post("/auth/login", json={"username": "admin2@uets.edu.ec", "password": temp})
    assert r.status_code == 201 and r.json()["redirectTo"] == "/cambiar-contrasena"
    r = a.get("/requests")
    assert r.status_code == 403 and r.json()["code"] == "PASSWORD_CHANGE_REQUIRED"
    r = a.post("/auth/change-password", json={"newPassword": "corta", "confirmPassword": "corta"})
    assert r.status_code == 400
    r = a.post("/auth/change-password", json={"newPassword": "Nueva-clave-segura-1", "confirmPassword": "Nueva-clave-segura-1"})
    assert r.status_code == 200 and "uets_session" not in a.jar
    a.login("admin2@uets.edu.ec", "Nueva-clave-segura-1")
    assert a.get("/requests").status_code == 200


def test_password_temporal_vencida(client, superadmin, mongo):
    temp = _create(superadmin).json()["tempPassword"]
    mongo.users.update_one({"email": "admin2@uets.edu.ec"}, {"$set": {"tempPasswordExpiresAt": datetime.now(timezone.utc) - timedelta(minutes=1)}})
    r = Actor(client).post("/auth/login", json={"username": "admin2@uets.edu.ec", "password": temp})
    assert r.status_code == 401 and "venció" in r.json()["message"]


def test_no_puede_cambiar_su_propio_rol_ni_desactivarse(superadmin, mongo):
    me = superadmin.user["id"]
    r = superadmin.post(f"/superadmin/users/{me}/role", json={"role": "ADMIN", "reason": "prueba de auto cambio"})
    assert r.status_code == 403
    assert mongo.user_events.find_one({"eventType": "PRIVILEGE_ESCALATION_BLOCKED"})
    assert superadmin.post(f"/superadmin/users/{me}/deactivate", json={"reason": "prueba de auto baja"}).status_code == 403
    assert superadmin.post(f"/superadmin/users/{me}/reset-password").status_code == 403


def test_ultimo_superadmin_protegido(client, superadmin, mongo):
    me = superadmin.user["id"]
    # un segundo SUPERADMIN intenta degradar al primero cuando es el único activo restante
    r = _create(superadmin, role="SUPERADMIN", email="sa2@uets.edu.ec", cedula="0105055051")
    sa2_id, temp = r.json()["user"]["id"], r.json()["tempPassword"]
    sa2 = Actor(client)
    sa2.login("sa2@uets.edu.ec", temp)
    sa2.post("/auth/change-password", json={"newPassword": "Clave-sa2-segura!", "confirmPassword": "Clave-sa2-segura!"})
    sa2.login("sa2@uets.edu.ec", "Clave-sa2-segura!")
    assert superadmin.post(f"/superadmin/users/{sa2_id}/deactivate", json={"reason": "baja de prueba controlada"}).status_code == 200
    r = sa2.post(f"/superadmin/users/{me}/role", json={"role": "ADMIN", "reason": "intento de dejar sin SA"})
    assert r.status_code in (401, 409)  # sa2 ya está desactivado: su sesión cae (revalidación en escrituras)
    mongo.users.update_one({"_id": ObjectId(sa2_id)}, {"$set": {"isActive": True}})
    mongo.users.update_one({"_id": ObjectId(me)}, {"$set": {"isActive": True}})
    # con dos SA activos, uno puede degradar al otro; luego el último no puede ser degradado
    sa2.login("sa2@uets.edu.ec", "Clave-sa2-segura!")
    assert superadmin.post(f"/superadmin/users/{sa2_id}/role", json={"role": "ADMIN", "reason": "rotación de responsables"}).status_code == 200
    assert mongo.users.count_documents({"role": "SUPERADMIN", "isActive": True}) == 1
    ev = mongo.user_events.find_one({"eventType": "USER_ROLE_CHANGED", "metadata.to": "ADMIN"})
    assert ev["metadata"]["from"] == "SUPERADMIN"
    # sa2 ahora es ADMIN: su sesión fue invalidada (versión de sesión) y no entra al panel
    assert sa2.get("/superadmin/dashboard").status_code in (401, 403)
    assert sa2.post("/superadmin/users", json={}).status_code in (401, 403)
    mongo.users.delete_one({"_id": ObjectId(sa2_id)})


def test_reset_password_invalida_sesiones(client, superadmin, mongo):
    r = _create(superadmin)
    uid, temp = r.json()["user"]["id"], r.json()["tempPassword"]
    a = Actor(client)
    a.login("admin2@uets.edu.ec", temp)
    a.post("/auth/change-password", json={"newPassword": "Clave-admin2-ok!", "confirmPassword": "Clave-admin2-ok!"})
    a.login("admin2@uets.edu.ec", "Clave-admin2-ok!")
    assert a.get("/requests").status_code == 200
    r = superadmin.post(f"/superadmin/users/{uid}/reset-password")
    assert r.status_code == 200 and r.json()["tempPassword"]
    assert a.patch(f"/requests/{FAKE}/status", json={"status": "aprobado"}).status_code == 401
    assert mongo.user_events.find_one({"eventType": "USER_PASSWORD_RESET", "targetId": uid})
    acc = superadmin.get(f"/superadmin/users/{uid}/access").json()
    assert acc["total"] >= 3


def test_desactivado_no_entra_mismo_mensaje(client, superadmin, mongo):
    r = _create(superadmin)
    uid, temp = r.json()["user"]["id"], r.json()["tempPassword"]
    superadmin.post(f"/superadmin/users/{uid}/deactivate", json={"reason": "salida de la institución"})
    r = Actor(client).post("/auth/login", json={"username": "admin2@uets.edu.ec", "password": temp})
    assert r.status_code == 401 and r.json()["message"] == "Usuario o contraseña incorrectos"
    assert superadmin.post(f"/superadmin/users/{uid}/reactivate").status_code == 200


# ── estudiantes ───────────────────────────────────────────────────────────────
def test_registro_institucional_y_cifrado(client, mongo):
    a = Actor(client)
    r = a.post("/auth/register", json={"email": "alguien@gmail.com", "password": "clave-larga-1", "confirmPassword": "clave-larga-1"})
    assert r.status_code == 400
    s = register_student(client)
    assert a.post("/auth/register", json={"email": f"est{VALID_STUDENT_DNI}.est@uets.edu.ec", "password": "clave-larga-1",
                                          "confirmPassword": "clave-larga-1"}).status_code == 409
    prof = mongo.student_profiles.find_one({})
    raw = str(prof) + str(list(mongo.representatives.find()))
    assert VALID_STUDENT_DNI not in raw and VALID_REP_DNI not in raw and "Maria Jose" not in raw and "0998765432" not in raw
    assert prof["completed"] is True and prof["padronMatch"] is False
    me = s.get("/me/profile").json()
    assert me["profile"]["cedula"] == VALID_STUDENT_DNI and me["representatives"][0]["isPrimary"]
    # otra cuenta no puede registrar la misma cédula
    b = Actor(client)
    b.post("/auth/register", json={"email": "otro.est@uets.edu.ec", "password": "clave-larga-1", "confirmPassword": "clave-larga-1"})
    r = b.put("/me/profile", json={"firstNames": "Maria Jose", "lastNames": "Perez Lopez", "cedula": VALID_STUDENT_DNI,
                                   "course": "5to", "paralelo": "B"})
    assert r.status_code == 409


def test_estudiante_solo_ve_y_cancela_lo_suyo(client, aporte_activo, mongo):
    a = register_student(client)
    b = register_student(client, OTHER_STUDENT_DNI, "Pedro Pablo Ruiz Diaz")
    r = a.post("/me/requests", json={"requestType": "proceso_mejora", "representativeLinkId": a.link_id,
                                     "payload": {"subjects": ["Matemática"]}})
    assert r.status_code == 201, r.text
    rid = r.json()["_id"]
    assert "pdfAccessToken" not in r.json()
    assert b.post(f"/me/requests/{rid}/cancel").status_code == 404
    assert b.get(f"/me/requests/{rid}/pdf").status_code == 404
    assert b.get(f"/requests/{rid}/pdf").status_code == 401
    assert a.get(f"/me/requests/{rid}/pdf").content[:5] == b"%PDF-"
    assert b.get("/me/requests").json()["data"] == []
    # el vínculo de otro estudiante no sirve
    r = b.post("/me/requests", json={"requestType": "proceso_mejora", "representativeLinkId": a.link_id, "payload": {"subjects": ["Computación"]}})
    assert r.status_code == 404
    r = a.post(f"/me/requests/{rid}/cancel")
    assert r.status_code == 200 and r.json()["status"] == "cancelado"
    assert a.post(f"/me/requests/{rid}/cancel").status_code == 409
    assert mongo.request_events.find_one({"requestId": ObjectId(rid), "eventType": "CANCELLED_BY_STUDENT"})


def test_formulario_original_exige_identidad_del_perfil(client, aporte_activo):
    a = register_student(client)
    r = a.post("/requests", json=make_request(studentDni=OTHER_STUDENT_DNI))
    assert r.status_code == 403 and r.json()["code"] == "IDENTITY_MISMATCH"


def test_limite_concurrente_atomico(client, superadmin, aporte_activo, mongo):
    assert superadmin.put("/superadmin/limits", json={"requestType": "proceso_mejora", "scope": "aporte", "maxRequests": 1}).status_code == 200
    a = register_student(client)
    subjects = [["Matemática"], ["Inglés"], ["Computación"], ["Enseñanza Religiosa Escolar"], ["Ciencias Naturales"], ["Estudios Sociales"], ["Educación Física"]]

    def go(i):
        return a.post("/me/requests", json={"requestType": "proceso_mejora", "representativeLinkId": a.link_id,
                                            "payload": {"subjects": subjects[i]}})

    with ThreadPoolExecutor(max_workers=5) as ex:
        codes = sorted(r.status_code for r in ex.map(go, range(5)))
    assert codes.count(201) == 1 and all(c in (400, 409) for c in codes if c != 201), codes
    assert mongo.requests.count_documents({"studentDni": VALID_STUDENT_DNI}) == 1
    # excepción individual con motivo: permite una más
    sid = a.user["id"]
    r = superadmin.post("/superadmin/limit-exceptions", json={"studentUserId": sid, "requestType": "proceso_mejora", "extra": 1,
                                                             "reason": "Caso de salud documentado"})
    assert r.status_code == 201
    assert go(5).status_code == 201
    assert go(6).status_code == 400
    assert mongo.user_events.find_one({"eventType": "LIMIT_EXCEPTION_GRANTED"})
    prev = superadmin.post("/superadmin/limits/preview", json={"requestType": "proceso_mejora", "scope": "aporte", "maxRequests": 1}).json()
    assert prev["over"] == 1 and prev["students"] == 1


def test_plazo_vencido_y_por_abrir(client, superadmin, aporte_activo):
    a = register_student(client)
    body = {"requestType": "proceso_mejora", "representativeLinkId": a.link_id, "payload": {"subjects": ["Computación"]}}
    now = datetime.now(timezone.utc)
    fmt = lambda d: d.astimezone(timezone.utc).isoformat()  # noqa: E731
    r = superadmin.put("/superadmin/deadlines", json={"requestType": "proceso_mejora", "periodKey": None,
                                                      "opensAt": fmt(now - timedelta(days=5)), "closesAt": fmt(now - timedelta(hours=1))})
    assert r.status_code == 200, r.text
    r = a.post("/me/requests", json=body)
    assert r.status_code == 400 and r.json()["code"] == "DEADLINE_CLOSED" and "hora de Ecuador" in r.json()["message"]
    superadmin.put("/superadmin/deadlines", json={"requestType": "proceso_mejora", "periodKey": None,
                                                  "opensAt": fmt(now + timedelta(days=1)), "closesAt": fmt(now + timedelta(days=3))})
    assert a.post("/me/requests", json=body).json()["code"] == "DEADLINE_NOT_OPEN"
    superadmin.put("/superadmin/deadlines", json={"requestType": "proceso_mejora", "periodKey": None,
                                                  "opensAt": fmt(now - timedelta(days=1)), "closesAt": fmt(now + timedelta(days=3))})
    assert a.post("/me/requests", json=body).status_code == 201
    r = superadmin.put("/superadmin/deadlines", json={"requestType": "proceso_mejora", "periodKey": None,
                                                      "opensAt": fmt(now), "closesAt": fmt(now - timedelta(days=1))})
    assert r.status_code == 400


# ── solicitudes ───────────────────────────────────────────────────────────────
def test_correccion_de_estado_con_motivo_y_auditoria(client, superadmin, admin, aporte_activo, mongo):
    a = register_student(client)
    rid = a.post("/me/requests", json={"requestType": "proceso_mejora", "representativeLinkId": a.link_id,
                                       "payload": {"subjects": ["Computación"]}}).json()["_id"]
    assert admin.patch(f"/requests/{rid}/status", json={"status": "rechazado", "reason": "Documentos incompletos"}).status_code == 200
    base = f"/superadmin/requests/{rid}"
    r = superadmin.post(f"{base}/correct-status", json={"expectedStatus": "rechazado", "status": "en_revision", "reason": "corto"})
    assert r.status_code == 400
    r = superadmin.post(f"{base}/correct-status", json={"expectedStatus": "pendiente", "status": "en_revision", "reason": "Reapertura por error de digitación"})
    assert r.status_code == 409 and r.json()["code"] == "STALE_STATE"
    r = superadmin.post(f"{base}/correct-status", json={"expectedStatus": "rechazado", "status": "aprobado", "reason": "No se permite de final a final"})
    assert r.status_code == 409 and r.json()["code"] == "INVALID_TRANSITION"
    r = superadmin.post(f"{base}/correct-status", json={"expectedStatus": "rechazado", "status": "en_revision", "reason": "Reapertura por error de digitación"})
    assert r.status_code == 200, r.text
    ev = mongo.request_events.find_one({"requestId": ObjectId(rid), "eventType": "STATUS_CORRECTED"})
    assert ev["comment"] == "Reapertura por error de digitación" and ev["fromStatus"] == "rechazado" and ev["actorRole"] == "SUPERADMIN"
    d = superadmin.get(base).json()
    assert [h["eventType"] for h in d["history"]] == ["REQUEST_CREATED", "STATUS_CHANGED", "STATUS_CORRECTED"]
    assert d["request"]["status"] == "en_revision" and "pdfAccessToken" not in d["request"]
    # el detalle del SUPERADMIN no auto-aprueba
    assert superadmin.get(base).json()["request"]["status"] == "en_revision"
    # responsable y plazo de atención
    staff = superadmin.get("/superadmin/requests/staff").json()
    adm = next(s for s in staff if s["role"] == "ADMIN")
    r = superadmin.post(f"{base}/assign", json={"userId": adm["id"]})
    assert r.status_code == 200, r.text
    assert r.json()["request"]["assignedName"]
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    assert superadmin.post(f"{base}/due", json={"dueAt": past}).json()["request"]["overdue"] is True
    lst = superadmin.get("/superadmin/requests", params={"due": "overdue"}).json()
    assert lst["total"] == 1
    assert superadmin.post(f"{base}/due", json={"dueAt": None}).json()["request"]["dueAt"] is None
    # exportación y PDF en memoria
    x = superadmin.get("/superadmin/requests/export", params={"status": "en_revision"})
    ws = openpyxl.load_workbook(io.BytesIO(x.content)).active
    assert ws.max_row == 2 and ws["A2"].value.startswith("PM-")
    assert superadmin.get(f"{base}/pdf").content[:5] == b"%PDF-"


def test_transaccion_compensa_si_falla_la_auditoria(client, superadmin, admin, aporte_activo, mongo, monkeypatch):
    a = register_student(client)
    rid = a.post("/me/requests", json={"requestType": "proceso_mejora", "representativeLinkId": a.link_id,
                                       "payload": {"subjects": ["Computación"]}}).json()["_id"]
    from app.modules.requests import workflow

    async def boom(*args, **kw):
        raise RuntimeError("auditoría caída")

    monkeypatch.setattr(workflow, "log_request_event", boom)
    client._transport.raise_server_exceptions = False
    try:
        r = superadmin.post(f"/superadmin/requests/{rid}/correct-status",
                            json={"expectedStatus": "en_revision", "status": "cancelado", "reason": "Prueba de atomicidad"})
    finally:
        client._transport.raise_server_exceptions = True
    assert r.status_code == 500
    assert mongo.requests.find_one({"_id": ObjectId(rid)})["status"] == "en_revision"


# ── errores, mantenimiento, configuración ─────────────────────────────────────
def test_errores_sin_traza_al_cliente(client, superadmin, mongo, monkeypatch):
    from app.modules.superadmin import monitor

    async def boom():
        raise RuntimeError("detalle interno secreto /srv/app.py")

    monkeypatch.setattr(monitor.system, "error_stats", boom)
    client._transport.raise_server_exceptions = False
    try:
        r = superadmin.get("/superadmin/dashboard")
    finally:
        client._transport.raise_server_exceptions = True
    assert r.status_code == 500 and r.json() == {"statusCode": 500, "message": "Internal server error"}
    assert "secreto" not in r.text and "Traceback" not in r.text
    monkeypatch.undo()
    err = mongo.system_errors.find_one({})
    assert "Traceback" in err["stack"] and "secreto" in err["message"]
    lst = superadmin.get("/superadmin/errors").json()
    assert lst["total"] == 1 and "stack" not in lst["rows"][0]
    eid = lst["rows"][0]["id"]
    assert "Traceback" in superadmin.get(f"/superadmin/errors/{eid}").json()["stack"]
    assert superadmin.post(f"/superadmin/errors/{eid}/resolve", json={"note": "corregido"}).json()["resolved"] is True
    assert superadmin.post(f"/superadmin/errors/{eid}/reopen").json()["resolved"] is False


def test_modo_mantenimiento(client, superadmin, admin, mongo):
    s = register_student(client)
    r = superadmin.put("/superadmin/settings/maintenance", json={"enabled": True, "message": "Volvemos a las 18:00"})
    assert r.status_code == 200 and r.json()["maintenance"]["enabled"]
    assert client.get("/system/maintenance").json() == {"enabled": True, "message": "Volvemos a las 18:00"}
    r = s.get("/me/profile")
    assert r.status_code == 503 and r.json()["code"] == "MAINTENANCE"
    assert Actor(client).post("/auth/register", json={"email": "n.est@uets.edu.ec", "password": "clave-larga-1",
                                                      "confirmPassword": "clave-larga-1"}).status_code == 503
    assert admin.get("/requests").status_code == 200
    assert superadmin.get("/superadmin/dashboard").json()["maintenance"]["enabled"]
    assert mongo.user_events.find_one({"eventType": "MAINTENANCE_MODE_ENABLED"})
    superadmin.put("/superadmin/settings/maintenance", json={"enabled": False})
    assert s.get("/me/profile").status_code == 200


def test_retencion_configurable(superadmin, mongo):
    r = superadmin.put("/superadmin/settings/params", json={"errorRetentionDays": 90, "dueDaysDefault": 5})
    assert r.status_code == 200 and r.json()["retention"]["errorRetentionDays"] == 90
    idx = mongo.system_errors.index_information()["ttl_createdAt"]
    assert idx["expireAfterSeconds"] == 90 * 86400
    assert superadmin.put("/superadmin/settings/params", json={"errorRetentionDays": 1}).status_code == 400
    superadmin.put("/superadmin/settings/params", json={"errorRetentionDays": 180, "dueDaysDefault": None})


def test_fichas_vinculos_e_inconsistencias(client, superadmin, mongo):
    register_student(client)
    Actor(client).post("/auth/register", json={"email": "sinperfil.est@uets.edu.ec", "password": "clave-larga-1", "confirmPassword": "clave-larga-1"})
    lst = superadmin.get("/superadmin/students", params={"q": "maria"}).json()
    assert lst["total"] == 1 and lst["rows"][0]["name"] == "Maria Jose Perez Lopez"
    assert superadmin.get("/superadmin/students", params={"q": VALID_STUDENT_DNI}).json()["total"] == 1
    sid = lst["rows"][0]["id"]
    det = superadmin.get(f"/superadmin/students/{sid}").json()
    assert det["profile"]["cedula"] == VALID_STUDENT_DNI and len(det["representatives"]) == 1
    assert mongo.user_events.find_one({"eventType": "SENSITIVE_DATA_VIEWED"})
    # segundo representante como principal: el anterior deja de serlo (un único principal)
    r = superadmin.post("/superadmin/links", json={"studentUserId": sid, "cedula": OTHER_STUDENT_DNI, "names": "Rosa Elena Lopez Vera",
                                                   "phone": "0987654321", "relationship": "Madre", "isPrimary": True})
    assert r.status_code == 201, r.text
    reps = r.json()["representatives"]
    assert sum(1 for x in reps if x["isPrimary"]) == 1 and next(x for x in reps if x["isPrimary"])["relationship"] == "Madre"
    old = next(x for x in reps if not x["isPrimary"])
    assert superadmin.post(f"/superadmin/links/{old['linkId']}/unlink", json={"reason": "corto"}).status_code == 400
    r = superadmin.post(f"/superadmin/links/{old['linkId']}/unlink", json={"reason": "Ya no es responsable legal"})
    assert len(r.json()["representatives"]) == 1
    assert mongo.student_representatives.count_documents({}) == 2  # baja lógica, sin borrado
    inc = superadmin.get("/superadmin/inconsistencies").json()
    groups = {g["key"]: g["rows"] for g in inc["groups"]}
    assert len(groups["no_profile"]) == 1 and len(groups["orphan_reps"]) == 1 and len(groups["not_in_padron"]) == 1
    reps_l = superadmin.get("/superadmin/representatives", params={"linked": "none"}).json()
    assert reps_l["total"] == 1


def test_dashboard_datos_reales(client, superadmin, aporte_activo):
    a = register_student(client)
    a.post("/me/requests", json={"requestType": "proceso_mejora", "representativeLinkId": a.link_id, "payload": {"subjects": ["Computación"]}})
    d = superadmin.get("/superadmin/dashboard").json()
    assert d["requests"]["byStatus"]["en_revision"] == 1 and d["requests"]["needsAction"] == 1
    assert d["students"]["total"] == 1 and d["students"]["profileCompleted"] == 1 and d["students"]["withRequestsThisPeriod"] == 1
    assert d["users"]["SUPERADMIN"]["active"] >= 1 and d["representatives"]["total"] == 1
    assert d["recentActivity"][0]["eventLabel"] == "Solicitud creada"
    ev = superadmin.get("/superadmin/audit/request-events").json()
    assert ev["total"] == 1
    assert superadmin.get("/superadmin/audit/users", params={"eventType": "STUDENT_REGISTERED"}).json()["total"] == 1


def test_sesion_y_logout(client, superadmin):
    s = superadmin.get("/auth/session").json()
    assert s["user"]["role"] == "SUPERADMIN" and s["user"]["home"] == "/superadmin/dashboard"
    superadmin.post("/auth/logout")
    assert "uets_session" not in superadmin.jar
    assert superadmin.get("/superadmin/dashboard").status_code == 401





def test_historial_para_admin(client, admin, aporte_activo):
    a = register_student(client)
    rid = a.post("/me/requests", json={"requestType": "proceso_mejora", "representativeLinkId": a.link_id,
                                       "payload": {"subjects": ["Computación"]}}).json()["_id"]
    assert a.get(f"/requests/{rid}/history").status_code == 403
    assert Actor(client).get(f"/requests/{rid}/history").status_code == 401
    admin.get(f"/requests/{rid}")  # solo visualiza
    admin.patch(f"/requests/{rid}/status", json={"status": "aprobado"})
    h = admin.get(f"/requests/{rid}/history").json()
    assert [e["eventType"] for e in h] == ["REQUEST_CREATED", "STATUS_CHANGED"]


# ── formulario de datos del estudiante (nombres, apellidos, cédula, curso, paralelo/especialidad) ──
@pytest.mark.parametrize("course,paralelo,specialty,ok", [
    ("1ro_Basica", "I", None, True), ("1ro_Basica", "J", None, False),
    ("4to_Basica", "G", None, True), ("7mo", "H", None, False),
    ("9no", "J", None, True),
    ("1ro_Bach", "E1", "Desarrollo de Software", True), ("2do_Bach", "F3", "Ciencias", True),
    ("3ro_Bach", "B", "Mecánica Industrial", True), ("3ro_Bach", "A1", "Mecatrónica", False),
    ("1ro_Bach", "A", None, False),
])
def test_paralelos_por_curso_y_especialidad(client, course, paralelo, specialty, ok):
    a = Actor(client)
    a.post("/auth/register", json={"email": "curso.test.est@uets.edu.ec", "password": "clave-larga-1", "confirmPassword": "clave-larga-1"})
    body = {"firstNames": "Maria Jose", "lastNames": "Perez Lopez", "cedula": VALID_STUDENT_DNI, "course": course, "paralelo": paralelo}
    if specialty:
        body["specialty"] = specialty
    r = a.put("/me/profile", json=body)
    assert (r.status_code == 200) is ok, r.text
    if ok:
        p = r.json()["profile"]
        assert p["names"] == "Maria Jose Perez Lopez" and p["firstNames"] == "Maria Jose" and p["specialty"] == specialty


def test_representante_con_correo_personal(client):
    a = Actor(client)
    a.post("/auth/register", json={"email": "rep.test.est@uets.edu.ec", "password": "clave-larga-1", "confirmPassword": "clave-larga-1"})
    a.put("/me/profile", json={"firstNames": "Maria Jose", "lastNames": "Perez Lopez", "cedula": VALID_STUDENT_DNI, "course": "5to", "paralelo": "B"})
    rep = {"firstNames": "Juan Carlos", "lastNames": "Perez Lopez", "cedula": VALID_REP_DNI, "phone": "0998765432"}
    r = a.post("/me/representatives", json={**rep, "email": "no-es-correo"})
    assert r.status_code == 400 and "inválido" in r.json()["message"]
    # los padres no tienen correo institucional: se acepta su correo personal
    r = a.post("/me/representatives", json={**rep, "email": "Juan.Perez@Gmail.com"})
    assert r.status_code == 201
    x = r.json()["representatives"][0]
    assert x["email"] == "juan.perez@gmail.com" and x["isPrimary"] and x["lastNames"] == "Perez Lopez"
    assert a.get("/auth/session").json()["user"]["profileCompleted"] is True
    # una vez registrado, el representante queda bloqueado para el estudiante
    r = a.put(f"/me/representatives/{x['linkId']}", json={"phone": "0991112233", "lastNames": "Perez Mora"})
    assert r.status_code == 403 and r.json()["code"] == "PROFILE_LOCKED"



def test_datos_bloqueados_y_desbloqueo_por_superadmin(client, superadmin, mongo):
    a = register_student(client)
    sid = a.user["id"]
    me = a.get("/me/profile").json()
    assert me["locks"] == {"student": True, "representative": True}
    body = {"firstNames": "Maria Jose", "lastNames": "Perez Mora", "cedula": VALID_STUDENT_DNI, "course": "5to", "paralelo": "C"}
    r = a.put("/me/profile", json=body)
    assert r.status_code == 403 and r.json()["code"] == "PROFILE_LOCKED" and "Departamento de Sistemas" in r.json()["message"]
    assert a.delete(f"/me/representatives/{a.link_id}").status_code == 403
    assert a.post("/me/representatives", json={"firstNames": "Rosa Elena", "lastNames": "Lopez Vera", "cedula": OTHER_STUDENT_DNI,
                                                "email": "rosa@uets.edu.ec", "phone": "0987654321"}).status_code == 403
    # el SUPERADMIN habilita la edición con motivo (solicitud presencial) y queda auditado
    assert superadmin.post(f"/superadmin/students/{sid}/unlock", json={"reason": "corto"}).status_code == 400
    r = superadmin.post(f"/superadmin/students/{sid}/unlock", json={"reason": "Solicitud presencial: apellido mal escrito"})
    assert r.status_code == 200 and r.json()["locks"]["student"] is False and r.json()["locks"]["representative"] is False
    assert mongo.user_events.find_one({"eventType": "PROFILE_UNLOCKED", "metadata.studentUserId": sid})
    assert a.get("/me/profile").json()["locks"] == {"student": False, "representative": False}
    r = a.put("/me/profile", json=body)
    assert r.status_code == 200 and r.json()["profile"]["names"] == "Maria Jose Perez Mora" and r.json()["profile"]["paralelo"] == "C"
    assert a.put("/me/profile", json=body).status_code == 403  # se vuelve a bloquear al guardar
    # representante: cambiar de persona (quitar y agregar) dentro de la ventana, luego se bloquea
    assert a.delete(f"/me/representatives/{a.link_id}").status_code == 200
    r = a.post("/me/representatives", json={"firstNames": "Rosa Elena", "lastNames": "Lopez Vera", "cedula": OTHER_STUDENT_DNI,
                                             "email": "rosa@uets.edu.ec", "phone": "0987654321"})
    assert r.status_code == 201 and r.json()["locks"]["representative"] is True
    # el SUPERADMIN también puede cancelar una habilitación
    superadmin.post(f"/superadmin/students/{sid}/unlock", json={"reason": "Prueba de cancelación de la habilitación", "scope": "student"})
    assert a.get("/me/profile").json()["locks"] == {"student": False, "representative": True}
    assert superadmin.post(f"/superadmin/students/{sid}/lock").status_code == 200
    assert a.get("/me/profile").json()["locks"]["student"] is True


def test_un_solo_nombre_es_valido(client, aporte_activo):
    """Estudiante y representante con un solo nombre (y sus dos apellidos) pueden registrarse y enviar solicitudes."""
    a = Actor(client)
    a.post("/auth/register", json={"email": "un.nombre.est@uets.edu.ec", "password": "clave-larga-1", "confirmPassword": "clave-larga-1"})
    r = a.put("/me/profile", json={"firstNames": "Ana", "lastNames": "Mora Vega", "cedula": VALID_STUDENT_DNI, "course": "5to", "paralelo": "A"})
    assert r.status_code == 200 and r.json()["profile"]["names"] == "Ana Mora Vega"
    r = a.post("/me/representatives", json={"firstNames": "Luis", "lastNames": "Mora Paz", "cedula": VALID_REP_DNI,
                                             "email": "luis.mora@uets.edu.ec", "phone": "0991234567"})
    assert r.status_code == 201
    link = r.json()["representatives"][0]["linkId"]
    r = a.post("/me/requests", json={"requestType": "proceso_mejora", "representativeLinkId": link, "payload": {"subjects": ["Computación"]}})
    assert r.status_code == 201, r.text
    assert r.json()["studentName"] == "Ana Mora Vega" and r.json()["representativeName"] == "Luis Mora Paz"


def test_eliminar_usuario(client, superadmin, aporte_activo, mongo):
    from tests.conftest import register_student
    a = register_student(client)
    uid = a.user["id"]
    r = a.post("/me/requests", json={"requestType": "proceso_mejora", "representativeLinkId": a.link_id,
                                     "payload": {"subjects": ["Matemática"]}})
    assert r.status_code == 201
    rid = r.json()["_id"]
    email = mongo.users.find_one({"_id": ObjectId(uid)})["email"]
    # motivo y confirmación obligatorios
    assert superadmin.post(f"/superadmin/users/{uid}/delete", json={"reason": "corto", "confirm": email}).status_code == 400
    r = superadmin.post(f"/superadmin/users/{uid}/delete", json={"reason": "cuenta creada por error", "confirm": "otro@uets.edu.ec"})
    assert r.status_code == 400 and "correo" in r.json()["message"]
    r = superadmin.post(f"/superadmin/users/{uid}/delete", json={"reason": "cuenta creada por error", "confirm": email.upper()})
    assert r.status_code == 200 and r.json() == {"deleted": True}
    oid = ObjectId(uid)
    assert mongo.users.count_documents({"_id": oid}) == 0
    assert mongo.student_profiles.count_documents({"userId": oid}) == 0
    assert mongo.student_representatives.count_documents({"studentUserId": oid}) == 0
    assert mongo.representatives.count_documents({}) == 0  # el representante no tenía otro estudiante
    assert mongo.requests.count_documents({"_id": ObjectId(rid)}) == 1  # la solicitud se conserva
    ev = mongo.user_events.find_one({"eventType": "USER_DELETED"})
    assert ev["metadata"]["requestsKept"] == 1 and ev["metadata"]["reason"] == "cuenta creada por error"
    # su sesión deja de valer (escrituras al instante; lecturas en la siguiente revalidación, ≤60 s)
    # y la misma cédula puede registrarse otra vez
    assert a.post("/me/requests", json={"requestType": "proceso_mejora", "representativeLinkId": a.link_id,
                                        "payload": {"subjects": ["Inglés"]}}).status_code == 401
    register_student(client, ip="10.0.0.9")
    assert superadmin.post(f"/superadmin/users/{uid}/delete", json={"reason": "cuenta creada por error", "confirm": email}).status_code == 404


def test_eliminar_protecciones(client, superadmin, mongo):
    me = superadmin.user["id"]
    r = superadmin.post(f"/superadmin/users/{me}/delete", json={"reason": "intento de autoeliminación", "confirm": SA_EMAIL})
    assert r.status_code == 403
    r = _create(superadmin, role="ADMIN")
    aid = r.json()["user"]["id"]
    mongo.requests.insert_one({"requestNumber": "PM-X", "assignedTo": ObjectId(aid), "status": "pendiente"})
    adm = Actor(client)
    assert adm.post("/superadmin/users/" + me + "/delete", json={"reason": "sin permisos de SA", "confirm": SA_EMAIL}).status_code in (401, 403)
    r = superadmin.post(f"/superadmin/users/{aid}/delete", json={"reason": "salida de la institución", "confirm": "admin2@uets.edu.ec"})
    assert r.status_code == 200
    assert mongo.requests.find_one({"requestNumber": "PM-X"})["assignedTo"] is None
