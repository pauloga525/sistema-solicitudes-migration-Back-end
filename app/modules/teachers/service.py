"""Panel docente: asignaciones, solicitudes por materia, seguimiento y notificaciones.

Colecciones nuevas (no tocan las del sistema original):
- `teachers`: un docente del distributivo (nombre, correo, cuenta vinculada).
- `teacher_assignments`: docente → curso, paralelo, materia (las de la importación vigente tienen active=True).
- `teacher_tracking`: seguimiento del docente por solicitud y materia (por atender / programada / evaluado).
- `teacher_tracking_events`: historial de cada cambio del seguimiento (quién, cuándo, de qué a qué).
- `notifications`: avisos en el sistema para docentes y estudiantes.

Un "ítem" es una solicitud vista desde una materia: una solicitud con tres materias genera tres ítems, y cada
docente solo ve los de las materias, cursos y paralelos que tiene asignados (lo verifica el servidor).
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime
from typing import Any

from bson import ObjectId

from app.core.config import get_settings
from app.core.dependencies import AuthUser
from app.core.exceptions import AppError, BadRequest
from app.database.connection import get_database
from app.modules.limits.service import fmt_local, parse_local, to_local
from app.modules.mail.service import queue_email
from app.shared.crypto.field_encryption import hash_field
from app.shared.utilities.dates import utcnow

log = logging.getLogger("app.teachers")

TRACKS = ("por_atender", "programada", "evaluado", "no_asistio")
TRACK_LABELS = {"por_atender": "Por atender", "programada": "Evaluación programada", "evaluado": "Evaluado",
                "no_asistio": "No se presentó"}
DONE = ("evaluado", "no_asistio")
CLOSED = ("rechazado", "cancelado")
COURSE_SHORT = {"1ro_Basica": "1ro de Básica", "2do_Basica": "2do de Básica", "3ro_Basica": "3ro de Básica",
                "4to_Basica": "4to de Básica", "5to": "5to de Básica", "6to": "6to de Básica", "7mo": "7mo de Básica",
                "8vo": "8vo de Básica", "9no": "9no de Básica", "10mo": "10mo de Básica",
                "1ro_Bach": "1ro de Bachillerato", "2do_Bach": "2do de Bachillerato", "3ro_Bach": "3ro de Bachillerato"}


def db():
    return get_database()


def item_id(request_id: Any, subject: str) -> str:
    return f"{request_id}~{hashlib.sha1(subject.encode()).hexdigest()[:12]}"


def course_label(c: str) -> str:
    return COURSE_SHORT.get(c, c)


# ── docente de la sesión ──────────────────────────────────────────────────────
async def teacher_for(user: AuthUser) -> dict[str, Any]:
    u = await db()["users"].find_one({"_id": user.oid}, {"teacherId": 1})
    t = await db()["teachers"].find_one({"_id": u.get("teacherId")}) if u and u.get("teacherId") else None
    if not t or not t.get("active", True):
        raise AppError(403, "Tu cuenta no tiene materias asignadas. Comunícate con el Departamento de Sistemas.",
                       code="TEACHER_NOT_ASSIGNED")
    return t


async def assignments(teacher_id: ObjectId) -> list[dict[str, Any]]:
    return await db()["teacher_assignments"].find({"teacherId": teacher_id, "active": True}).to_list(None)


def _akey(course: str, paralelo: str, subject: str) -> tuple[str, str, str]:
    return course, paralelo, subject


async def _requests_for(asg: list[dict[str, Any]], limit: int = 2000) -> list[dict[str, Any]]:
    if not asg:
        return []
    ors = [{"course": a["course"], "paralelo": a["paralelo"], "payload.subjects": a["subject"]} for a in asg]
    return await db()["requests"].find({"requestType": "proceso_mejora", "$or": ors},
                                       {"pdfAccessToken": 0}).sort("createdAt", -1).to_list(limit)


async def build_items(teacher: dict[str, Any], user_id: ObjectId) -> list[dict[str, Any]]:
    asg = await assignments(teacher["_id"])
    mine = {_akey(a["course"], a["paralelo"], a["subject"]) for a in asg}
    reqs = await _requests_for(asg)
    pairs = [(r, s) for r in reqs for s in (r.get("payload") or {}).get("subjects", [])
             if _akey(r["course"], r["paralelo"], s) in mine]
    if not pairs:
        return []
    track = {(t["requestId"], t["subject"]): t async for t in db()["teacher_tracking"].find(
        {"requestId": {"$in": list({r["_id"] for r, _ in pairs})}})}
    unread = {(n["requestId"], n.get("subject")) async for n in db()["notifications"].find(
        {"userId": user_id, "kind": "nueva", "read": False}, {"requestId": 1, "subject": 1})}
    out = []
    for r, s in pairs:
        t = track.get((r["_id"], s)) or {}
        out.append({
            "id": item_id(r["_id"], s), "requestId": str(r["_id"]), "requestNumber": r.get("requestNumber"),
            "studentName": r.get("studentName"), "course": r["course"], "paralelo": r["paralelo"],
            "specialty": None, "subject": s, "requestStatus": r.get("status"),
            "submittedAt": r.get("createdAt"), "track": t.get("track", "por_atender"),
            "scheduledAt": to_local(t.get("scheduledAt")) if t.get("scheduledAt") else None, "note": t.get("note"),
            "isNew": (r["_id"], s) in unread,
        })
    return out


async def item_detail(teacher: dict[str, Any], user_id: ObjectId, iid: str) -> dict[str, Any]:
    """Una solicitud vista por el docente: solo su materia, datos mínimos del estudiante y el historial de su seguimiento."""
    items = {i["id"]: i for i in await build_items(teacher, user_id)}
    it = items.get(iid)
    if not it:
        raise AppError(404, "No encontramos esa solicitud entre las de tus materias.", code="NOT_FOUND")
    rid = ObjectId(it["requestId"])
    req = await db()["requests"].find_one({"_id": rid}, {"campus": 1, "representativeName": 1, "createdAt": 1})
    t = await db()["teacher_tracking"].find_one({"requestId": rid, "subject": it["subject"]}) or {}
    history = [{"at": req["createdAt"], "title": "Solicitud enviada por el estudiante", "who": it["studentName"], "note": None}]
    async for ev in db()["teacher_tracking_events"].find({"requestId": rid, "subject": it["subject"]}).sort("createdAt", 1):
        title = TRACK_LABELS.get(ev.get("to"), ev.get("to"))
        if ev.get("to") == "programada" and ev.get("scheduledAt"):
            title += f" para el {fmt_local(ev['scheduledAt'])}"
        history.append({"at": ev["createdAt"], "title": title, "who": ev.get("byName"), "note": ev.get("note")})
    notice = await db()["notifications"].find_one({"requestId": rid, "subject": it["subject"], "kind": "programada"},
                                                  sort=[("createdAt", -1)])
    mail = await db()["email_outbox"].find_one({"context.requestId": str(rid), "context.subject": it["subject"],
                                                "context.kind": "programada"}, sort=[("createdAt", -1)])
    return {**it, "campus": (req or {}).get("campus"), "representativeName": (req or {}).get("representativeName"),
            "doneAt": t.get("doneAt"), "updatedBy": t.get("updatedByName"),
            "coTeachers": [a.get("name") for a in t.get("assignedTeachers") or [] if a.get("name") and a.get("name") != teacher.get("name")],
            "studentNotice": {"sentAt": notice["createdAt"], "read": bool(notice.get("read")), "readAt": notice.get("readAt"),
                              "mailStatus": (mail or {}).get("status")} if notice else None,
            "history": sorted(history, key=lambda h: h["at"])}


async def subjects_summary(teacher: dict[str, Any], items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    asg = await assignments(teacher["_id"])
    out = []
    for a in sorted(asg, key=lambda a: (list(COURSE_SHORT).index(a["course"]) if a["course"] in COURSE_SHORT else 99,
                                        a["paralelo"], a["subject"])):
        mine = [i for i in items if (i["course"], i["paralelo"], i["subject"]) == (a["course"], a["paralelo"], a["subject"])
                and i["requestStatus"] not in CLOSED]
        out.append({"key": f"{a['course']}|{a['paralelo']}|{a['subject']}", "subject": a["subject"], "course": a["course"],
                    "paralelo": a["paralelo"], "specialty": a.get("specialty") or None, "total": len(mine),
                    "porAtender": sum(1 for i in mine if i["track"] == "por_atender"),
                    "nuevas": sum(1 for i in mine if i["isNew"])})
    return out


async def update_tracking(teacher: dict[str, Any], user: AuthUser, ids: list[str], track: str,
                          scheduled: str | None, note: str | None, missed: bool = False) -> list[dict[str, Any]]:
    """Reglas del seguimiento (las verifica el servidor):
    - Programar exige fecha y hora.
    - "Evaluado" y "No se presentó" solo después de programar, y son definitivos: ya no se pueden cambiar.
    - `missed=True` con `track="programada"`: el estudiante no se presentó y se reprograma con una nueva fecha
      (queda registrado en el historial).
    """
    if track not in TRACKS:
        raise BadRequest("Seguimiento no válido")
    when: datetime | None = None
    if track == "programada":
        if not scheduled:
            raise BadRequest("Indica la fecha y hora de la evaluación.")
        when = parse_local(scheduled)
    if missed and track != "programada":
        raise BadRequest("Para reprogramar indica la nueva fecha y hora.")
    items = {i["id"]: i for i in await build_items(teacher, user.oid)}
    unknown = [i for i in ids if i not in items]
    if unknown:
        raise AppError(403, "Alguna de las solicitudes no corresponde a tus materias.", code="AUTH_FORBIDDEN")
    prevs: dict[str, dict[str, Any]] = {}
    for iid in dict.fromkeys(ids):
        it = items[iid]
        prev = await db()["teacher_tracking"].find_one({"requestId": ObjectId(it["requestId"]), "subject": it["subject"]}) or {}
        prevs[iid] = prev
        if it["requestStatus"] in CLOSED:
            continue
        prev_track = prev.get("track", "por_atender")
        if prev_track in DONE:
            raise AppError(409, f"La evaluación de {it['studentName']} ya quedó registrada como «{TRACK_LABELS[prev_track]}» "
                                "y no se puede cambiar.", code="TRACK_FINAL")
        if (track in DONE or missed) and not (prev_track == "programada" and prev.get("scheduledAt")):
            raise BadRequest("Primero programa la fecha y hora de la evaluación.")
    now = utcnow()
    changed_schedule = []
    for iid in dict.fromkeys(ids):
        it = items[iid]
        if it["requestStatus"] in CLOSED:
            continue
        rid = ObjectId(it["requestId"])
        prev = prevs[iid]
        prev_track = prev.get("track", "por_atender")
        doc: dict[str, Any] = {"track": track, "scheduledAt": when if track == "programada" else None, "updatedBy": user.oid,
                               "updatedByName": teacher.get("name"), "updatedAt": now}
        if note is not None:
            doc["note"] = note.strip()[:300] or None
        if track in DONE:
            # Conserva la fecha en que se programó: sirve para saber si se evaluó en la fecha avisada.
            doc["scheduledAt"] = prev.get("scheduledAt")
            doc["doneAt"] = now
        else:
            doc["doneAt"] = None
        ev_base = {"requestId": rid, "subject": it["subject"], "byUserId": user.oid, "byName": teacher.get("name")}
        inc: dict[str, Any] = {}
        if missed:
            # Constancia de la inasistencia antes de la nueva fecha.
            await db()["teacher_tracking_events"].insert_one({**ev_base, "from": "programada", "to": "no_asistio",
                                                              "scheduledAt": prev.get("scheduledAt"), "note": "Se reprograma la evaluación",
                                                              "createdAt": now})
            inc = {"missedCount": 1}
            prev_track = "no_asistio"
        changed = (prev_track != track or prev.get("scheduledAt") != doc["scheduledAt"]
                   or ("note" in doc and prev.get("note") != doc["note"]))
        if changed:
            await db()["teacher_tracking_events"].insert_one({
                **ev_base, "from": prev_track, "to": track, "scheduledAt": doc["scheduledAt"],
                "note": doc.get("note", prev.get("note")), "createdAt": now})
        upd: dict[str, Any] = {"$set": doc, "$setOnInsert": {"createdAt": now}}
        if inc:
            upd["$inc"] = inc
        await db()["teacher_tracking"].update_one({"requestId": rid, "subject": it["subject"]}, upd, upsert=True)
        if track == "programada" and when and (missed or prev.get("scheduledAt") != when or prev.get("track") != "programada"):
            changed_schedule.append((rid, it["subject"], when, doc.get("note", prev.get("note")), missed))
    # El docente atendió esas novedades: sus avisos de "nueva" quedan leídos.
    await db()["notifications"].update_many(
        {"userId": user.oid, "kind": "nueva", "read": False,
         "$or": [{"requestId": ObjectId(items[i]["requestId"]), "subject": items[i]["subject"]} for i in ids]},
        {"$set": {"read": True, "readAt": now}})
    for rid, subject, when, n, rep in changed_schedule:
        await approve_on_schedule(rid, user, subject)
        await notify_scheduled(rid, subject, when, teacher, n, rescheduled=rep)
    fresh = {i["id"]: i for i in await build_items(teacher, user.oid)}
    return [fresh[i] for i in dict.fromkeys(ids) if i in fresh]


async def approve_on_schedule(request_id: ObjectId, user: AuthUser, subject: str) -> None:
    """Al programar la evaluación, una solicitud pendiente o en revisión pasa sola a "Aprobado" (pedido de la
    institución). Queda en el historial a nombre del docente. Si ya cambió de estado, no hace nada."""
    from app.modules.requests import workflow
    try:
        req = await db()["requests"].find_one({"_id": request_id}, {"status": 1})
        if not req or req.get("status") not in ("pendiente", "en_revision"):
            return
        ev = workflow.status_event("AUTO_APPROVED_ON_SCHEDULE", req["status"], "aprobado", user, None,
                                   f"El docente programó la evaluación de {subject}.")
        await workflow.update_with_event(request_id, {"status": req["status"]}, {"status": "aprobado"}, ev)
    except AppError:
        pass  # otro cambio la tocó al mismo tiempo: se respeta ese estado
    except Exception:  # noqa: BLE001 — aprobar nunca impide guardar el seguimiento
        log.exception("No se pudo aprobar al programar la solicitud %s", request_id)


# ── notificaciones ───────────────────────────────────────────────────────────
async def _notify(user_ids: list[ObjectId], kind: str, title: str, body: str, request_id: ObjectId,
                  subject: str | None, audience: str) -> None:
    if not user_ids:
        return
    now = utcnow()
    await db()["notifications"].insert_many([{
        "userId": uid, "audience": audience, "kind": kind, "title": title, "body": body, "requestId": request_id,
        "subject": subject, "read": False, "createdAt": now} for uid in user_ids])


async def _teachers_of(course: str, paralelo: str, subject: str) -> list[dict[str, Any]]:
    tids = [a["teacherId"] async for a in db()["teacher_assignments"].find(
        {"course": course, "paralelo": paralelo, "subject": subject, "active": True}, {"teacherId": 1})]
    if not tids:
        return []
    return await db()["teachers"].find({"_id": {"$in": tids}, "active": True}).to_list(None)


def _link(path: str) -> tuple[str, str] | None:
    base = get_settings().public_url
    return ("Abrir el sistema", f"{base}{path}") if base else None


async def on_request_created(req: dict[str, Any]) -> None:
    """Aviso en el panel y correo a cada docente de las materias solicitadas."""
    try:
        for s in (req.get("payload") or {}).get("subjects", []):
            teachers = await _teachers_of(req["course"], req["paralelo"], s)
            uids = [t["userId"] for t in teachers if t.get("userId")]
            where = f"{course_label(req['course'])} {req['paralelo']}"
            # Constancia de a quién se asignó la solicitud al momento de enviarla (el distributivo puede cambiar).
            now = utcnow()
            await db()["teacher_tracking"].update_one(
                {"requestId": req["_id"], "subject": s},
                {"$set": {"assignedTeachers": [{"teacherId": t["_id"], "name": t.get("name"), "email": t.get("email"),
                                                "hasAccount": bool(t.get("userId"))} for t in teachers],
                          "assignedAt": now},
                 "$setOnInsert": {"track": "por_atender", "createdAt": now}}, upsert=True)
            await _notify(uids, "nueva", f"Nueva solicitud de {s}", f"{req.get('studentName')} · {where}",
                          req["_id"], s, "teacher")
            emails = [t["email"] for t in teachers if t.get("email")]
            if emails:
                await queue_email(
                    emails, f"Nueva solicitud de evaluación de mejora: {s}", "Nueva solicitud de evaluación de mejora",
                    [f"{req.get('studentName')} ({where}) solicitó rendir la evaluación de mejora de {s}.",
                     f"Número de solicitud: {req.get('requestNumber')}.",
                     "Puedes revisarla y programar la evaluación en tu panel docente."],
                    action=_link("/docente/solicitudes"), context={"kind": "nueva", "requestId": str(req["_id"]), "subject": s})
    except Exception:  # noqa: BLE001 — avisar nunca impide registrar la solicitud
        log.exception("No se pudo notificar a los docentes de la solicitud %s", req.get("_id"))


async def representative_email(req: dict[str, Any]) -> str | None:
    """Correo personal del representante que eligió el estudiante en la solicitud (guardado cifrado)."""
    rep = await db()["representatives"].find_one({"cedulaHash": hash_field(req.get("representativeDni") or "")}, {"emailEnc": 1})
    if not rep or not rep.get("emailEnc"):
        return None
    from app.modules.students.service import dec
    return dec(rep.get("emailEnc"))


async def notify_representative_created(req: dict[str, Any]) -> None:
    """Correo al representante: su representado envió una solicitud de evaluación de mejora."""
    try:
        to = await representative_email(req)
        if not to:
            return
        subjects = (req.get("payload") or {}).get("subjects", [])
        fecha = fmt_local(req["createdAt"]) if req.get("createdAt") else ""
        materias = ", ".join(subjects)
        await queue_email(
            [to], f"Solicitud de evaluación de mejora de {req.get('studentName')}",
            "Se registró una solicitud de evaluación de mejora",
            [f"Estimado/a {req.get('representativeName') or 'representante'}:",
             f"Le informamos que {req.get('studentName')} registró una solicitud para rendir la evaluación de mejora.",
             f"Estudiante: {req.get('studentName')}.",
             f"Curso: {course_label(req['course'])}, paralelo {req['paralelo']}.",
             f"Fecha de la solicitud: {fecha}.",
             f"{'Materia' if len(subjects) == 1 else 'Materias'}: {materias}.",
             f"Número de solicitud: {req.get('requestNumber')}.",
             "Cuando el docente programe la evaluación, le avisaremos por este medio la fecha y la hora."],
            context={"kind": "solicitud_creada", "requestId": str(req["_id"]), "subject": None})
    except Exception:  # noqa: BLE001 — avisar nunca impide registrar la solicitud
        log.exception("No se pudo avisar al representante de la solicitud %s", req.get("_id"))


async def on_request_closed(req: dict[str, Any], new_status: str) -> None:
    """La solicitud se canceló o se rechazó: los docentes ya no deben evaluarla."""
    try:
        kind = "cancelada" if new_status == "cancelado" else "rechazada"
        title = "Solicitud cancelada por el estudiante" if kind == "cancelada" else "Solicitud rechazada por la administración"
        for s in (req.get("payload") or {}).get("subjects", []):
            uids = [t["userId"] for t in await _teachers_of(req["course"], req["paralelo"], s) if t.get("userId")]
            await _notify(uids, kind, title, f"{req.get('studentName')} · {s}: no requiere evaluación.", req["_id"], s, "teacher")
    except Exception:  # noqa: BLE001
        log.exception("No se pudo notificar el cierre de la solicitud %s", req.get("_id"))


async def notify_scheduled(request_id: ObjectId, subject: str, when: datetime, teacher: dict[str, Any],
                           note: str | None = None, rescheduled: bool = False) -> None:
    """Evaluación programada: aviso al estudiante en el sistema y correo a él con copia a su representante."""
    try:
        req = await db()["requests"].find_one({"_id": request_id})
        if not req:
            return
        fecha = fmt_local(when)
        docente = teacher.get("name") or "tu docente"
        que = "reprogramada" if rescheduled else "programada"
        if req.get("studentUserId"):
            await _notify([req["studentUserId"]], "programada", f"Evaluación de mejora {que}: {subject}",
                          f"{fecha} · Docente: {docente}" + (f" · Nota: {note}" if note else ""), request_id, subject, "student")
        student = await db()["users"].find_one({"_id": req.get("studentUserId")}, {"email": 1}) if req.get("studentUserId") else None
        rep_email = await representative_email(req)
        to = [student["email"]] if student and student.get("email") else ([rep_email] if rep_email else [])
        cc = [rep_email] if rep_email and to and to[0] != rep_email else []
        await queue_email(
            to, f"Evaluación de mejora {que}: {subject}", f"Tu evaluación de mejora está {que}",
            [*(["El estudiante no se presentó en la fecha anterior; el docente fijó una nueva fecha."] if rescheduled else []),
             f"Estudiante: {req.get('studentName')} ({course_label(req['course'])} {req['paralelo']}).",
             f"Materia: {subject}.", f"Fecha y hora: {fecha}.", f"Docente: {docente}.",
             *([f"Indicaciones del docente: {note}"] if note else []),
             "Si no puedes asistir, comunícate con tu docente o con la institución."],
            cc=cc, action=_link("/estudiante"),
            context={"kind": "programada", "requestId": str(request_id), "subject": subject})
    except Exception:  # noqa: BLE001
        log.exception("No se pudo avisar la evaluación programada de %s", request_id)


async def notifications_for(user_id: ObjectId, limit: int = 100) -> list[dict[str, Any]]:
    rows = await db()["notifications"].find({"userId": user_id}).sort("createdAt", -1).to_list(limit)
    return [{"id": str(n["_id"]), "kind": n["kind"], "title": n["title"], "body": n["body"], "createdAt": n["createdAt"],
             "read": bool(n.get("read")), "itemId": item_id(n["requestId"], n["subject"]) if n.get("subject") else None,
             "requestId": str(n["requestId"]) if n.get("requestId") else None, "subject": n.get("subject")} for n in rows]


async def mark_read(user_id: ObjectId, ids: list[str] | str) -> int:
    q: dict[str, Any] = {"userId": user_id, "read": False}
    if ids != "all":
        oids = []
        for i in ids:
            try:
                oids.append(ObjectId(i))
            except Exception:  # noqa: BLE001
                continue
        q["_id"] = {"$in": oids}
    res = await db()["notifications"].update_many(q, {"$set": {"read": True, "readAt": utcnow()}})
    return res.modified_count


async def evaluations_for_requests(request_ids: list[ObjectId]) -> dict[ObjectId, list[dict[str, Any]]]:
    """Para el estudiante: seguimiento de cada materia de sus solicitudes (programada con fecha, evaluada)."""
    out: dict[ObjectId, list[dict[str, Any]]] = {}
    async for t in db()["teacher_tracking"].find({"requestId": {"$in": request_ids}, "track": {"$ne": "por_atender"}}):
        out.setdefault(t["requestId"], []).append({
            "subject": t["subject"], "track": t.get("track"),
            "scheduledAt": to_local(t.get("scheduledAt")) if t.get("scheduledAt") else None,
            "note": t.get("note"), "missedCount": int(t.get("missedCount") or 0),
            "teacher": t.get("updatedByName") or ", ".join(a["name"] for a in t.get("assignedTeachers") or [] if a.get("name")) or None})
    return out


# ── cuentas con rol docente asignado a mano ──────────────────────────────────
async def ensure_teacher_for_user(u: dict[str, Any]) -> dict[str, Any]:
    """Al dar el rol TEACHER a una cuenta: la vincula a su docente (por correo) o crea uno manual sin materias.

    Los docentes manuales (`manual: True`) no los toca una nueva importación del distributivo.
    """
    col = db()["teachers"]
    t = await col.find_one({"_id": u.get("teacherId")}) if u.get("teacherId") else None
    if not t and u.get("email"):
        t = await col.find_one({"email": u["email"]})
    now = utcnow()
    if t:
        await col.update_one({"_id": t["_id"]}, {"$set": {"userId": u["_id"], "active": True, "updatedAt": now}})
    else:
        res = await col.insert_one({"nameKey": f"MANUAL:{u['_id']}", "name": u.get("name") or u.get("email"),
                                    "email": u.get("email"), "active": True, "manual": True, "userId": u["_id"],
                                    "createdAt": now, "updatedAt": now})
        t = {"_id": res.inserted_id}
    await db()["users"].update_one({"_id": u["_id"]}, {"$set": {"teacherId": t["_id"]}})
    return await col.find_one({"_id": t["_id"]})


async def release_teacher_role(u: dict[str, Any]) -> None:
    """La cuenta deja de ser docente: un docente manual se desactiva (deja de recibir avisos)."""
    if u.get("teacherId"):
        await db()["teachers"].update_one({"_id": u["teacherId"], "manual": True},
                                          {"$set": {"active": False, "updatedAt": utcnow()}})


async def evaluation_summary(reqs: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Seguimiento docente de cada materia, para mostrarlo en las listas de la administración."""
    ids = [r["_id"] for r in reqs if r.get("requestType") == "proceso_mejora"]
    track = {(t["requestId"], t["subject"]): t async for t in db()["teacher_tracking"].find(
        {"requestId": {"$in": ids}}, {"requestId": 1, "subject": 1, "track": 1, "scheduledAt": 1, "missedCount": 1})} if ids else {}
    out: dict[str, list[dict[str, Any]]] = {}
    for r in reqs:
        if r.get("requestType") != "proceso_mejora":
            continue
        rows = []
        for s in (r.get("payload") or {}).get("subjects", []):
            t = track.get((r["_id"], s)) or {}
            tr = "no_requiere" if r.get("status") in CLOSED else (t.get("track") or "por_atender")
            rows.append({"subject": s, "track": tr,
                         "label": "No requiere evaluación" if tr == "no_requiere" else TRACK_LABELS.get(tr, tr),
                         "scheduledAt": to_local(t.get("scheduledAt")) if t.get("scheduledAt") else None,
                         "missedCount": int(t.get("missedCount") or 0)})
        out[str(r["_id"])] = rows
    return out
