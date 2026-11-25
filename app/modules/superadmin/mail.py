"""Correo saliente (SUPERADMIN): estado de la configuración, correo de prueba y reintento de los fallidos."""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, Request
from pydantic import Field

from app.core.dependencies import AuthUser, require_superadmin
from app.core.exceptions import BadRequest
from app.database.connection import get_database
from app.modules.auth.schemas import StrictModel
from app.modules.mail import service as mail
from app.modules.superadmin.common import audit
from app.modules.system.service import enforce_rate_limit

router = APIRouter(prefix="/mail")
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@router.get("/status")
async def status(_: AuthUser = Depends(require_superadmin)):
    counts = {c["_id"]: c["n"] async for c in get_database()["email_outbox"].aggregate(
        [{"$group": {"_id": "$status", "n": {"$sum": 1}}}])}
    return {"config": mail.config_summary(), "counts": counts}


class TestIn(StrictModel):
    to: str = Field(min_length=5, max_length=254)


@router.post("/test")
async def send_test(body: TestIn, request: Request, actor: AuthUser = Depends(require_superadmin)):
    to = body.to.strip().lower()
    if not EMAIL.match(to):
        raise BadRequest("Correo no válido")
    await enforce_rate_limit("SA_TEST_MAIL", actor.user_id, "Enviaste muchos correos de prueba. Espera un momento.")
    res = await mail.send_test(to)
    await audit(actor, request, "MAIL_TEST", result="SUCCESS" if res["ok"] else "FAILURE",
                metadata={"to": to, "error": res.get("error")})
    return res


@router.post("/retry-failed")
async def retry_failed(request: Request, actor: AuthUser = Depends(require_superadmin)):
    n = await mail.retry_failed()
    await audit(actor, request, "MAIL_RETRY", metadata={"count": n})
    return {"retried": n}
