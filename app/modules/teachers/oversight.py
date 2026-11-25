"""Seguimiento de evaluaciones para la administración (vicerrectorado): ADMIN o SUPERADMIN, verificado en el servidor.

Por cada solicitud y materia muestra a qué docente se asignó, qué hizo (programó, evaluó, marcó que no se presentó),
y la constancia de cada aviso: notificaciones en el sistema (con la hora en que se leyeron) y correos (con su estado
de envío). Sirve para responder a un representante si la evaluación se tomó y si se le avisó.

Solo lectura: no modifica solicitudes, seguimiento ni avisos.
"""
from __future__ import annotations

import csv
import io
import re
from datetime import timedelta
from typing import Any

from bson import ObjectId
from fastapi import APIRouter, Depends
from fastapi.responses import Response

from app.core.dependencies import AuthUser, require_admin
from app.core.exceptions import NotFound
from app.database.connection import get_database
from app.modules.audit.service import ROLE_LABELS, request_history
from app.modules.limits.service import to_local
from app.modules.mail.service import error_hint
from app.modules.superadmin.common import page_args, paged
from app.modules.teachers.service import CLOSED, COURSE_SHORT, TRACK_LABELS, course_label
from app.shared.serialization import to_json
from app.shared.utilities.dates import utcnow
from app.shared.validators.object_id import parse_object_id

router = APIRouter(prefix="/evaluations", tags=["seguimiento"])

ESTADOS = {
    "sin_docente": "Sin docente asignado",
    "por_atender": "Por atender",
    "programada": "Programada",
    "vencida": "Fecha pasada sin registrar",
    "evaluado": "Evaluado",
    "no_asistio": "No se presentó",
    "no_requiere": "No requiere evaluación",
}
STATUS_LABELS = {"pendiente": "Pendiente", "en_revision": "En revisión", "aprobado": "Aprobado", "rechazado": "Rechazado",
                 "cancelado": "Cancelado"}
LATE_DAYS = 3  # por atender más de 3 días desde el envío
KIND_LABELS = {"solicitud_creada": "Aviso de solicitud al representante", "nueva": "Aviso de nueva solicitud", "cancelada": "Aviso de cancelación", "rechazada": "Aviso de rechazo",
               "programada": "Aviso de evaluación programada", "recordatorio": "Recordatorio"}
MAIL_STATUS = {"enviado": "Enviado", "pendiente": "En cola", "fallido": "Falló el envío",
               "sin_configurar": "No enviado (correo sin configurar)"}


def db():
    return get_database()


async def _current_teachers() -> dict[tuple[str, str, str], list[str]]:
    names = {t["_id"]: t.get("name") async for t in db()["teachers"].find({"active": True}, {"name": 1})}
    out: dict[tuple[str, str, str], list[str]] = {}
    async for a in db()["teacher_assignments"].find({"active": True}, {"teacherId": 1, "course": 1, "paralelo": 1, "subject": 1}):
        if a["teacherId"] in names:
            out.setdefault((a["course"], a["paralelo"], a["subject"]), []).append(names[a["teacherId"]])
    return out


def _estado(req: dict[str, Any], t: dict[str, Any], teachers: list[str], now) -> str:
    if req.get("status") in CLOSED:
        return "no_requiere"
    track = t.get("track") or "por_atender"
    if track in ("evaluado", "no_asistio"):
        return track
    if track == "programada":
        sch = t.get("scheduledAt")
        return "vencida" if sch and sch.replace(tzinfo=now.tzinfo) < now - timedelta(hours=2) else "programada"
    return "sin_docente" if not teachers else "por_atender"


def _mail_summary(mails: list[dict[str, Any]]) -> str | None:
    if not mails:
        return None
    st = {m.get("status") for m in mails}
    for s in ("enviado", "fallido", "pendiente", "sin_configurar"):
        if s in st:
            return s
    return None


async def _rows(course: str | None, paralelo: str | None, q: str | None) -> list[dict[str, Any]]:
    query: dict[str, Any] = {"requestType": "proceso_mejora"}
    if course in COURSE_SHORT:
        query["course"] = course
    if paralelo:
        query["paralelo"] = paralelo[:4]
    if q and q.strip():
        rx = {"$regex": re.escape(q.strip()[:80]), "$options": "i"}
        query["$or"] = [{"studentName": rx}, {"requestNumber": rx}]
    reqs = await db()["requests"].find(query, {"pdfAccessToken": 0}).sort("createdAt", -1).to_list(5000)
    if not reqs:
        return []
    ids = [r["_id"] for r in reqs]
    track = {(t["requestId"], t["subject"]): t async for t in db()["teacher_tracking"].find({"requestId": {"$in": ids}})}
    notes: dict[tuple[ObjectId, str, str], list[dict[str, Any]]] = {}
    async for n in db()["notifications"].find({"requestId": {"$in": ids}}, {"requestId": 1, "subject": 1, "kind": 1, "read": 1,
                                                                           "readAt": 1, "createdAt": 1, "audience": 1}):
        notes.setdefault((n["requestId"], n.get("subject") or "", n.get("audience") or ""), []).append(n)
    mails: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    async for m in db()["email_outbox"].find({"context.requestId": {"$in": [str(i) for i in ids]}},
                                             {"context": 1, "status": 1, "sentAt": 1}):
        c = m.get("context") or {}
        mails.setdefault((c.get("requestId"), c.get("subject") or "", c.get("kind") or ""), []).append(m)
    current = await _current_teachers()
    now = utcnow()
    out = []
    for r in reqs:
        for s in (r.get("payload") or {}).get("subjects", []):
            t = track.get((r["_id"], s)) or {}
            assigned = [a.get("name") for a in t.get("assignedTeachers") or [] if a.get("name")]
            teachers = assigned or current.get((r["course"], r["paralelo"], s), [])
            tn = [n for n in notes.get((r["_id"], s, "teacher"), []) if n["kind"] == "nueva"]
            sn = [n for n in notes.get((r["_id"], s, "student"), []) if n["kind"] == "programada"]
            created = r.get("createdAt")
            estado = _estado(r, t, teachers, now)
            out.append({
                "requestId": str(r["_id"]), "requestNumber": r.get("requestNumber"), "studentName": r.get("studentName"),
                "course": r["course"], "courseLabel": course_label(r["course"]), "paralelo": r["paralelo"], "subject": s,
                "requestStatus": r.get("status"), "submittedAt": created, "isTest": bool(r.get("isTest")),
                "teachers": teachers, "estado": estado, "estadoLabel": ESTADOS[estado],
                "track": t.get("track") or "por_atender", "trackLabel": TRACK_LABELS.get(t.get("track") or "por_atender"),
                "scheduledAt": to_local(t.get("scheduledAt")), "doneAt": t.get("doneAt"), "updatedBy": t.get("updatedByName"),
                "late": estado == "por_atender" and created is not None
                        and created.replace(tzinfo=now.tzinfo) < now - timedelta(days=LATE_DAYS),
                "teacherNotified": bool(tn), "teacherRead": any(n.get("read") for n in tn),
                "teacherMail": _mail_summary(mails.get((str(r["_id"]), s, "nueva"), [])),
                "studentNotified": bool(sn), "studentRead": any(n.get("read") for n in sn),
                "studentMail": _mail_summary(mails.get((str(r["_id"]), s, "programada"), [])),
            })
    return out


def _fold(v: str) -> str:
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFD", v) if unicodedata.category(c) != "Mn").lower()


def _narrow(rows: list[dict[str, Any]], subject: str | None, teacher: str | None) -> list[dict[str, Any]]:
    if subject:
        rows = [r for r in rows if r["subject"] == subject]
    if teacher and teacher.strip():
        k = _fold(teacher.strip()[:80])
        rows = [r for r in rows if any(k in _fold(n or "") for n in r["teachers"])]
    return rows


def _filter(rows: list[dict[str, Any]], estado: str | None) -> list[dict[str, Any]]:
    if estado == "atrasadas":
        return [r for r in rows if r["late"]]
    if estado in ESTADOS:
        return [r for r in rows if r["estado"] == estado]
    if estado == "abiertas":
        return [r for r in rows if r["estado"] not in ("no_requiere", "evaluado", "no_asistio")]
    return rows


@router.get("")
async def list_evaluations(estado: str | None = None, course: str | None = None, paralelo: str | None = None,
                           q: str | None = None, subject: str | None = None, teacher: str | None = None,
                           page: int = 1, pageSize: int = 25, _: AuthUser = Depends(require_admin)):
    rows = _narrow(await _rows(course, paralelo, q), subject, teacher)
    totals = {k: sum(1 for r in rows if r["estado"] == k) for k in ESTADOS}
    totals["atrasadas"] = sum(1 for r in rows if r["late"])
    totals["total"] = len(rows)
    sel = _filter(rows, estado)
    p, ps = page_args(page, pageSize)
    return to_json({**paged(sel[(p - 1) * ps:p * ps], len(sel), p, ps), "totals": totals, "estados": ESTADOS})


def _cell(v: Any) -> str:
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s  # evita fórmulas al abrir en Excel


@router.get("/export")
async def export_csv(estado: str | None = None, course: str | None = None, paralelo: str | None = None,
                     q: str | None = None, subject: str | None = None, teacher: str | None = None,
                     _: AuthUser = Depends(require_admin)):
    rows = _filter(_narrow(await _rows(course, paralelo, q), subject, teacher), estado)
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Solicitud", "Estudiante", "Curso", "Paralelo", "Materia", "Docente", "Seguimiento", "Fecha programada",
                "Registrado", "Aviso al docente", "Correo al docente", "Aviso al estudiante", "Correo al estudiante",
                "Estado de la solicitud", "Enviada"])
    for r in rows:
        w.writerow([_cell(x) for x in (
            r["requestNumber"], r["studentName"], r["courseLabel"], r["paralelo"], r["subject"], ", ".join(r["teachers"]) or "—",
            r["estadoLabel"], (r["scheduledAt"] or "").replace("T", " "),
            to_local(r["doneAt"]).replace("T", " ") if r["doneAt"] else "",
            ("Leído" if r["teacherRead"] else "Enviado") if r["teacherNotified"] else "No",
            MAIL_STATUS.get(r["teacherMail"] or "", "No"),
            ("Leído" if r["studentRead"] else "Enviado") if r["studentNotified"] else "No",
            MAIL_STATUS.get(r["studentMail"] or "", "No"), r["requestStatus"],
            to_local(r["submittedAt"]).replace("T", " ") if r["submittedAt"] else "")])
    data = "﻿" + buf.getvalue()  # BOM: Excel abre bien las tildes
    name = f"seguimiento-evaluaciones-{to_local(utcnow())[:10]}.csv"
    return Response(data, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})


@router.get("/request/{id_}")
async def request_tracking(id_: str, _: AuthUser = Depends(require_admin)):
    """Expediente de una solicitud: docentes, seguimiento por materia y la línea de tiempo con cada aviso y correo."""
    r = await db()["requests"].find_one({"_id": parse_object_id(id_)}, {"pdfAccessToken": 0})
    if not r:
        raise NotFound("Solicitud no encontrada")
    rid = r["_id"]
    subjects = (r.get("payload") or {}).get("subjects", []) if r.get("requestType") == "proceso_mejora" else []
    track = {t["subject"]: t async for t in db()["teacher_tracking"].find({"requestId": rid})}
    current = await _current_teachers()
    now = utcnow()
    uid_names: dict[ObjectId, tuple[str, str]] = {}
    notes = await db()["notifications"].find({"requestId": rid}).sort("createdAt", 1).to_list(1000)
    user_ids = list({n["userId"] for n in notes})
    async for u in db()["users"].find({"_id": {"$in": user_ids}}, {"name": 1, "email": 1, "role": 1}):
        uid_names[u["_id"]] = (u.get("name") or u.get("email") or "—", u.get("role") or "")

    blocks = []
    for s in subjects:
        t = track.get(s) or {}
        assigned = t.get("assignedTeachers") or []
        teachers = []
        if assigned:
            tids = [a["teacherId"] for a in assigned if a.get("teacherId")]
            info = {x["_id"]: x async for x in db()["teachers"].find({"_id": {"$in": tids}}, {"userId": 1, "active": 1})}
            logins = {u["_id"]: u.get("lastLoginAt") async for u in db()["users"].find(
                {"_id": {"$in": [x.get("userId") for x in info.values() if x.get("userId")]}}, {"lastLoginAt": 1})}
            for a in assigned:
                x = info.get(a.get("teacherId")) or {}
                teachers.append({"name": a.get("name"), "email": a.get("email"), "hasAccount": bool(x.get("userId")),
                                 "lastLoginAt": logins.get(x.get("userId")), "active": x.get("active", True)})
        else:
            teachers = [{"name": n, "email": None, "hasAccount": None, "lastLoginAt": None, "active": True}
                        for n in current.get((r["course"], r["paralelo"], s), [])]
        estado = _estado(r, t, [x["name"] for x in teachers], now)
        blocks.append({"subject": s, "estado": estado, "estadoLabel": ESTADOS[estado], "teachers": teachers,
                       "snapshot": bool(assigned), "track": t.get("track") or "por_atender",
                       "trackLabel": TRACK_LABELS.get(t.get("track") or "por_atender"),
                       "scheduledAt": to_local(t.get("scheduledAt")), "doneAt": t.get("doneAt"), "note": t.get("note"),
                       "updatedBy": t.get("updatedByName"), "updatedAt": t.get("updatedAt")})

    timeline: list[dict[str, Any]] = []
    for e in await request_history(rid):
        timeline.append({"at": e["createdAt"], "type": "solicitud", "icon": "description", "title": e["eventLabel"],
                         "detail": " → ".join(STATUS_LABELS.get(x, x) for x in (e.get("fromStatus"), e.get("toStatus")) if x) or None,
                         "who": " · ".join(x for x in (e.get("actorName"), e.get("actorRoleLabel")) if x) or None,
                         "comment": e.get("comment"), "subject": None})
    async for ev in db()["teacher_tracking_events"].find({"requestId": rid}).sort("createdAt", 1):
        to = ev.get("to")
        detail = TRACK_LABELS.get(to, to)
        if to == "programada" and ev.get("scheduledAt"):
            detail += f" para el {to_local(ev['scheduledAt']).replace('T', ' a las ')}"
        timeline.append({"at": ev["createdAt"], "type": "docente", "icon": "co_present",
                         "title": f"Seguimiento del docente: {detail}", "detail": f"Antes: {TRACK_LABELS.get(ev.get('from'), ev.get('from'))}",
                         "who": f"{ev.get('byName') or '—'} · Docente", "comment": ev.get("note"), "subject": ev.get("subject")})
    for n in notes:
        name, role = uid_names.get(n["userId"], ("Cuenta eliminada", ""))
        timeline.append({"at": n["createdAt"], "type": "aviso", "icon": "notifications",
                         "title": f"{KIND_LABELS.get(n['kind'], n['kind'])} en el sistema",
                         "detail": f"Para {name} ({ROLE_LABELS.get(role, role) or '—'})",
                         "who": "Sistema", "comment": None, "subject": n.get("subject"),
                         "read": bool(n.get("read")), "readAt": n.get("readAt")})
    async for m in db()["email_outbox"].find({"context.requestId": str(rid)}).sort("createdAt", 1):
        c = m.get("context") or {}
        timeline.append({"at": m["createdAt"], "type": "correo", "icon": "mail",
                         "title": f"Correo: {m.get('subject')}",
                         "detail": "Para " + ", ".join(m.get("to") or []) + (f" · copia a {', '.join(m['cc'])}" if m.get("cc") else ""),
                         "who": "Sistema", "comment": None, "subject": c.get("subject"),
                         "mailStatus": m.get("status"), "mailStatusLabel": MAIL_STATUS.get(m.get("status") or "", m.get("status")),
                         "mailError": error_hint(m.get("error")) if m.get("status") == "fallido" else None,
                         "sentAt": m.get("sentAt")})
    timeline.sort(key=lambda x: x["at"])
    return to_json({"requestId": str(rid), "requestNumber": r.get("requestNumber"), "studentName": r.get("studentName"),
                    "course": r["course"], "courseLabel": course_label(r["course"]), "paralelo": r["paralelo"],
                    "requestStatus": r.get("status"), "submittedAt": r.get("createdAt"), "isTest": bool(r.get("isTest")),
                    "subjects": blocks, "timeline": timeline})
