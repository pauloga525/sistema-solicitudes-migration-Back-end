"""Correo saliente con bandeja de salida (`email_outbox`).

Cada correo se registra primero en la base y luego se envía en segundo plano por SMTP (Google Workspace:
smtp.gmail.com:587 con STARTTLS o :465 con SSL, y una contraseña de aplicación). Si el SMTP no está configurado, el correo queda
registrado con estado "sin_configurar" y no se envía: así el sistema funciona igual y se puede revisar qué se
habría enviado. Un fallo de correo nunca interrumpe la acción que lo originó.
"""
from __future__ import annotations

import asyncio
import html
import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr
from typing import Any, Callable

from app.core.config import get_settings
from app.database.connection import get_database
from app.shared.utilities.dates import utcnow

log = logging.getLogger("app.mail")

# Las pruebas reemplazan esto para no abrir conexiones reales.
Sender = Callable[[EmailMessage], None]


def _smtp_send(msg: EmailMessage) -> None:
    s = get_settings()
    ctx = ssl.create_default_context()
    # 465: SSL directo (SMTPS). Otro puerto (587): conexión normal + STARTTLS.
    conn = (smtplib.SMTP_SSL(s.mail_smtp_host, s.mail_smtp_port, timeout=20, context=ctx) if s.mail_smtp_port == 465
            else smtplib.SMTP(s.mail_smtp_host, s.mail_smtp_port, timeout=20))
    with conn as smtp:
        if s.mail_smtp_port != 465:
            smtp.starttls(context=ctx)
        if s.mail_smtp_user and s.mail_smtp_password:
            smtp.login(s.mail_smtp_user, s.mail_smtp_password)
        smtp.send_message(msg)


sender: Sender = _smtp_send
_tasks: set[asyncio.Task] = set()


def render_html(title: str, paragraphs: list[str], action: tuple[str, str] | None = None) -> str:
    """Correo sencillo con la identidad institucional (azul marino y dorado). El texto se escapa."""
    body = "".join(f'<p style="margin:0 0 12px;line-height:1.5">{html.escape(p)}</p>' for p in paragraphs)
    btn = ""
    if action:
        label, url = action
        btn = (f'<p style="margin:20px 0 0"><a href="{html.escape(url, quote=True)}" style="background:#002753;color:#fff;'
               f'padding:10px 18px;border-radius:8px;text-decoration:none;font-weight:600">{html.escape(label)}</a></p>')
    return (
        '<div style="font-family:Arial,Helvetica,sans-serif;color:#1a1c1e;max-width:560px;margin:0 auto">'
        '<div style="border-bottom:3px solid #fdd000;padding:16px 0;margin-bottom:16px">'
        '<strong style="color:#002753;font-size:16px">UETS · Sistema de Solicitudes</strong></div>'
        f'<h1 style="font-size:18px;color:#002753;margin:0 0 12px">{html.escape(title)}</h1>{body}{btn}'
        '<p style="margin:24px 0 0;font-size:12px;color:#6f7480">Este es un mensaje automático; por favor no respondas a este correo.</p>'
        '</div>'
    )


async def queue_email(to: list[str], subject: str, title: str, paragraphs: list[str], *, cc: list[str] | None = None,
                      action: tuple[str, str] | None = None, context: dict[str, Any] | None = None) -> str | None:
    """Registra el correo y, si el SMTP está configurado, lo envía en segundo plano. Devuelve el id del registro."""
    to = sorted({a.strip().lower() for a in to if a and "@" in a})
    cc = sorted({a.strip().lower() for a in (cc or []) if a and "@" in a} - set(to))
    if not to:
        return None
    s = get_settings()
    text = "\n\n".join([title, *paragraphs] + ([f"{action[0]}: {action[1]}"] if action else []))
    doc = {"to": to, "cc": cc, "subject": subject, "text": text, "html": render_html(title, paragraphs, action),
           "status": "pendiente" if s.mail_enabled else "sin_configurar", "error": None, "attempts": 0,
           "context": context or {}, "createdAt": utcnow(), "sentAt": None}
    try:
        res = await get_database()["email_outbox"].insert_one(doc)
    except Exception:  # noqa: BLE001 — el correo nunca interrumpe la acción principal
        log.exception("No se pudo registrar el correo")
        return None
    if s.mail_enabled:
        task = asyncio.get_running_loop().create_task(_deliver(res.inserted_id))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
    return str(res.inserted_id)


async def _deliver(oid: Any) -> None:
    col = get_database()["email_outbox"]
    doc = await col.find_one({"_id": oid})
    if not doc:
        return
    s = get_settings()
    msg = EmailMessage()
    msg["Subject"] = doc["subject"]
    msg["From"] = formataddr((s.mail_from_name, s.mail_from))
    msg["To"] = ", ".join(doc["to"])
    if s.mail_reply_to:
        msg["Reply-To"] = s.mail_reply_to
    if doc.get("cc"):
        msg["Cc"] = ", ".join(doc["cc"])
    msg.set_content(doc["text"])
    msg.add_alternative(doc["html"], subtype="html")
    try:
        await asyncio.get_running_loop().run_in_executor(None, sender, msg)
        await col.update_one({"_id": oid}, {"$set": {"status": "enviado", "sentAt": utcnow(), "error": None}, "$inc": {"attempts": 1}})
    except Exception as exc:  # noqa: BLE001
        # Sin credenciales ni direcciones en el registro: solo el tipo de error.
        log.warning("Correo no enviado (%s)", type(exc).__name__)
        await col.update_one({"_id": oid}, {"$set": {"status": "fallido", "error": type(exc).__name__}, "$inc": {"attempts": 1}})


# Causa probable de cada tipo de error (se guarda solo el tipo, nunca credenciales).
ERROR_HINTS = {
    "SMTPAuthenticationError": "Google rechazó el usuario o la clave. Revisa MAIL_SMTP_USER y que MAIL_SMTP_PASSWORD sea una "
                               "contraseña de aplicación vigente de esa cuenta (16 letras, sin espacios).",
    "SMTPSenderRefused": "Google no permite enviar con ese remitente. MAIL_FROM debe ser la misma cuenta de MAIL_SMTP_USER "
                         "(o un alias autorizado de ella).",
    "SMTPRecipientsRefused": "El servidor rechazó la dirección del destinatario.",
    "SMTPNotSupportedError": "El servidor no acepta ese modo de conexión. Con el puerto 465 se usa SSL; con 587, STARTTLS.",
    "SMTPServerDisconnected": "El servidor cerró la conexión. Revisa que el puerto (465 o 587) corresponda al servidor.",
    "SSLError": "Falló la conexión segura. Con smtp.gmail.com usa el puerto 465 (SSL) o 587 (STARTTLS).",
    "gaierror": "No se encontró el servidor de correo: revisa MAIL_SMTP_HOST o la conexión a internet del servidor.",
    "TimeoutError": "No hubo respuesta del servidor de correo: la red o el firewall pueden estar bloqueando el puerto.",
    "timeout": "No hubo respuesta del servidor de correo: la red o el firewall pueden estar bloqueando el puerto.",
    "ConnectionRefusedError": "El servidor de correo rechazó la conexión en ese puerto.",
    "OSError": "No se pudo conectar con el servidor de correo (red o firewall).",
}


def error_hint(error: str | None) -> str | None:
    if not error:
        return None
    return ERROR_HINTS.get(error, f"Error de envío ({error}).")


def config_summary() -> dict[str, Any]:
    s = get_settings()
    return {"enabled": s.mail_enabled, "host": s.mail_smtp_host, "port": s.mail_smtp_port, "user": s.mail_smtp_user,
            "passwordSet": bool(s.mail_smtp_password), "from": s.mail_from, "fromName": s.mail_from_name,
            "replyTo": s.mail_reply_to, "mode": "SSL" if s.mail_smtp_port == 465 else "STARTTLS"}


async def send_test(to: str) -> dict[str, Any]:
    """Envía un correo de prueba en el momento y devuelve el resultado (para la página de Configuración)."""
    s = get_settings()
    if not s.mail_enabled:
        return {"ok": False, "error": "sin_configurar", "hint": "Falta MAIL_SMTP_HOST o MAIL_FROM en el .env del backend."}
    msg = EmailMessage()
    msg["Subject"] = "Correo de prueba · Sistema de Solicitudes UETS"
    msg["From"] = formataddr((s.mail_from_name, s.mail_from))
    msg["To"] = to
    if s.mail_reply_to:
        msg["Reply-To"] = s.mail_reply_to
    paragraphs = ["Si recibes este mensaje, el envío de correos del sistema está bien configurado."]
    msg.set_content("\n\n".join(["Correo de prueba", *paragraphs]))
    msg.add_alternative(render_html("Correo de prueba", paragraphs), subtype="html")
    try:
        await asyncio.get_running_loop().run_in_executor(None, sender, msg)
    except Exception as exc:  # noqa: BLE001
        name = type(exc).__name__
        log.warning("Correo de prueba no enviado (%s)", name)
        return {"ok": False, "error": name, "hint": error_hint(name)}
    return {"ok": True}


async def retry_failed(limit: int = 500) -> int:
    """Vuelve a intentar los correos que fallaron (p. ej. después de corregir la clave)."""
    if not get_settings().mail_enabled:
        return 0
    col = get_database()["email_outbox"]
    ids = [d["_id"] async for d in col.find({"status": {"$in": ["fallido", "sin_configurar"]}}, {"_id": 1}).sort("createdAt", -1).limit(limit)]
    if ids:
        await col.update_many({"_id": {"$in": ids}}, {"$set": {"status": "pendiente", "error": None}})
    for oid in ids:
        task = asyncio.get_running_loop().create_task(_deliver(oid))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
    return len(ids)


async def drain() -> None:
    """Espera los envíos en curso (pruebas y apagado ordenado)."""
    if _tasks:
        await asyncio.gather(*list(_tasks), return_exceptions=True)
