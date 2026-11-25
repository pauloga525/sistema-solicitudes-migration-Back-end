"""Diagnóstico del envío de correo, paso a paso. No muestra la clave ni envía nada salvo que lo pidas.

Uso (desde la carpeta backend, con el entorno virtual activo):
    python tools/probar_correo.py                 # prueba conexión y usuario/clave con los datos del .env
    python tools/probar_correo.py tu@correo.com   # además envía un correo de prueba a esa dirección
"""
from __future__ import annotations

import os
import smtplib
import socket
import ssl
import sys
from email.message import EmailMessage
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass


def env(*names: str) -> str:
    return next(((os.environ.get(n) or "").strip() for n in names if (os.environ.get(n) or "").strip()), "")


HOST = env("MAIL_SMTP_HOST", "SMTP_HOST")
USER = env("MAIL_SMTP_USER", "SMTP_USER")
PWD = (os.environ.get("MAIL_SMTP_PASSWORD") or os.environ.get("SMTP_PASS") or "").strip()
FROM = env("MAIL_FROM") or USER


def paso(n: str, ok: bool, detalle: str = "") -> None:
    print(f"  [{'OK ' if ok else 'ERR'}] {n}{(' — ' + detalle) if detalle else ''}")


def probar(port: int) -> bool:
    print(f"\nPuerto {port} ({'SSL directo' if port == 465 else 'STARTTLS'}):")
    try:
        sock = socket.create_connection((HOST, port), timeout=10)
        paso("Conexión TCP", True, f"{sock.getpeername()[0]}")
        first = sock.recv(200) if port != 465 else b""
        sock.close()
        if port != 465:
            paso("Saludo del servidor", first.startswith(b"220"), first.decode(errors="replace").strip()[:80] or "(vacío)")
    except Exception as e:  # noqa: BLE001
        paso("Conexión TCP", False, f"{type(e).__name__}: {e}")
        print("     → La red, el firewall o el antivirus bloquean la salida a ese puerto.")
        return False
    ctx = ssl.create_default_context()
    try:
        smtp = smtplib.SMTP_SSL(HOST, port, timeout=15, context=ctx) if port == 465 else smtplib.SMTP(HOST, port, timeout=15)
        with smtp:
            code, banner = smtp.ehlo()
            paso("EHLO", code == 250, banner.decode(errors="replace").splitlines()[0][:80])
            if port != 465:
                smtp.starttls(context=ctx)
                smtp.ehlo()
                paso("STARTTLS", True)
            try:
                cert = smtp.sock.getpeercert()
                issuer = dict(x[0] for x in cert.get("issuer", ()))
                paso("Certificado", True, f"emitido por {issuer.get('organizationName', '?')}")
                if "google" not in str(issuer).lower():
                    print("     → El certificado NO es de Google: un antivirus o proxy está interceptando el correo.")
            except Exception:  # noqa: BLE001
                pass
            if not (USER and PWD):
                paso("Login", False, "faltan MAIL_SMTP_USER o MAIL_SMTP_PASSWORD en el .env")
                return False
            try:
                smtp.login(USER, PWD)
                paso("Login", True, USER)
            except smtplib.SMTPServerDisconnected:
                paso("Login", False, "Google cerró la conexión al recibir el usuario y la clave")
                control(port)
                return False
            except smtplib.SMTPAuthenticationError as e:
                paso("Login", False, f"{e.smtp_code} {e.smtp_error.decode(errors='replace')[:120]}")
                print("     → Usuario o clave rechazados. Debe ser una contraseña de aplicación de esa cuenta.")
                return False
            if len(sys.argv) > 1:
                msg = EmailMessage()
                msg["Subject"], msg["From"], msg["To"] = "Prueba · Sistema de Solicitudes UETS", FROM, sys.argv[1]
                msg.set_content("Si recibes este mensaje, el correo del sistema funciona.")
                smtp.send_message(msg)
                paso("Envío", True, f"a {sys.argv[1]}")
        return True
    except Exception as e:  # noqa: BLE001
        paso("Conversación SMTP", False, f"{type(e).__name__}: {e}")
        if isinstance(e, smtplib.SMTPServerDisconnected):
            print("     → La conexión abre pero se corta: típico de antivirus con 'protección de correo' o de un firewall/proxy.")
        return False


def control(port: int) -> None:
    """Repite el login con una clave a propósito incorrecta: si Google responde con un código, la red está bien
    y el problema es la clave o la cuenta."""
    try:
        ctx = ssl.create_default_context()
        smtp = smtplib.SMTP_SSL(HOST, port, timeout=15, context=ctx) if port == 465 else smtplib.SMTP(HOST, port, timeout=15)
        with smtp:
            smtp.ehlo()
            if port != 465:
                smtp.starttls(context=ctx)
                smtp.ehlo()
            smtp.login(USER, "clave-de-control-incorrecta")
    except smtplib.SMTPAuthenticationError as e:
        print(f"     Control con clave falsa: Google respondió {e.smtp_code} "
              f"{e.smtp_error.decode(errors='replace').splitlines()[0][:90]}")
        print("     → La red funciona. Google corta solo con la clave real: la clave o la cuenta tienen el problema.")
    except Exception as e:  # noqa: BLE001
        print(f"     Control con clave falsa: también se corta ({type(e).__name__}).")
        print("     → Google está cortando todos los intentos de esta cuenta o de esta red (bloqueo temporal o política).")


print(f"Servidor: {HOST or '(falta MAIL_SMTP_HOST)'} · Cuenta: {USER or '(falta)'} · Clave cargada: {'sí' if PWD else 'no'}"
      f" ({len(PWD)} caracteres)")
if not HOST:
    sys.exit(1)
try:
    print("DNS:", ", ".join(sorted({a[4][0] for a in socket.getaddrinfo(HOST, None)}))[:120])
except Exception as e:  # noqa: BLE001
    print("DNS: ERROR", e)
    sys.exit(1)
ok = [probar(p) for p in (465, 587)]
print("\nResultado:", "funciona" if any(ok) else "no funciona en ninguno de los dos puertos")
