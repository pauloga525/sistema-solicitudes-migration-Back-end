"""Utilidades del panel SUPERADMIN: paginación, fechas, actor y auditoría."""
from __future__ import annotations

from typing import Any

from fastapi import Request

from app.core.dependencies import AuthUser, client_ip
from app.core.exceptions import BadRequest
from app.modules.audit.service import log_user_event

PAGE_SIZE = 25
MAX_PAGE_SIZE = 100


def page_args(page: int | str | None, size: int | str | None = None) -> tuple[int, int]:
    try:
        p = max(1, int(page or 1))
        s = min(MAX_PAGE_SIZE, max(1, int(size or PAGE_SIZE)))
    except (TypeError, ValueError) as exc:
        raise BadRequest("Paginación inválida") from exc
    return p, s


def paged(rows: list[Any], total: int, page: int, size: int) -> dict[str, Any]:
    return {"rows": rows, "total": total, "page": page, "pageSize": size, "totalPages": max(1, -(-total // size))}


async def audit(actor: AuthUser, request: Request, event: str, *, target: dict[str, Any] | None = None,
                result: str = "SUCCESS", metadata: dict[str, Any] | None = None, session: Any = None) -> None:
    await log_user_event(event, result=result, actor_id=actor.user_id, actor_email=actor.email or actor.username,
                         actor_role=actor.role, actor_ip=client_ip(request),
                         target_id=str(target["_id"]) if target else None,
                         target_email=(target.get("email") or target.get("username")) if target else None,
                         target_role=target.get("role") if target else None, metadata=metadata, session=session)
