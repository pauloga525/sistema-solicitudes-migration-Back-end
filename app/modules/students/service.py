"""Perfiles de estudiante y representantes. Todo dato personal se guarda cifrado (AES-256-GCM).

La cédula se guarda además como HMAC (`cedulaHash`) para unicidad y para cruzarla con el padrón
sin descifrarlo. Los vínculos estudiante-representante no se borran: se desactivan.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from bson import ObjectId
from pymongo.errors import DuplicateKeyError

from app.core.exceptions import AppError, BadRequest, Conflict, NotFound
from app.database.connection import get_database
from app.shared.crypto.field_encryption import decrypt_field, encrypt_field, hash_field
from app.shared.utilities.dates import utcnow

PROFILE_ENC = ("names", "firstNames", "lastNames", "cedula")
LEGACY_PROFILE_ENC = ("birthDate", "phone", "address")  # perfiles guardados antes del formulario actual
REP_ENC = ("names", "firstNames", "lastNames", "cedula", "phone", "email")
RELATIONSHIPS = ("Representante legal", "Madre", "Padre", "Abuelo/a", "Tío/a", "Hermano/a", "Tutor legal", "Otro")


def db():
    return get_database()


def enc(v: str | None) -> str | None:
    return encrypt_field(v) if v not in (None, "") else None


def dec(v: str | None) -> str | None:
    if not v:
        return None
    try:
        return decrypt_field(v)
    except Exception:  # noqa: BLE001 — dato ilegible: no se rompe el listado
        return "[ilegible]"


LOCKED_MSG = ("Tus datos ya fueron registrados y no se pueden cambiar desde tu cuenta. "
              "Si necesitas corregirlos, acércate al Departamento de Sistemas para que habiliten la edición.")


def student_data_locked(p: dict[str, Any] | None) -> bool:
    """Los datos del estudiante se bloquean al guardarlos la primera vez. Solo el SUPERADMIN los desbloquea
    (`locked: False`); al volver a guardarlos se bloquean otra vez. Perfiles anteriores a esta regla: bloqueados."""
    return bool(p) and p.get("locked") is not False


def rep_data_locked(p: dict[str, Any] | None, has_primary: bool) -> bool:
    """Los datos del representante se bloquean en cuanto hay un representante principal registrado."""
    return has_primary and (not p or p.get("repLocked") is not False)


def profile_out(p: dict[str, Any] | None) -> dict[str, Any] | None:
    if not p:
        return None
    out = {f: dec(p.get(f + "Enc")) for f in PROFILE_ENC}
    out.update({"course": p.get("course"), "paralelo": p.get("paralelo"), "specialty": p.get("specialty"), "padronMatch": bool(p.get("padronMatch")),
                "completed": bool(p.get("completed")), "updatedAt": p.get("updatedAt"),
                "locked": student_data_locked(p),
                "unlockedAt": p.get("unlockedAt"), "unlockReason": p.get("unlockReason")})
    return out


def rep_out(r: dict[str, Any], link: dict[str, Any] | None = None) -> dict[str, Any]:
    out = {"id": str(r["_id"]), **{f: dec(r.get(f + "Enc")) for f in REP_ENC}, "isActive": r.get("isActive", True)}
    if link:
        out.update({"linkId": str(link["_id"]), "relationship": link.get("relationship"),
                    "isPrimary": bool(link.get("isPrimary")), "linkedAt": link.get("linkedAt")})
    return out


async def get_profile(user_id: ObjectId) -> dict[str, Any] | None:
    return await db()["student_profiles"].find_one({"userId": user_id})


async def padron_match(cedula_hash: str) -> bool:
    return bool(await db()["students"].find_one({"cedulaHash": cedula_hash}, {"_id": 1}))


async def save_profile(user_id: ObjectId, data: dict[str, Any]) -> dict[str, Any]:
    cedula = data["cedula"]
    h = hash_field(cedula)
    prev = await get_profile(user_id)
    if student_data_locked(prev):
        raise AppError(403, LOCKED_MSG, code="PROFILE_LOCKED")
    if prev and prev.get("cedulaHash") != h:
        if await db()["requests"].count_documents({"studentUserId": user_id}, limit=1):
            raise BadRequest("No puedes cambiar la cédula porque ya tienes solicitudes registradas. Pide ayuda a la institución.")
    fields = {f + "Enc": enc(data.get(f)) for f in PROFILE_ENC}
    fields.update({"cedulaHash": h, "course": data["course"], "paralelo": data["paralelo"], "specialty": data.get("specialty"),
                   "padronMatch": await padron_match(h), "updatedAt": utcnow(), "locked": True})
    try:
        await db()["student_profiles"].update_one(
            {"userId": user_id}, {"$set": fields, "$setOnInsert": {"userId": user_id, "createdAt": utcnow(), "completed": False}},
            upsert=True)
    except DuplicateKeyError as exc:
        raise Conflict("Esa cédula ya está registrada en otra cuenta. Si es tuya, pide ayuda a la institución.") from exc
    await refresh_completion(user_id)
    name = data.get("names")
    await db()["users"].update_one({"_id": user_id}, {"$set": {"name": name, "updatedAt": utcnow()}})
    return profile_out(await get_profile(user_id))  # type: ignore[return-value]


async def refresh_completion(user_id: ObjectId) -> bool:
    """El perfil está completo con datos personales y al menos un representante principal activo."""
    prof = await get_profile(user_id)
    primary = await db()["student_representatives"].find_one({"studentUserId": user_id, "active": True, "isPrimary": True})
    done = bool(prof and prof.get("namesEnc") and prof.get("cedulaHash") and primary)
    if prof:
        await db()["student_profiles"].update_one({"_id": prof["_id"]}, {"$set": {"completed": done}})
    return done


async def links_of(student_id: ObjectId) -> list[dict[str, Any]]:
    links = await db()["student_representatives"].find({"studentUserId": student_id, "active": True}).sort("linkedAt", 1).to_list(50)
    reps = {r["_id"]: r async for r in db()["representatives"].find({"_id": {"$in": [l["representativeId"] for l in links]}})}
    return [rep_out(reps[l["representativeId"]], l) for l in links if l["representativeId"] in reps]


async def upsert_representative(data: dict[str, Any], owner: ObjectId | None) -> tuple[dict[str, Any], bool]:
    """Devuelve (representante, creado). Un representante con varios estudiantes se guarda una sola vez;
    sus datos solo los puede editar un estudiante si es el único vinculado (o el SUPERADMIN)."""
    h = hash_field(data["cedula"])
    col = db()["representatives"]
    existing = await col.find_one({"cedulaHash": h})
    if existing:
        return existing, False
    doc = {**{f + "Enc": enc(data.get(f)) for f in REP_ENC}, "cedulaHash": h, "isActive": True,
           "createdBy": owner, "createdAt": utcnow(), "updatedAt": utcnow()}
    try:
        res = await col.insert_one(doc)
    except DuplicateKeyError:
        return await col.find_one({"cedulaHash": h}), False  # type: ignore[return-value]
    return {"_id": res.inserted_id, **doc}, True


async def link(student_id: ObjectId, rep_id: ObjectId, relationship: str, is_primary: bool, by: str) -> dict[str, Any]:
    col = db()["student_representatives"]
    if await col.find_one({"studentUserId": student_id, "representativeId": rep_id, "active": True}):
        raise Conflict("Ese representante ya está vinculado a este estudiante")
    if await col.count_documents({"studentUserId": student_id, "active": True}) >= 5:
        raise BadRequest("Un estudiante puede tener como máximo 5 representantes")
    has_primary = await col.find_one({"studentUserId": student_id, "active": True, "isPrimary": True})
    is_primary = is_primary or not has_primary
    if is_primary and has_primary:
        await col.update_many({"studentUserId": student_id, "active": True, "isPrimary": True}, {"$set": {"isPrimary": False}})
    doc = {"studentUserId": student_id, "representativeId": rep_id, "relationship": relationship, "isPrimary": is_primary,
           "active": True, "linkedBy": by, "linkedAt": utcnow(), "unlinkedAt": None, "unlinkedBy": None}
    try:
        res = await col.insert_one(doc)
    except DuplicateKeyError as exc:
        raise Conflict("El vínculo cambió al mismo tiempo en otra pestaña. Recarga e inténtalo de nuevo.") from exc
    await refresh_completion(student_id)
    return {"_id": res.inserted_id, **doc}


async def set_primary(link_id: ObjectId, student_id: ObjectId | None = None) -> dict[str, Any]:
    col = db()["student_representatives"]
    q: dict[str, Any] = {"_id": link_id, "active": True}
    if student_id:
        q["studentUserId"] = student_id
    ln = await col.find_one(q)
    if not ln:
        raise NotFound("Vínculo no encontrado")
    await col.update_many({"studentUserId": ln["studentUserId"], "active": True, "isPrimary": True, "_id": {"$ne": link_id}},
                          {"$set": {"isPrimary": False}})
    await col.update_one({"_id": link_id}, {"$set": {"isPrimary": True}})
    await refresh_completion(ln["studentUserId"])
    return ln


async def unlink(link_id: ObjectId, by: str, student_id: ObjectId | None = None) -> dict[str, Any]:
    col = db()["student_representatives"]
    q: dict[str, Any] = {"_id": link_id, "active": True}
    if student_id:
        q["studentUserId"] = student_id
    ln = await col.find_one_and_update(q, {"$set": {"active": False, "isPrimary": False, "unlinkedAt": utcnow(), "unlinkedBy": by}})
    if not ln:
        raise NotFound("Vínculo no encontrado")
    if ln.get("isPrimary"):  # se promueve el siguiente para no dejar al estudiante sin principal
        nxt = await col.find_one({"studentUserId": ln["studentUserId"], "active": True}, sort=[("linkedAt", 1)])
        if nxt:
            await col.update_one({"_id": nxt["_id"]}, {"$set": {"isPrimary": True}})
    await refresh_completion(ln["studentUserId"])
    return ln


async def update_rep_data(rep_id: ObjectId, data: dict[str, Any], *, only_if_single_owner: ObjectId | None) -> bool:
    """Actualiza nombres/teléfono/correo. Para un estudiante, solo si es el único vinculado."""
    if only_if_single_owner is not None:
        others = await db()["student_representatives"].count_documents(
            {"representativeId": rep_id, "active": True, "studentUserId": {"$ne": only_if_single_owner}})
        if others:
            return False
    fields = {f + "Enc": enc(data.get(f)) for f in ("names", "firstNames", "lastNames", "phone", "email") if f in data}
    if fields:
        await db()["representatives"].update_one({"_id": rep_id}, {"$set": {**fields, "updatedAt": utcnow()}})
    return True


def validate_birth_date(v: str | None) -> None:
    if not v:
        return
    try:
        d = date.fromisoformat(v)
    except ValueError as exc:
        raise BadRequest("Fecha de nacimiento inválida") from exc
    if not (date(1990, 1, 1) <= d <= date.today()):
        raise BadRequest("Fecha de nacimiento fuera de rango")


def assert_cedula(v: str, label: str = "La cédula") -> None:
    from app.shared.validators.cedula import validate_ecuadorian_dni
    if not validate_ecuadorian_dni(v):
        raise AppError(400, f"{label} no es una cédula ecuatoriana válida")


def full_name(first: str, last: str, label: str) -> str:
    """Nombres + apellidos → nombre completo, con las mismas reglas del formulario original
    (solo letras; al menos un nombre y dos apellidos en total, que es lo que exige la solicitud)."""
    import re as _re
    from app.shared.validators.cedula import is_letters_only
    first = _re.sub(r"\s+", " ", first).strip()
    last = _re.sub(r"\s+", " ", last).strip()
    if not first or not is_letters_only(first):
        raise BadRequest(f"Los nombres {label} solo pueden tener letras")
    if not last or not is_letters_only(last):
        raise BadRequest(f"Los apellidos {label} solo pueden tener letras")
    full = f"{first} {last}"
    assert_person_name(full, f"El nombre completo {label}")
    return full


def assert_person_name(v: str, label: str) -> None:
    from app.shared.validators.cedula import validate_person_name
    if not validate_person_name(v):
        raise BadRequest(f"{label} debe tener al menos un nombre y dos apellidos (solo letras)")
