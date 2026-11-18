"""Datos de DEMOSTRACIÓN para revisar la interfaz (solo desarrollo, nunca contra la base real).

Usa la API pública del backend (respeta todas las reglas). Requiere el backend en marcha.
    python tools/seed_demo.py http://127.0.0.1:3001
"""
import random
import unicodedata
import sys
from datetime import date

import httpx

random.seed(7)

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:3001"
SA_EMAIL, SA_DEV, SA_PASS = "superadmin@uets.edu.ec", "superadmin123", "Demo-SuperAdmin-2026"


def dni(seed: int) -> str:
    base = f"01{seed:07d}"[:9]
    for k in range(10):
        d = base + str(k)
        coef = [2, 1] * 4 + [2]
        t = sum((v - 9 if v >= 10 else v) for v in (int(d[i]) * coef[i] for i in range(9)))
        if (0 if t % 10 == 0 else 10 - t % 10) == int(d[9]) and int(d[2]) < 6:
            return d
    return dni(seed + 1)


class C:
    def __init__(self):
        # IP distinta por cliente (el backend de demo corre con TRUST_PROXY=true) para no chocar con los límites de uso.
        self.h = httpx.Client(base_url=BASE, timeout=30, headers={"X-Forwarded-For": f"10.9.{random.randint(0, 255)}.{random.randint(1, 254)}"})
        self.h.get("/auth/session")

    def req(self, m, url, **kw):
        kw.setdefault("headers", {})["X-CSRF-Token"] = self.h.cookies.get("uets_csrf", "")
        r = self.h.request(m, url, **kw)
        return r


def login(email, pw):
    c = C()
    r = c.req("POST", "/auth/login", json={"username": email, "password": pw})
    return c, r


sa, r = login(SA_EMAIL, SA_DEV)
if r.status_code == 201 and r.json()["user"]["forcePasswordChange"]:
    sa.req("POST", "/auth/change-password", json={"newPassword": SA_PASS, "confirmPassword": SA_PASS})
sa, r = login(SA_EMAIL, SA_PASS)
assert r.status_code == 201, r.text
admin, r = login("admin", "admin123")
assert r.status_code == 201, r.text

y = date.today().year
r = admin.req("POST", "/plazos", json={"name": f"Demo {y}", "startDate": f"{y - 1}-09-01", "endDate": f"{y + 1}-07-31", "trimestres": [
    {"name": "Primer trimestre", "startDate": f"{y - 1}-09-01", "endDate": f"{y}-12-31", "aportes": [
        {"name": "Aporte 1", "startDate": f"{y - 1}-09-01", "endDate": f"{y}-06-30"},
        {"name": "Aporte 2", "startDate": f"{y}-07-01", "endDate": f"{y}-12-31"}]},
    {"name": "Segundo trimestre", "startDate": f"{y + 1}-01-01", "endDate": f"{y + 1}-07-31", "aportes": [
        {"name": "Aporte 3", "startDate": f"{y + 1}-01-01", "endDate": f"{y + 1}-07-31"}]}]})
print("plazos", r.status_code)
for c in ("8vo", "9no", "10mo", "1ro_Bach", "2do_Bach"):
    admin.req("PATCH", "/solicitud-config/proceso_mejora/courses", json={"name": c, "active": 1})

NAMES = ["Mateo Sebastián Ordóñez Vintimilla", "Valentina Isabel Cárdenas Peña", "Joaquín Andrés Guzmán Torres", "Camila Fernanda Rivera Mora",
         "Santiago José Pesántez Ochoa", "Isabella María Álvarez Crespo", "Martín Alejandro Vázquez León", "Emilia Sofía Jaramillo Ortiz",
         "Nicolás David Zhunio Patiño", "Antonella Paz Merchán Idrovo", "Benjamín Esteban Coronel Calle", "Renata Lucía Arévalo Palacios"]
REPS = ["Carlos Alberto Ordóñez Ruiz", "Patricia Elena Peña Vélez", "Jorge Luis Guzmán Sarmiento", "Lorena Beatriz Mora Ávila",
        "Diego Fernando Pesántez Bravo", "Mónica Alexandra Crespo Abad", "Fabián Rodrigo Vázquez Tapia", "Gabriela Inés Ortiz Molina",
        "Raúl Patricio Zhunio Loja", "Verónica Paola Idrovo Cueva", "Hernán Mauricio Coronel Ríos", "Silvia Cristina Palacios Neira"]
COURSES = ["5to", "6to", "7mo", "8vo", "9no", "10mo", "1ro_Bach", "2do_Bach", "5to", "6to", "7mo", "4to_Basica"]
students = []
for i, (n, rp, co) in enumerate(zip(NAMES, REPS, COURSES)):
    st = C()
    email = unicodedata.normalize("NFD", f"{n.split()[0]}.{n.split()[2]}".lower()).encode("ascii", "ignore").decode() + "@uets.edu.ec"
    r = st.req("POST", "/auth/register", json={"email": email, "password": "Estudiante-2026", "confirmPassword": "Estudiante-2026"})
    if r.status_code != 201:
        if r.status_code != 409:
            print("register", email, r.status_code, r.text[:200])
        st, r = login(email, "Estudiante-2026")
    if i == 11:
        continue  # registrado sin datos (aparece en inconsistencias)
    sd = dni(1000 + i * 37)
    w = n.split()
    bach = co.endswith("_Bach")
    r = st.req("PUT", "/me/profile", json={"firstNames": " ".join(w[:-2]), "lastNames": " ".join(w[-2:]), "cedula": sd, "course": co,
                                           "paralelo": random.choice(["E1", "E2"]) if bach else random.choice("ABC"),
                                           **({"specialty": "Desarrollo de Software"} if bach else {})})
    if r.status_code != 200:
        print("profile", r.status_code, r.text[:200])
    if i != 10:
        rw = rp.split()
        rdni = dni(5000 + i * 53)
        r = st.req("POST", "/me/representatives", json={"firstNames": " ".join(rw[:-2]), "lastNames": " ".join(rw[-2:]), "cedula": rdni,
                                                         "email": f"rep.{rdni}@uets.edu.ec", "phone": f"09{random.randint(10000000, 99999999)}",
                                                         "relationship": random.choice(["Madre", "Padre"]), "isPrimary": True})
        if r.status_code != 201:
            print("rep", r.status_code, r.text[:200]); continue
        link = r.json()["representatives"][0]["linkId"]
        for k in range(random.randint(1, 2)):
            own = st.req("GET", "/me/subjects").json().get("subjects") or ["Matemática"]
            subs = random.sample(own, min(len(own), random.randint(1, 2)))
            r = st.req("POST", "/me/requests", json={"requestType": "proceso_mejora", "representativeLinkId": link, "payload": {"subjects": subs}})
            if r.status_code == 201:
                students.append(r.json()["_id"])
print("solicitudes", len(students))
for rid in students[:5]:
    admin.req("GET", f"/requests/{rid}")  # auto-aprobación al abrir
for rid in students[5:8]:
    admin.req("PATCH", f"/requests/{rid}/status", json={"status": "rechazado", "reason": "Las materias ya fueron aprobadas en el aporte anterior"})
r = sa.req("POST", "/superadmin/users", json={"email": "lucia.paredes@uets.edu.ec", "name": "Lucía Andrea Paredes Saquicela", "cedula": dni(9100), "role": "ADMIN"})
print("admin2", r.status_code)
sa.req("PUT", "/superadmin/limits", json={"requestType": "proceso_mejora", "scope": "aporte", "maxRequests": 3})
login("noexiste@uets.edu.ec", "x")
print("ok")
