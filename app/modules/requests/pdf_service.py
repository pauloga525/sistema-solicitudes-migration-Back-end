"""Genera el PDF de una solicitud: contenido por tipo (metadata) + membrete por campus."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from app.modules.catalog import CAMPUS_BY_COURSE
from app.modules.requests.request_type_config import REQUEST_TYPE_METADATA
from app.modules.requests.schemas import CreateRequestDTO
from app.shared.pdf.merger import letterhead_path, merge_with_letterhead


class PdfService:
    def __init__(self, assets_dir: Path):
        self.assets_dir = assets_dir

    async def generate_request_pdf(self, request: dict[str, Any]) -> bytes:
        """Usa el trimestre del aporte en que se creó la solicitud (o el vigente, en la vista previa)."""
        from app.modules.limits.service import period_for_request
        period = await period_for_request(request)
        enriched = {**request, "_trimestre": period["trimestre"] if period else None}
        content = REQUEST_TYPE_METADATA[request["requestType"]].build_pdf_content(enriched)
        return merge_with_letterhead(content, letterhead_path(self.assets_dir, request.get("campus", "")))

    async def generate_preview_pdf(self, dto: CreateRequestDTO) -> bytes:
        draft = {
            "requestNumber": "BORRADOR",
            "requestType": dto.requestType,
            "representativeName": dto.representativeName,
            "representativeDni": dto.representativeDni,
            "studentName": dto.studentName,
            "course": dto.course,
            "paralelo": dto.paralelo,
            "campus": CAMPUS_BY_COURSE[dto.course].value,
            "payload": dto.payload,
        }
        return await self.generate_request_pdf(draft)
