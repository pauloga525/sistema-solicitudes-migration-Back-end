"""Crea cuentas de ESTUDIANTE de prueba usando la API (respeta todas las reglas del sistema).

Uso (con el backend en marcha y apuntando a una base de PRUEBAS):
    python tools/crear_estudiante_prueba.py                 # http://127.0.0.1:3001
    python tools/crear_estudiante_prueba.py http://127.0.0.1:3001

Crea:
  1. estudiante.prueba@uets.edu.ec   - datos completos + representante principal (lista para enviar solicitudes)
  2. estudiante.nuevo@uets.edu.ec    - solo registrada (para probar el llenado de datos y representantes)
Si ya existen, solo verifica que se pueda ingresar.
"""
import sys

import httpx

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:3001").rstrip("/")
PASSWORD = "Prueba-Estudiante-2026"
COMPLETA = {"email": "estudiante.prueba@uets.edu.ec",
            "perfil": {"firstNames": "Sofia Valentina", "lastNames": "Prueba Andrade", "cedula": "0105566772",
                       "course": "5to", "paralelo": "B"},
            "representante": {"firstNames": "Carlos Andres", "lastNames": "Prueba Mora", "cedula": "0102030400",
                              "email": "carlos.prueba@uets.edu.ec", "phone": "0998765432", "isPrimary": True}}
NUEVA = {"email": "estudiante.nuevo@uets.edu.ec"}


class Cliente:
    def __init__(self):
        self.h = httpx.Client(base_url=BASE, timeout=30)
        self.h.get("/auth/session").raise_for_status()

    def send(self, method, url, **kw):
        kw.setdefault("headers", {})["X-CSRF-Token"] = self.h.cookies.get("uets_csrf", "")
        return self.h.request(method, url, **kw)


def cuenta(email):
    c = Cliente()
    r = c.send("POST", "/auth/register", json={"email": email, "password": PASSWORD, "confirmPassword": PASSWORD})
    if r.status_code == 201:
        print(f"  creada: {email}")
        return c
    if r.status_code == 409:
        r = c.send("POST", "/auth/login", json={"username": email, "password": PASSWORD})
        if r.status_code == 201:
            print(f"  ya existía: {email}")
            return c
    sys.exit(f"  No se pudo crear/ingresar {email}: {r.status_code} {r.text}")


def main():
    try:
        httpx.get(f"{BASE}/health", timeout=5).raise_for_status()
    except Exception as exc:  # noqa: BLE001
        sys.exit(f"No hay backend en {BASE} ({exc}). Arranca uvicorn primero.")
    print(f"Backend: {BASE}")

    c = cuenta(COMPLETA["email"])
    me = c.send("GET", "/me/profile").json()
    if not me.get("profile"):
        r = c.send("PUT", "/me/profile", json=COMPLETA["perfil"])
        if r.status_code != 200:
            print(f"  Aviso perfil: {r.status_code} {r.json().get('message')}")
    if not me.get("representatives"):
        r = c.send("POST", "/me/representatives", json=COMPLETA["representante"])
        if r.status_code != 201:
            print(f"  Aviso representante: {r.status_code} {r.json().get('message')}")
    listo = c.send("GET", "/auth/session").json()["user"].get("profileCompleted")
    print(f"  datos completos: {'sí' if listo else 'NO (revisa los avisos)'}")

    cuenta(NUEVA["email"])

    print("\nCredenciales de prueba (ingreso en http://localhost:5173/ingresar):")
    print(f"  {COMPLETA['email']:<34} {PASSWORD}   - lista para enviar solicitudes")
    print(f"  {NUEVA['email']:<34} {PASSWORD}   - recién registrada, para probar el llenado de datos")
    print("\nPara enviar una solicitud necesitas un aporte activo (Calendario académico) y el curso 5to activo (Cursos y materias).")


if __name__ == "__main__":
    main()
