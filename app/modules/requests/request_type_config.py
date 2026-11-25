"""Metadata por tipo de trámite — fuente única de verdad (request-type.config.ts).

Agregar un nuevo tipo = una entrada aquí + su esquema de payload
(`schemas.PAYLOAD_SCHEMA_BY_TYPE`) + su plantilla PDF. El resto del backend
consulta esta tabla en vez de comparar el string del tipo.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from app.modules.catalog import RequestStatus, RequestType
from app.modules.requests.templates.proceso_mejora import build_proceso_mejora_pdf_content


@dataclass(frozen=True)
class RequestTypeMetadata:
    label: str
    prefix: str
    initial_status: RequestStatus
    # Si True, abrir el detalle (o el PDF) aprueba automáticamente una solicitud en_revision.
    # Desactivado para proceso_mejora a pedido de la institución (nov. 2026): abrir solo visualiza y la
    # administración aprueba o niega a mano. El mecanismo se conserva por si se vuelve a activar.
    auto_approve_on_view: bool
    uses_academic_catalog: bool
    uses_aporte_quota: bool
    build_pdf_content: Callable[[dict[str, Any]], bytes]


REQUEST_TYPE_METADATA: dict[str, RequestTypeMetadata] = {
    RequestType.PROCESO_MEJORA.value: RequestTypeMetadata(
        label="Evaluación de Mejora",
        prefix="PM",
        initial_status=RequestStatus.EN_REVISION,
        auto_approve_on_view=False,
        uses_academic_catalog=True,
        uses_aporte_quota=True,
        build_pdf_content=build_proceso_mejora_pdf_content,
    ),
}
