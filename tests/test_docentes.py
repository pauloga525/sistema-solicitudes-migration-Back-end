"""Panel docente: importación del distributivo, permisos por materia, seguimiento, avisos y correos."""
from __future__ import annotations

import io
from dataclasses import replace

import openpyxl
import pytest
from bson import ObjectId

from tests.conftest import VALID_REP_DNI, Actor, register_student
from tests.test_google import CLIENT, KEY, token

DIST = [("Etapa", "Especialidad", "Curso", "Grupo", "Materia", "Profesor"),
        ("EDUCACIÓN GENERAL BÁSICA", "Básica Media", "5", "B", "MATEMÁTICA", "PEREZ LOPEZ JUAN CARLOS"),
        ("EDUCACIÓN GENERAL BÁSICA", "Básica Media", "5", "B", "MATEMÁTICA", "PEREZ LOPEZ JUAN CARLOS"),  # repetida
        ("EDUCACIÓN GENERAL BÁSICA", "Básica Media", "5", "A", "MATEMÁTICA", "PEREZ LOPEZ JUAN CARLOS"),
        ("EDUCACIÓN GENERAL BÁSICA", "Básica Media", "5", "B", "COMPORTAMENTAL", "PEREZ LOPEZ JUAN CARLOS"),  # no evaluable
        ("EDUCACIÓN GENERAL BÁSICA", "Básica Media", "5", "B", "INGLÉS", "MORA DIAZ ANA LUCIA"),
        ("EDUCACIÓN GENERAL BÁSICA", "Básica Media", "5", "B", "LENGUA Y LITERATURA", "RUIZ CHACON BYRON GEOVANY"),
        ("BACHILLERATO", "Informática", "3 I", "E1", "MOD. SOPORTE TECNICO", "VERA OCHOA JORGE LUIS")]
EMAILS = [("First Name [Required]", "Last Name [Required]", "Email Address [Required]"),
          ("Juan Carlos", "Pérez López", "juanpl@uets.edu.ec"),
          ("Ana Lucía", "Mora Díaz", "anamd@uets.edu.ec"),
          ("Jorge Luis", "Vera Ochoa", "jorgevo@uets.edu.ec"),
          ("Estudiante", "Ejemplo", "otro.est@uets.edu.ec")]


def xlsx(rows, sheet="BD2") -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet
    for r in rows:
        ws.append(list(r))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


@pytest.fixture
def google(client, monkeypatch):
    from app.core import config
    from app.modules.auth import router as auth_router
    s = replace(config.get_settings(), google_client_id=CLIENT)
    monkeypatch.setattr(auth_router, "get_settings", lambda: s)
    client.app.state.google_key_resolver = lambda _t: KEY.public_key()
    yield
    del client.app.state.google_key_resolver


def importar(sa, dist=DIST, emails=EMAILS, **confirm):
    r = sa.post("/superadmin/teachers/import/preview",
                files={"distributivo": ("d.xlsx", xlsx(dist)), "correos": ("c.xlsx", xlsx(emails, "Hoja1"))})
    assert r.status_code == 201, r.text
    prev = r.json()
    r = sa.post("/superadmin/teachers/import/confirm", json={"draftId": prev["draftId"], **confirm})
    assert r.status_code == 200, r.text
    return prev, r.json()


def docente(client, email, sub):
    a = Actor(client)
    r = a.post("/auth/google", json={"credential": token(email=email, sub=sub)})
    assert r.status_code == 200, r.text
    assert r.json()["user"]["role"] == "TEACHER" and r.json()["redirectTo"] == "/docente"
    return a


def test_importacion_vista_previa_y_confirmacion(client, superadmin, mongo):
    prev, res = importar(superadmin, exclude=["RUIZ CHACON BYRON GEOVANY"])
    s = prev["summary"]
    assert s["docentes"] == 4 and s["asignaciones"] == 5 and s["sin_correo"] == 1 and s["excluidas"] == 1
    t = {x["nameKey"]: x for x in prev["teachers"]}
    assert t["PEREZ LOPEZ JUAN CARLOS"]["email"] == "juanpl@uets.edu.ec" and t["PEREZ LOPEZ JUAN CARLOS"]["name"] == "Juan Carlos Pérez López"
    assert t["RUIZ CHACON BYRON GEOVANY"]["match"] == "sin_correo"
    assert res["importados"] == 3 and res["excluidos"] == 1 and res["cuentas"]["creada"] == 3
    assert mongo.teachers.count_documents({}) == 3
    u = mongo.users.find_one({"email": "jorgevo@uets.edu.ec"})
    assert u["role"] == "TEACHER" and u["passwordHash"] is None
    a = mongo.teacher_assignments.find_one({"subject": "Mod. Soporte Técnico"})
    assert a["course"] == "3ro_Bach" and a["specialty"] == "Desarrollo de Software" and a["paralelo"] == "E1"
    # nunca se lee la hoja de estudiantes: un archivo sin BD2 se rechaza
    r = superadmin.post("/superadmin/teachers/import/preview",
                        files={"distributivo": ("d.xlsx", xlsx(DIST, "BD1")), "correos": ("c.xlsx", xlsx(EMAILS))})
    assert r.status_code == 400 and "BD2" in r.json()["message"]
    # reimportar no duplica: desactiva lo anterior y agrega lo nuevo
    importar(superadmin, exclude=["RUIZ CHACON BYRON GEOVANY"])
    assert mongo.teachers.count_documents({}) == 3
    assert mongo.teacher_assignments.count_documents({"active": True}) == 4
    # lista y correo puesto a mano
    lst = superadmin.get("/superadmin/teachers").json()
    assert lst["totals"]["activos"] == 3 and lst["total"] == 3


def test_correo_manual_y_permisos(client, superadmin, mongo):
    importar(superadmin)  # Ruiz entra sin correo
    tid = str(mongo.teachers.find_one({"nameKey": "RUIZ CHACON BYRON GEOVANY"})["_id"])
    assert superadmin.patch(f"/superadmin/teachers/{tid}/email", json={"email": "x@gmail.com"}).status_code == 400
    assert superadmin.patch(f"/superadmin/teachers/{tid}/email", json={"email": "byron.est@uets.edu.ec"}).status_code == 400
    r = superadmin.patch(f"/superadmin/teachers/{tid}/email", json={"email": "byronrc@uets.edu.ec"})
    assert r.status_code == 200 and r.json()["account"] == "creada"
    st = register_student(client)
    assert st.get("/superadmin/teachers").status_code == 403
    assert st.get("/teacher/items").status_code == 403


def test_flujo_docente_completo(client, superadmin, google, aporte_activo, mongo):
    importar(superadmin, exclude=["RUIZ CHACON BYRON GEOVANY"])
    st = register_student(client)  # 5to B
    body = {"requestType": "proceso_mejora", "representativeLinkId": st.link_id, "payload": {"subjects": ["Matemática", "Inglés"]}}
    r = st.post("/me/requests", json=body)
    assert r.status_code == 201
    rid = r.json()["_id"]
    # cada docente recibe su aviso y su correo (sin SMTP configurado queda registrado, sin enviar)
    juan = mongo.users.find_one({"email": "juanpl@uets.edu.ec"})
    assert mongo.notifications.count_documents({"userId": juan["_id"], "kind": "nueva"}) == 1
    out = mongo.email_outbox.find_one({"to": "juanpl@uets.edu.ec"})
    assert out["status"] == "sin_configurar" and "Matemática" in out["subject"]

    t = docente(client, "juanpl@uets.edu.ec", "g-juan")
    items = t.get("/teacher/items").json()
    assert [i["subject"] for i in items] == ["Matemática"] and items[0]["isNew"] and items[0]["track"] == "por_atender"
    assert items[0]["studentName"] == "Maria Jose Perez Lopez" and items[0]["paralelo"] == "B"
    subj = {s["key"]: s for s in t.get("/teacher/subjects").json()}
    assert subj["5to|B|Matemática"]["porAtender"] == 1 and subj["5to|A|Matemática"]["total"] == 0
    assert t.get("/teacher/me").json()["email"] == "juanpl@uets.edu.ec"
    assert len(t.get("/teacher/notifications").json()) == 1

    # otro docente no ve ni puede tocar la materia de Juan
    ana = docente(client, "anamd@uets.edu.ec", "g-ana")
    ana_items = ana.get("/teacher/items").json()
    assert [i["subject"] for i in ana_items] == ["Inglés"]
    r = ana.post("/teacher/items/track", json={"ids": [items[0]["id"]], "track": "evaluado"})
    assert r.status_code == 403

    # programar: aviso al estudiante y correo con copia al representante
    r = t.post("/teacher/items/track", json={"ids": [items[0]["id"]], "track": "programada", "scheduledAt": "2026-12-02T09:30",
                                              "note": "Aula 12"})
    assert r.status_code == 200, r.text
    up = r.json()[0]
    assert up["track"] == "programada" and up["scheduledAt"] == "2026-12-02T09:30" and up["note"] == "Aula 12" and not up["isNew"]
    n = mongo.notifications.find_one({"userId": ObjectId(st.user["id"]), "kind": "programada"})
    assert n and "02/12/2026 09:30" in n["body"] and "Juan Carlos Pérez López" in n["body"]
    mail = mongo.email_outbox.find_one({"context.kind": "programada"})
    assert mail["to"] == [st.user["email"]]
    assert mail["cc"] == [f"rep{VALID_REP_DNI}@uets.edu.ec"]
    mine = st.get("/me/requests").json()["data"][0]
    assert mine["evaluations"] == [{"subject": "Matemática", "track": "programada", "scheduledAt": "2026-12-02T09:30",
                                    "note": "Aula 12", "missedCount": 0, "teacher": "Juan Carlos Pérez López"}]
    assert st.get("/me/notifications").json()[0]["kind"] == "programada"
    assert st.post("/me/notifications/read", json={"ids": "all"}).json()["updated"] == 1
    # volver a guardar la misma fecha no repite el aviso
    t.post("/teacher/items/track", json={"ids": [items[0]["id"]], "track": "programada", "scheduledAt": "2026-12-02T09:30"})
    assert mongo.email_outbox.count_documents({"context.kind": "programada"}) == 1

    # al programar quedó aprobada; el SUPERADMIN la reabre y el estudiante la cancela:
    # los docentes reciben el aviso y ya no se puede marcar
    assert mongo.requests.find_one({"_id": ObjectId(rid)})["status"] == "aprobado"
    r = superadmin.post(f"/superadmin/requests/{rid}/correct-status",
                        json={"expectedStatus": "aprobado", "status": "en_revision", "reason": "Reabrir para la prueba"})
    assert r.status_code == 200, r.text
    assert st.post(f"/me/requests/{rid}/cancel").status_code == 200
    assert mongo.notifications.count_documents({"userId": juan["_id"], "kind": "cancelada"}) == 1
    r = t.post("/teacher/items/track", json={"ids": [items[0]["id"]], "track": "evaluado"})
    assert r.json()[0]["track"] == "programada"  # cerrada: no cambia
    assert t.post("/teacher/notifications/read", json={"ids": "all"}).json()["updated"] >= 1


def test_envio_smtp_configurado(client, superadmin, aporte_activo, mongo, monkeypatch):
    import asyncio

    from app.core import config
    from app.modules.mail import service as mail
    s = replace(config.get_settings(), mail_smtp_host="smtp.gmail.com", mail_from="notificaciones@uets.edu.ec")
    monkeypatch.setattr(mail, "get_settings", lambda: s)
    sent = []
    monkeypatch.setattr(mail, "sender", lambda msg: sent.append(msg))
    importar(superadmin, exclude=["RUIZ CHACON BYRON GEOVANY"])
    st = register_student(client)
    st.post("/me/requests", json={"requestType": "proceso_mejora", "representativeLinkId": st.link_id,
                                  "payload": {"subjects": ["Matemática"]}})
    for _ in range(50):
        if mongo.email_outbox.find_one({"status": "enviado"}):
            break
        asyncio.run(asyncio.sleep(0.05))
    doc = mongo.email_outbox.find_one({"to": "juanpl@uets.edu.ec"})
    assert doc["status"] == "enviado" and sent and sent[0]["To"] == "juanpl@uets.edu.ec"
    assert "Sistema de Solicitudes UETS" in sent[0]["From"]


def test_rol_docente_manual_y_materias(client, superadmin, google, aporte_activo, mongo):
    """El SUPERADMIN da el rol de docente a un administrador y le asigna una materia a mano para probar."""
    r = superadmin.post("/superadmin/users", json={"email": "nuevo.colega@uets.edu.ec", "name": "Colega De Prueba",
                                                   "cedula": "0109876540", "role": "ADMIN"})
    assert r.status_code == 201, r.text
    uid = r.json()["user"]["id"]
    assert superadmin.get("/superadmin/users?role=staff").json()["total"] >= 2
    r = superadmin.post(f"/superadmin/users/{uid}/role", json={"role": "TEACHER", "reason": "Pruebas del panel docente"})
    assert r.status_code == 200, r.text
    t = mongo.teachers.find_one({"email": "nuevo.colega@uets.edu.ec"})
    assert t["manual"] and t["active"] and mongo.users.find_one({"_id": ObjectId(uid)})["teacherId"] == t["_id"]
    tid = str(t["_id"])
    cat = superadmin.get("/superadmin/teachers/catalog").json()
    assert any(c["course"] == "5to" for c in cat)
    bad = superadmin.post(f"/superadmin/teachers/{tid}/assignments", json={"course": "5to", "paralelo": "Z", "subject": "Matemática"})
    assert bad.status_code == 400
    r = superadmin.post(f"/superadmin/teachers/{tid}/assignments", json={"course": "5to", "paralelo": "B", "subject": "Matemática"})
    assert r.status_code == 200 and r.json()["assignments"][0]["manual"] and r.json()["account"]["role"] == "TEACHER"
    # una importación no quita al docente manual ni su materia
    importar(superadmin, exclude=["RUIZ CHACON BYRON GEOVANY"])
    assert mongo.teachers.find_one({"_id": t["_id"]})["active"]
    assert mongo.teacher_assignments.count_documents({"teacherId": t["_id"], "active": True}) == 1
    # recibe el aviso de la solicitud junto con el docente del distributivo
    st = register_student(client)
    st.post("/me/requests", json={"requestType": "proceso_mejora", "representativeLinkId": st.link_id,
                                  "payload": {"subjects": ["Matemática"]}})
    assert mongo.notifications.count_documents({"userId": ObjectId(uid), "kind": "nueva"}) == 1
    # quitar la materia y devolverle el rol de administrador
    r = superadmin.post(f"/superadmin/teachers/{tid}/assignments/remove", json={"course": "5to", "paralelo": "B", "subject": "Matemática"})
    assert r.status_code == 200 and r.json()["assignments"] == []
    assert superadmin.post(f"/superadmin/users/{uid}/role", json={"role": "ADMIN", "reason": "Fin de las pruebas"}).status_code == 200
    assert not mongo.teachers.find_one({"_id": t["_id"]})["active"]


def test_seguimiento_para_vicerrectorado(client, superadmin, admin, google, aporte_activo, mongo):
    importar(superadmin, exclude=["RUIZ CHACON BYRON GEOVANY"])
    st = register_student(client)  # 5to B: Lengua y literatura queda sin docente (Ruiz excluido)
    r = st.post("/me/requests", json={"requestType": "proceso_mejora", "representativeLinkId": st.link_id,
                                      "payload": {"subjects": ["Matemática", "Lengua y Literatura"]}})
    assert r.status_code == 201, r.text
    rid = r.json()["_id"]
    snap = mongo.teacher_tracking.find_one({"requestId": ObjectId(rid), "subject": "Matemática"})
    assert snap["assignedTeachers"][0]["name"] == "Juan Carlos Pérez López"
    lst = admin.get("/evaluations").json()
    est = {x["subject"]: x for x in lst["rows"]}
    assert est["Matemática"]["estado"] == "por_atender" and est["Matemática"]["teacherNotified"]
    assert est["Lengua y Literatura"]["estado"] == "sin_docente" and lst["totals"]["sin_docente"] == 1
    t = docente(client, "juanpl@uets.edu.ec", "g-juan")
    iid = t.get("/teacher/items").json()[0]["id"]
    t.post("/teacher/items/track", json={"ids": [iid], "track": "programada", "scheduledAt": "2026-12-02T09:30"})
    t.post("/teacher/items/track", json={"ids": [iid], "track": "no_asistio", "note": "No asistió"})
    exp = admin.get(f"/evaluations/request/{rid}").json()
    m = next(b for b in exp["subjects"] if b["subject"] == "Matemática")
    assert m["estado"] == "no_asistio" and m["scheduledAt"] == "2026-12-02T09:30" and m["doneAt"]
    kinds = [e["type"] for e in exp["timeline"]]
    assert {"solicitud", "docente", "aviso", "correo"} <= set(kinds)
    assert sum(1 for e in exp["timeline"] if e["type"] == "docente") == 2
    assert admin.get("/evaluations?estado=no_asistio").json()["total"] == 1
    csv_ = admin.get("/evaluations/export")
    assert csv_.status_code == 200 and "No se presentó" in csv_.text and "text/csv" in csv_.headers["content-type"]
    # solo administración
    assert st.get("/evaluations").status_code == 403 and t.get("/evaluations").status_code == 403
    # el estudiante ve que no se presentó
    ev = st.get("/me/requests").json()["data"][0]["evaluations"]
    assert {e["subject"]: e["track"] for e in ev}["Matemática"] == "no_asistio"


def test_materia_de_prueba(client, superadmin, google, aporte_activo, mongo):
    """La materia de prueba solo la ven las cuentas marcadas como prueba y no se mezcla con la malla."""
    from app.modules.curriculum import TEST_SUBJECT
    st = register_student(client)  # 5to B
    assert TEST_SUBJECT not in st.get("/me/subjects").json()["subjects"]
    body = {"requestType": "proceso_mejora", "representativeLinkId": st.link_id, "payload": {"subjects": [TEST_SUBJECT]}}
    assert st.post("/me/requests", json=body).status_code == 400  # sin marcar, no puede pedirla
    r = superadmin.post(f"/superadmin/students/{st.user['id']}/test-account", json={"enabled": True})
    assert r.status_code == 200 and superadmin.get(f"/superadmin/students/{st.user['id']}").json()["user"]["testAccount"]
    assert st.get("/me/subjects").json()["subjects"][-1] == TEST_SUBJECT
    # docente manual con la materia de prueba
    r = superadmin.post("/superadmin/users", json={"email": "probador@uets.edu.ec", "name": "Docente De Prueba",
                                                   "cedula": "0111222337", "role": "TEACHER"})
    assert r.status_code == 201, r.text
    t = mongo.teachers.find_one({"email": "probador@uets.edu.ec"})
    r = superadmin.post(f"/superadmin/teachers/{t['_id']}/assignments", json={"course": "5to", "paralelo": "B", "subject": TEST_SUBJECT})
    assert r.status_code == 200, r.text
    r = st.post("/me/requests", json=body)
    assert r.status_code == 201, r.text
    req = mongo.requests.find_one({"_id": ObjectId(r.json()["_id"])})
    assert req["isTest"] is True
    assert mongo.notifications.count_documents({"userId": t["userId"], "kind": "nueva", "subject": TEST_SUBJECT}) == 1
    assert st.get(f"/me/requests/{r.json()['_id']}/pdf").status_code == 200
    # no aparece en la configuración de materias del trámite
    cfg = superadmin.get("/solicitud-config/proceso_mejora").json()
    assert TEST_SUBJECT not in str(cfg)


def test_eliminar_solicitud_superadmin(client, superadmin, admin, aporte_activo, mongo):
    importar(superadmin, exclude=["RUIZ CHACON BYRON GEOVANY"])
    st = register_student(client)
    body = {"requestType": "proceso_mejora", "representativeLinkId": st.link_id, "payload": {"subjects": ["Matemática"]}}
    r = st.post("/me/requests", json=body)
    rid, num = r.json()["_id"], r.json()["requestNumber"]
    assert st.post("/me/requests", json=body).status_code in (400, 409)  # misma materia activa: duplicada
    assert mongo.notifications.count_documents({"requestId": ObjectId(rid)}) >= 1
    url = f"/superadmin/requests/{rid}/delete"
    assert admin.post(url, json={"reason": "Prueba del sistema", "confirm": num}).status_code == 403
    assert superadmin.post(url, json={"reason": "Prueba del sistema", "confirm": "PM-0000"}).status_code == 400
    r = superadmin.post(url, json={"reason": "Solicitud enviada en pruebas", "confirm": num.lower()})
    assert r.status_code == 200, r.text
    assert mongo.requests.count_documents({"_id": ObjectId(rid)}) == 0
    assert mongo.notifications.count_documents({"requestId": ObjectId(rid)}) == 0
    assert mongo.teacher_tracking.count_documents({"requestId": ObjectId(rid)}) == 0
    assert mongo.request_events.count_documents({"requestId": ObjectId(rid), "eventType": "REQUEST_DELETED"}) == 1
    ev = mongo.user_events.find_one({"eventType": "REQUEST_DELETED"})
    assert ev["metadata"]["requestNumber"] == num and ev["metadata"]["subjects"] == ["Matemática"]
    assert st.post("/me/requests", json=body).status_code == 201  # ya puede volver a enviarla
    assert superadmin.post(url, json={"reason": "Solicitud enviada en pruebas", "confirm": num}).status_code == 404


def test_config_correo_smtp_465_y_nombres_alternos(monkeypatch):
    from app.core import config
    for k in ("MAIL_SMTP_HOST", "MAIL_SMTP_PORT", "MAIL_SMTP_USER", "MAIL_SMTP_PASSWORD", "MAIL_FROM_NAME"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SMTP_HOST", "smtp.gmail.com")
    monkeypatch.setenv("SMTP_PORT", "465")
    monkeypatch.setenv("SMTP_USER", "noreply@uets.edu.ec")
    monkeypatch.setenv("SMTP_PASS", "clave-de-prueba")
    monkeypatch.setenv("MAIL_FROM", "Sistema UETS <noreply@uets.edu.ec>")
    monkeypatch.setenv("MAIL_REPLY_TO", "secretaria@uets.edu.ec")
    m = config._mail_settings()
    assert m["mail_smtp_host"] == "smtp.gmail.com" and m["mail_smtp_port"] == 465 and m["mail_smtp_password"] == "clave-de-prueba"
    assert m["mail_from"] == "noreply@uets.edu.ec" and m["mail_from_name"] == "Sistema UETS" and m["mail_reply_to"] == "secretaria@uets.edu.ec"
    # 465 usa SSL directo, sin STARTTLS
    from app.modules.mail import service as mail
    s = replace(config.get_settings(), **m)
    monkeypatch.setattr(mail, "get_settings", lambda: s)
    calls = []

    class FakeSSL:
        def __init__(self, host, port, timeout, context): calls.append(("ssl", host, port))
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def starttls(self, **k): calls.append("starttls")
        def login(self, u, p): calls.append(("login", u))
        def send_message(self, msg): calls.append(("send", msg["Reply-To"]))
    monkeypatch.setattr(mail.smtplib, "SMTP_SSL", FakeSSL)
    from email.message import EmailMessage
    msg = EmailMessage()
    msg["Reply-To"] = s.mail_reply_to
    mail._smtp_send(msg)
    assert calls == [("ssl", "smtp.gmail.com", 465), ("login", "noreply@uets.edu.ec"), ("send", "secretaria@uets.edu.ec")]


def test_correo_de_prueba_y_reintento(client, superadmin, admin, aporte_activo, mongo, monkeypatch):
    import asyncio
    import smtplib

    from app.core import config
    from app.modules.mail import service as mail
    assert superadmin.get("/superadmin/mail/status").json()["config"]["enabled"] is False
    assert superadmin.post("/superadmin/mail/test", json={"to": "yo@uets.edu.ec"}).json()["error"] == "sin_configurar"
    s = replace(config.get_settings(), mail_smtp_host="smtp.gmail.com", mail_smtp_port=465, mail_from="noreply@uets.edu.ec",
                mail_smtp_user="noreply@uets.edu.ec", mail_smtp_password="x")
    monkeypatch.setattr(mail, "get_settings", lambda: s)

    def bad(msg):
        raise smtplib.SMTPAuthenticationError(535, b"bad")
    monkeypatch.setattr(mail, "sender", bad)
    r = superadmin.post("/superadmin/mail/test", json={"to": "yo@uets.edu.ec"}).json()
    assert r["ok"] is False and r["error"] == "SMTPAuthenticationError" and "contraseña de aplicación" in r["hint"]
    st = superadmin.get("/superadmin/mail/status").json()
    assert st["config"]["mode"] == "SSL" and st["config"]["passwordSet"] is True and "password" not in str(st).lower().replace("passwordset", "")
    # un correo fallido se ve con su causa en el expediente y se puede reintentar
    importar(superadmin, exclude=["RUIZ CHACON BYRON GEOVANY"])
    stu = register_student(client)
    rid = stu.post("/me/requests", json={"requestType": "proceso_mejora", "representativeLinkId": stu.link_id,
                                         "payload": {"subjects": ["Matemática"]}}).json()["_id"]
    for _ in range(50):
        if mongo.email_outbox.find_one({"status": "fallido"}):
            break
        asyncio.run(asyncio.sleep(0.05))
    ev = [e for e in admin.get(f"/evaluations/request/{rid}").json()["timeline"] if e["type"] == "correo"][0]
    assert ev["mailStatus"] == "fallido" and "clave" in ev["mailError"]
    sent = []
    monkeypatch.setattr(mail, "sender", lambda m: sent.append(m))
    assert superadmin.post("/superadmin/mail/retry-failed").json()["retried"] >= 1
    for _ in range(50):
        if mongo.email_outbox.find_one({"status": "enviado"}):
            break
        asyncio.run(asyncio.sleep(0.05))
    assert sent and mongo.email_outbox.find_one({"to": "juanpl@uets.edu.ec"})["status"] == "enviado"
    assert admin.get("/superadmin/mail/status").status_code == 403


def test_correo_al_representante_al_enviar(client, superadmin, aporte_activo, mongo):
    importar(superadmin, exclude=["RUIZ CHACON BYRON GEOVANY"])
    st = register_student(client)
    r = st.post("/me/requests", json={"requestType": "proceso_mejora", "representativeLinkId": st.link_id,
                                      "payload": {"subjects": ["Matemática", "Inglés"]}})
    assert r.status_code == 201
    # recién enviada: ninguna materia tiene fecha ni el estudiante recibe aviso de evaluación
    mine = st.get("/me/requests").json()["data"][0]
    assert mine["evaluations"] == [] and st.get("/me/notifications").json() == []
    assert all(t["track"] == "por_atender" and t.get("scheduledAt") is None
               for t in mongo.teacher_tracking.find({"requestId": ObjectId(r.json()["_id"])}))
    m = mongo.email_outbox.find_one({"context.kind": "solicitud_creada"})
    assert m["to"] == [f"rep{VALID_REP_DNI}@uets.edu.ec"] and "Maria Jose Perez Lopez" in m["subject"]
    for txt in ("Maria Jose Perez Lopez", "5to de Básica, paralelo B", "Materias: Matemática, Inglés", r.json()["requestNumber"],
                "Fecha de la solicitud: "):
        assert txt in m["text"], txt


def test_docente_abre_la_solicitud(client, superadmin, google, aporte_activo, mongo):
    importar(superadmin, exclude=["RUIZ CHACON BYRON GEOVANY"])
    st = register_student(client)
    st.post("/me/requests", json={"requestType": "proceso_mejora", "representativeLinkId": st.link_id,
                                  "payload": {"subjects": ["Matemática", "Inglés"]}})
    t = docente(client, "juanpl@uets.edu.ec", "g-juan")
    iid = t.get("/teacher/items").json()[0]["id"]
    d = t.get(f"/teacher/items/{iid}").json()
    assert d["subject"] == "Matemática" and d["campus"] and d["history"][0]["title"].startswith("Solicitud enviada")
    assert "studentDni" not in d and "representativeDni" not in d and d["studentNotice"] is None
    t.post("/teacher/items/track", json={"ids": [iid], "track": "programada", "scheduledAt": "2026-12-02T09:30"})
    d = t.get(f"/teacher/items/{iid}").json()
    assert d["track"] == "programada" and len(d["history"]) == 2 and d["studentNotice"]["read"] is False
    # otro docente no puede abrir la materia de Juan
    ana = docente(client, "anamd@uets.edu.ec", "g-ana")
    assert ana.get(f"/teacher/items/{iid}").status_code == 404
    assert ana.get(f"/teacher/items/{iid}/pdf").status_code == 404
    pdf = t.get(f"/teacher/items/{iid}/pdf")
    assert pdf.status_code == 200 and pdf.content[:5] == b"%PDF-"


def test_reglas_del_seguimiento_y_trimestre(client, superadmin, admin, google, aporte_activo, mongo, monkeypatch):
    importar(superadmin, exclude=["RUIZ CHACON BYRON GEOVANY"])
    st = register_student(client)
    body = {"requestType": "proceso_mejora", "representativeLinkId": st.link_id, "payload": {"subjects": ["Matemática"]}}
    rid = st.post("/me/requests", json=body).json()["_id"]
    t = docente(client, "juanpl@uets.edu.ec", "g-juan")
    iid = t.get("/teacher/items").json()[0]["id"]
    track = lambda **b: t.post("/teacher/items/track", json={"ids": [iid], **b})  # noqa: E731
    assert track(track="evaluado").status_code == 400            # primero hay que programar
    assert track(track="programada").status_code == 400          # programar exige fecha
    assert track(track="programada", scheduledAt="2026-12-02T09:30", note="Aula 12, traer calculadora").status_code == 200
    # al programar, la solicitud pasa sola a aprobada (a nombre del docente)
    assert mongo.requests.find_one({"_id": ObjectId(rid)})["status"] == "aprobado"
    ev = mongo.request_events.find_one({"requestId": ObjectId(rid), "eventType": "AUTO_APPROVED_ON_SCHEDULE"})
    assert ev["fromStatus"] == "en_revision" and ev["actorRole"] == "TEACHER"
    mail = mongo.email_outbox.find_one({"context.kind": "programada"})
    assert "Indicaciones del docente: Aula 12, traer calculadora" in mail["text"]
    assert "Nota: Aula 12" in mongo.notifications.find_one({"kind": "programada"})["body"]
    # no se presentó y se reprograma: queda constancia y se avisa la nueva fecha
    r = track(track="programada", scheduledAt="2026-12-09T09:30", missed=True)
    assert r.status_code == 200 and r.json()[0]["scheduledAt"] == "2026-12-09T09:30"
    evs = [e["to"] for e in mongo.teacher_tracking_events.find({"requestId": ObjectId(rid)}).sort("createdAt", 1)]
    assert evs == ["programada", "no_asistio", "programada"]
    assert mongo.email_outbox.find_one({"subject": {"$regex": "reprogramada"}})
    assert st.get("/me/requests").json()["data"][0]["evaluations"][0]["missedCount"] == 1
    # evaluado es definitivo
    assert track(track="evaluado").status_code == 200
    # el seguimiento se ve en las listas de la administración y del SUPERADMIN
    row = next(r for r in admin.get("/requests").json()["data"] if r["_id"] == rid)
    assert row["evaluations"] == [{"subject": "Matemática", "track": "evaluado", "label": "Evaluado",
                                   "scheduledAt": "2026-12-09T09:30", "missedCount": 1}]
    row = next(r for r in superadmin.get("/superadmin/requests").json()["rows"] if r["id"] == rid)
    assert row["evaluations"][0]["track"] == "evaluado"
    assert superadmin.get(f"/superadmin/requests/{rid}").json()["evaluations"][0]["label"] == "Evaluado"
    r = track(track="programada", scheduledAt="2026-12-10T09:30")
    assert r.status_code == 409 and r.json()["code"] == "TRACK_FINAL"
    # el estudiante ya no puede cancelar (configuración por defecto)
    monkeypatch.setenv("STUDENT_CAN_CANCEL", "false")
    from app.core import config
    config.get_settings.cache_clear()
    try:
        r = st.post(f"/me/requests/{rid}/cancel")
        assert r.status_code == 403 and r.json()["code"] == "CANCEL_DISABLED"
    finally:
        monkeypatch.setenv("STUDENT_CAN_CANCEL", "true")
        config.get_settings.cache_clear()
    # una vez por materia y por trimestre: aprobada, no puede volver a pedirla
    assert mongo.requests.find_one({"_id": ObjectId(rid)})["status"] == "aprobado"
    assert "Matemática" in st.get("/me/subjects").json()["usedThisTrimester"]
    r = st.post("/me/requests", json=body)
    assert r.status_code == 400 and r.json()["code"] == "SUBJECT_USED_IN_TRIMESTER", r.text
    # otra materia sí
    assert st.post("/me/requests", json={**body, "payload": {"subjects": ["Inglés"]}}).status_code == 201


def test_multirol_admin_y_docente(client, superadmin, google, aporte_activo, mongo):
    from tests.test_google import token as gtoken
    r = superadmin.post("/superadmin/users", json={"email": "vice@uets.edu.ec", "name": "Vicerrectora De Prueba",
                                                   "cedula": "0109876540", "role": "ADMIN"})
    assert r.status_code == 201
    uid = r.json()["user"]["id"]
    r = superadmin.post(f"/superadmin/users/{uid}/roles", json={"roles": ["ADMIN", "TEACHER"], "reason": "También dicta clases"})
    assert r.status_code == 200, r.text
    assert r.json()["roles"] == ["ADMIN", "TEACHER"] and r.json()["roleLabel"] == "Administrador + Docente"
    t = mongo.teachers.find_one({"email": "vice@uets.edu.ec"})
    superadmin.post(f"/superadmin/teachers/{t['_id']}/assignments", json={"course": "5to", "paralelo": "B", "subject": "Matemática"})
    # sigue apareciendo en Administradores
    assert any(x["id"] == uid for x in superadmin.get("/superadmin/users?role=staff").json()["rows"])
    # entra con Google al panel del administrador y también al docente
    a = Actor(client)
    r = a.post("/auth/google", json={"credential": gtoken(email="vice@uets.edu.ec", sub="g-vice")})
    assert r.status_code == 200 and r.json()["redirectTo"] == "/admin/dashboard" and r.json()["user"]["roles"] == ["ADMIN", "TEACHER"]
    assert a.get("/requests").status_code == 200
    assert a.get("/teacher/items").status_code == 200
    assert a.get("/superadmin/users").status_code == 403
    st = register_student(client)
    st.post("/me/requests", json={"requestType": "proceso_mejora", "representativeLinkId": st.link_id, "payload": {"subjects": ["Matemática"]}})
    assert len(a.get("/teacher/items").json()) == 1
    # docente principal + administrador
    r = superadmin.post(f"/superadmin/users/{uid}/roles", json={"roles": ["TEACHER"], "reason": "Ya no es vicerrectora"})
    assert r.status_code == 200 and r.json()["roles"] == ["TEACHER"]
    assert a.get("/requests").status_code in (401, 403)  # su sesión cambió de inicio: vuelve a ingresar
    # no puede escalar a SUPERADMIN por esta vía ni darse roles a sí mismo
    assert superadmin.post(f"/superadmin/users/{uid}/roles", json={"roles": ["SUPERADMIN"], "reason": "intento indebido"}).status_code == 400
    me = superadmin.user["id"]
    assert superadmin.post(f"/superadmin/users/{me}/roles", json={"roles": ["SUPERADMIN", "TEACHER"], "reason": "intento propio"}).status_code == 403


def test_cierre_de_envios_no_bloquea_al_personal(client, superadmin, admin, google, aporte_activo, mongo):
    importar(superadmin, exclude=["RUIZ CHACON BYRON GEOVANY"])
    st = register_student(client)
    body = {"requestType": "proceso_mejora", "representativeLinkId": st.link_id, "payload": {"subjects": ["Matemática"]}}
    rid = st.post("/me/requests", json=body).json()["_id"]
    # la administración cierra el envío (fechas ya pasadas)
    r = admin.put("/system/submission-window", json={"start": "2026-01-01T00:00", "end": "2026-01-02T00:00"})
    assert r.status_code == 200, r.text
    assert st.post("/me/requests", json={**body, "payload": {"subjects": ["Inglés"]}}).status_code == 400
    # el estudiante sigue entrando y viendo sus solicitudes
    assert st.get("/me/requests").status_code == 200
    # docente, administrador y SUPERADMIN siguen trabajando
    t = docente(client, "juanpl@uets.edu.ec", "g-juan")
    iid = t.get("/teacher/items").json()[0]["id"]
    assert t.post("/teacher/items/track", json={"ids": [iid], "track": "programada", "scheduledAt": "2026-12-02T09:30"}).status_code == 200
    assert t.post("/teacher/items/track", json={"ids": [iid], "track": "evaluado"}).status_code == 200
    assert admin.get(f"/evaluations/request/{rid}").status_code == 200
    assert superadmin.get("/superadmin/requests").status_code == 200
