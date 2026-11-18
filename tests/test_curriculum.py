"""Malla por curso y especialidad: cada estudiante solo puede pedir mejora en las materias que cursa."""
from app.modules.curriculum import ALL_SUBJECTS, CURRICULUM_SUBJECTS, subjects_for
from app.modules.curriculum_data import CURRICULUM
from tests.conftest import REP_NAME, Actor, register_student, split_name


def _bach_student(client, course: str, specialty: str, paralelo: str, dni: str = "0102030400") -> Actor:
    a = Actor(client, "10.0.0.7")
    r = a.post("/auth/register", json={"email": f"est{dni}.est@uets.edu.ec", "password": "clave-estudiante-1",
                                       "confirmPassword": "clave-estudiante-1"})
    assert r.status_code == 201, r.text
    r = a.put("/me/profile", json={**split_name("Maria Jose Perez Lopez"), "cedula": dni, "course": course,
                                   "paralelo": paralelo, "specialty": specialty})
    assert r.status_code == 200, r.text
    r = a.post("/me/representatives", json={**split_name(REP_NAME), "cedula": "0912345675", "phone": "0998765432",
                                            "email": "rep0912345675@uets.edu.ec", "relationship": "Padre", "isPrimary": True})
    assert r.status_code == 201, r.text
    a.link_id = r.json()["representatives"][0]["linkId"]
    return a


def test_malla_cubre_todos_los_cursos_y_especialidades():
    from app.modules.catalog import BACH_COURSES, COURSES, SPECIALTIES
    for c in COURSES:
        if c in BACH_COURSES:
            for spec, codes in SPECIALTIES.items():
                for par in codes:
                    assert subjects_for(c, par), (c, spec, par)
        else:
            assert subjects_for(c, "A"), c
    # registros que no son asignaturas evaluables no se ofrecen
    names = {s.lower() for v in CURRICULUM.values() for s in v}
    assert not names & {"comportamental", "convivencia", "participación estudiantil"}
    assert set(CURRICULUM_SUBJECTS) <= set(ALL_SUBJECTS)


def test_paralelos_de_un_curso_comparten_materias():
    assert subjects_for("1ro_Bach", "E1") == subjects_for("1ro_Bach", "E2")
    assert "Mod. Programación Estructurada" in subjects_for("1ro_Bach", "E1")
    assert "Mod. Programación Estructurada" not in subjects_for("1ro_Bach", "D1")  # Mecatrónica
    # 3ro de bachillerato usa los nombres nuevos (Informática → Desarrollo de Software, etc.)
    assert "Mod. Soporte Técnico" in subjects_for("3ro_Bach", "E1")
    assert subjects_for("3ro_Bach", "B") and subjects_for("3ro_Bach", "A1")


def test_me_subjects_por_curso(client, aporte_activo, superadmin):
    a = _bach_student(client, "1ro_Bach", "Desarrollo de Software", "E2")
    r = a.get("/me/subjects")
    assert r.status_code == 200
    body = r.json()
    assert body["course"] == "1ro_Bach" and body["specialty"] == "Desarrollo de Software"
    assert body["subjects"] == list(subjects_for("1ro_Bach", "E2"))
    assert a.get("/me/subjects", params={"requestType": "otro"}).status_code == 400


def test_solo_materias_que_cursa(client, aporte_activo, auth):
    client.patch("/solicitud-config/proceso_mejora/courses", headers=auth, json={"name": "1ro_Bach", "active": 1})
    a = _bach_student(client, "1ro_Bach", "Desarrollo de Software", "E1")
    body = {"requestType": "proceso_mejora", "representativeLinkId": a.link_id}
    # Materia de otra especialidad (Mecatrónica)
    r = a.post("/me/requests", json={**body, "payload": {"subjects": ["Mod. Servomecanismos"]}})
    assert r.status_code == 400 and r.json()["message"] == "Las siguientes materias no corresponden a tu curso: Mod. Servomecanismos"
    # Nombre del catálogo histórico que ya no está en la malla
    r = a.post("/me/requests", json={**body, "payload": {"subjects": ["Science"]}})
    assert r.status_code == 400 and "no corresponden a tu curso" in r.json()["message"]
    r = a.post("/me/requests", json={**body, "payload": {"subjects": ["Mod. Programación Estructurada", "Matemática"]}})
    assert r.status_code == 201, r.text
    # La materia desactivada por el administrador deja de ofrecerse
    client.patch("/solicitud-config/proceso_mejora/subjects", headers=auth, json={"name": "Química", "active": 0})
    assert "Química" not in a.get("/me/subjects").json()["subjects"]


def test_config_anterior_se_completa_sin_borrar(client, mongo):
    legacy = [{"name": n, "active": 1} for n in ("Matemática", "Science", "ERE")]
    mongo.solicitudconfigs.insert_one({"requestType": "proceso_mejora", "courses": [{"name": "5to", "active": 1}],
                                       "subjects": legacy})
    r = client.get("/solicitud-config/proceso_mejora").json()
    names = [s["name"] for s in r["subjects"]]
    assert names == list(CURRICULUM_SUBJECTS)  # se muestran solo las de la malla vigente
    stored = [s["name"] for s in mongo.solicitudconfigs.find_one()["subjects"]]
    assert {"Science", "ERE"} <= set(stored) and len(stored) == len(set(CURRICULUM_SUBJECTS) | {"Science", "ERE"})
    client.get("/solicitud-config/proceso_mejora")
    assert len(mongo.solicitudconfigs.find_one()["subjects"]) == len(stored)  # idempotente


def test_pdf_historico_conserva_grilla():
    import io

    from pypdf import PdfReader

    from app.modules.requests.templates.proceso_mejora import build_proceso_mejora_pdf_content
    pdf = build_proceso_mejora_pdf_content({"course": "5to", "paralelo": "B", "representativeName": "Juan Perez",
                                             "representativeDni": "0102030400", "studentName": "Ana Perez",
                                             "payload": {"subjects": ["Science", "ERE"]}})
    text = PdfReader(io.BytesIO(pdf)).pages[0].extract_text()
    assert "Science" in text and "Acompañamiento integral en el Aula" in text


def test_curriculum_para_filtros(client, auth):
    assert client.get("/solicitud-config/proceso_mejora/curriculum").status_code == 401
    r = client.get("/solicitud-config/proceso_mejora/curriculum", headers=auth).json()
    assert r["byCourse"]["5to"] == list(subjects_for("5to", "A"))
    assert "Mod. Servomecanismos" in r["byCourse"]["3ro_Bach"] and "Mod. Programación Estructurada" in r["byCourse"]["1ro_Bach"]
    assert "Science" in r["legacy"] and "Matemática" not in r["legacy"]


def test_materias_por_curso_independientes(client, aporte_activo, auth):
    r = client.get("/solicitud-config/proceso_mejora/course-subjects", headers=auth)
    assert r.status_code == 200
    groups = {g["key"]: g for g in r.json()["groups"]}
    assert len(groups) == len(CURRICULUM) and list(groups)[0] == "1ro_Basica|"
    assert groups["1ro_Bach|Desarrollo de Software"]["specialty"] == "Desarrollo de Software"
    assert client.get("/solicitud-config/proceso_mejora/course-subjects").status_code == 401
    # Desactivar Matemática solo en 5to no afecta a 6to
    r = client.patch("/solicitud-config/proceso_mejora/course-subjects", headers=auth,
                     json={"key": "5to|", "name": "Matemática", "active": 0})
    assert r.status_code == 200 and next(s for s in r.json()["subjects"] if s["name"] == "Matemática")["active"] == 0
    a = register_student(client)  # 5to B
    assert "Matemática" not in a.get("/me/subjects").json()["subjects"]
    r = a.post("/me/requests", json={"requestType": "proceso_mejora", "representativeLinkId": a.link_id,
                                     "payload": {"subjects": ["Matemática"]}})
    assert r.status_code == 400 and r.json()["message"] == "Las siguientes materias no están disponibles actualmente: Matemática"
    groups = {g["key"]: g for g in client.get("/solicitud-config/proceso_mejora/course-subjects", headers=auth).json()["groups"]}
    assert next(s for s in groups["6to|"]["subjects"] if s["name"] == "Matemática")["active"] == 1
    # Materia o curso inexistente
    r = client.patch("/solicitud-config/proceso_mejora/course-subjects", headers=auth,
                     json={"key": "5to|", "name": "Mod. Servomecanismos", "active": 0})
    assert r.status_code == 404
    r = client.patch("/solicitud-config/proceso_mejora/course-subjects", headers=auth,
                     json={"key": {"$ne": 1}, "name": "Matemática", "active": 0})
    assert r.status_code == 400
    # La activación general sigue aplicándose a todos los cursos
    client.patch("/solicitud-config/proceso_mejora/subjects", headers=auth, json={"name": "Inglés", "active": 0})
    groups = {g["key"]: g for g in client.get("/solicitud-config/proceso_mejora/course-subjects", headers=auth).json()["groups"]}
    assert all(s["active"] == 0 for g in groups.values() for s in g["subjects"] if s["name"] == "Inglés")


def test_todos_los_cursos_se_activan_una_vez(client, mongo, auth):
    mongo.solicitudconfigs.insert_one({"requestType": "proceso_mejora", "subjects": [],
                                       "courses": [{"name": "5to", "active": 1}, {"name": "8vo", "active": 0}]})
    r = client.get("/solicitud-config/proceso_mejora").json()
    assert all(c["active"] == 1 for c in r["courses"])
    client.patch("/solicitud-config/proceso_mejora/courses", headers=auth, json={"name": "8vo", "active": 0})
    r = client.get("/solicitud-config/proceso_mejora").json()
    assert next(c for c in r["courses"] if c["name"] == "8vo")["active"] == 0  # no se vuelve a forzar


def test_nota_pdf_segun_materias():
    from app.modules.requests.templates.proceso_mejora import evaluation_note
    assert evaluation_note(["Matemática", "Inglés", "Computación"]) == (
        "(La MEJORA de las asignaturas de Matemática e Inglés será mediante exámenes)")
    assert evaluation_note(["Inglés"]) == "(La MEJORA de Inglés será mediante examen)"
    assert evaluation_note(["Computación", "Enseñanza Religiosa Escolar", "Biología"]) == ""  # sin nota
    assert "Science" not in evaluation_note(["Matemática"])


def test_pdf_usa_el_trimestre_activo(client, auth, superadmin):
    import io
    from datetime import date

    from pypdf import PdfReader

    from app.modules.requests.templates.proceso_mejora import trimestre_label
    assert trimestre_label("Primer trimestre") == "Primer Trimestre"
    assert trimestre_label("Segundo") == "Segundo Trimestre"
    assert trimestre_label(None) == "Tercer Trimestre"  # sin calendario: texto original
    y = date.today().year
    body = {"name": "Año PDF", "startDate": f"{y - 1}-01-01", "endDate": f"{y + 1}-12-31",
            "trimestres": [{"name": "Primer trimestre", "startDate": f"{y - 1}-01-01", "endDate": f"{y + 1}-12-31",
                            "aportes": [{"name": "Aporte 1", "startDate": f"{y - 1}-01-01", "endDate": f"{y + 1}-12-31"}]}]}
    assert client.post("/plazos", headers=auth, json=body).status_code == 201
    a = register_student(client)
    r = a.post("/me/requests/preview", json={"requestType": "proceso_mejora", "representativeLinkId": a.link_id,
                                             "payload": {"subjects": ["Matemática"]}})
    text = PdfReader(io.BytesIO(r.content)).pages[0].extract_text()
    assert "Primer Trimestre" in " ".join(text.split()) and "Tercer Trimestre" not in text
    rid = a.post("/me/requests", json={"requestType": "proceso_mejora", "representativeLinkId": a.link_id,
                                       "payload": {"subjects": ["Matemática"]}}).json()["_id"]
    text = PdfReader(io.BytesIO(superadmin.get(f"/superadmin/requests/{rid}/pdf").content)).pages[0].extract_text()
    assert "Primer Trimestre" in " ".join(text.split())
