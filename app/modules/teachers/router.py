"""Panel docente: rol TEACHER exacto, verificado en el servidor. Cada docente solo ve sus materias."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import Field

from app.core.dependencies import AuthUser, require_teacher
from app.modules.auth.schemas import StrictModel
from app.modules.teachers import service as svc
from app.shared.serialization import to_json
from app.shared.utilities.dates import utcnow
from app.database.connection import get_database

router = APIRouter(prefix="/teacher", tags=["docente"])


class TrackIn(StrictModel):
    ids: list[str] = Field(min_length=1, max_length=200)
    track: Literal["por_atender", "programada", "evaluado", "no_asistio"]
    scheduledAt: str | None = Field(None, max_length=40)
    note: str | None = Field(None, max_length=300)
    missed: bool = False  # no se presentó y se reprograma


class ReadIn(StrictModel):
    ids: list[str] | Literal["all"]


@router.get("/me")
async def me(user: AuthUser = Depends(require_teacher)):
    t = await svc.teacher_for(user)
    u = await get_database()["users"].find_one({"_id": user.oid}, {"teacherLastSeenAt": 1})
    await get_database()["users"].update_one({"_id": user.oid}, {"$set": {"teacherLastSeenAt": utcnow()}})
    return to_json({"name": t.get("name"), "email": t.get("email"), "lastVisitAt": (u or {}).get("teacherLastSeenAt")})


@router.get("/subjects")
async def subjects(user: AuthUser = Depends(require_teacher)):
    t = await svc.teacher_for(user)
    return to_json(await svc.subjects_summary(t, await svc.build_items(t, user.oid)))


@router.get("/items")
async def items(user: AuthUser = Depends(require_teacher)):
    t = await svc.teacher_for(user)
    return to_json(await svc.build_items(t, user.oid))


@router.get("/items/{item_id}")
async def item(item_id: str, user: AuthUser = Depends(require_teacher)):
    t = await svc.teacher_for(user)
    return to_json(await svc.item_detail(t, user.oid, item_id[:80]))


@router.get("/items/{item_id}/pdf")
async def item_pdf(item_id: str, user: AuthUser = Depends(require_teacher)):
    """PDF de la solicitud (solo si es de una materia, curso y paralelo del docente)."""
    from fastapi.responses import Response
    from bson import ObjectId

    from app.core.config import get_settings
    from app.modules.requests.pdf_service import PdfService
    t = await svc.teacher_for(user)
    it = await svc.item_detail(t, user.oid, item_id[:80])  # verifica que sea suya
    r = await get_database()["requests"].find_one({"_id": ObjectId(it["requestId"])})
    data = await PdfService(get_settings().assets_dir).generate_request_pdf(r)
    return Response(data, media_type="application/pdf", headers={
        "Content-Disposition": f'inline; filename="solicitud-{r["requestNumber"]}.pdf"', "Cache-Control": "no-store"})


@router.post("/items/track")
async def track(body: TrackIn, user: AuthUser = Depends(require_teacher)):
    t = await svc.teacher_for(user)
    return to_json(await svc.update_tracking(t, user, body.ids, body.track, body.scheduledAt, body.note, body.missed))


@router.get("/notifications")
async def notifications(user: AuthUser = Depends(require_teacher)):
    await svc.teacher_for(user)
    return to_json(await svc.notifications_for(user.oid))


@router.post("/notifications/read")
async def read(body: ReadIn, user: AuthUser = Depends(require_teacher)):
    return {"updated": await svc.mark_read(user.oid, body.ids)}
