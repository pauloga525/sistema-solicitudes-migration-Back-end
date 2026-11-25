"""Reglas de negocio de solicitudes (equivalente a RequestsService)."""
from __future__ import annotations

import secrets
from datetime import datetime
from typing import Any

from bson import ObjectId

from app.core.exceptions import BadRequest, Conflict, NotFound
from app.modules.catalog import CAMPUS_BY_COURSE, RequestStatus
from app.modules.plazos.service import PlazosService
from app.modules.requests.repository import CounterRepository, RequestRepository
from app.modules.requests.request_type_config import REQUEST_TYPE_METADATA
from app.modules.requests.schemas import CreateRequestDTO, FilterRequestsDTO
from app.modules.solicitud_config.service import SolicitudConfigService
from app.shared.utilities.dates import utcnow
from app.shared.utilities.escape_regexp import escape_regexp
from app.shared.validators.object_id import is_valid_object_id

ACTIVE_STATUSES = [RequestStatus.PENDIENTE.value, RequestStatus.EN_REVISION.value]


def _subjects(doc: dict[str, Any]) -> list[str]:
    payload = doc.get("payload")
    subjects = payload.get("subjects") if isinstance(payload, dict) else None
    return list(subjects) if isinstance(subjects, list) else []


class RequestsService:
    def __init__(self, requests: RequestRepository, counters: CounterRepository,
                 config_service: SolicitudConfigService, plazos_service: PlazosService):
        self.requests = requests
        self.counters = counters
        self.config_service = config_service
        self.plazos_service = plazos_service

    # ── creación ────────────────────────────────────────────────────────────
    async def _validate_against_solicitud_config(self, dto: CreateRequestDTO, allow_test: bool = False) -> None:
        config = await self.config_service.get_config(dto.requestType)
        active_courses = {c.get("name") for c in config.get("courses", []) if c.get("active") == 1}
        if dto.course not in active_courses:
            raise BadRequest(f'El curso "{dto.course}" no está disponible actualmente para este trámite.')
        # Solo las materias que el estudiante cursa (malla por curso y especialidad).
        from app.modules.curriculum import TEST_SUBJECT, specialty_of, subjects_for
        own = subjects_for(dto.course, dto.paralelo) + ((TEST_SUBJECT,) if allow_test else ())
        foreign = [s for s in dto.payload["subjects"] if s not in own]
        if foreign:
            raise BadRequest(f"Las siguientes materias no corresponden a tu curso: {', '.join(foreign)}")
        inactive = await self.config_service.inactive_subjects(dto.requestType, dto.course, specialty_of(dto.course, dto.paralelo))
        unavailable = [s for s in dto.payload["subjects"] if s in inactive]
        if unavailable:
            raise BadRequest(f"Las siguientes materias no están disponibles actualmente: {', '.join(unavailable)}")

    async def _validate_against_aporte_quota(self, dto: CreateRequestDTO) -> None:
        cupo = await self.plazos_service.get_cupo_info(dto.studentDni, dto.course, dto.requestType)
        if len(dto.payload["subjects"]) > cupo["disponible"]:
            raise BadRequest(
                f"El cupo de materias para este aporte es {cupo['limite']}. "
                f"Ya se han solicitado {cupo['consumido']}, quedan {cupo['disponible']} disponibles.")

    async def _next_request_number(self, request_type: str) -> str:
        year = datetime.now().year  # año del reloj local del servidor, como new Date().getFullYear()
        prefix = REQUEST_TYPE_METADATA[request_type].prefix
        seq = await self.counters.next_seq(f"{prefix}-{year}")
        return f"{prefix}-{year}-{seq:04d}"

    async def create(self, dto: CreateRequestDTO, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        from app.modules.catalog import paralelos_for, specialty_for_paralelo
        if dto.paralelo not in paralelos_for(dto.course, specialty_for_paralelo(dto.paralelo)):
            raise BadRequest(f'El paralelo "{dto.paralelo}" no existe para el curso elegido.')
        metadata = REQUEST_TYPE_METADATA[dto.requestType]
        if metadata.uses_academic_catalog:
            await self._validate_against_solicitud_config(dto, allow_test=bool((extra or {}).get("isTest")))
        if metadata.uses_aporte_quota:
            await self._validate_against_aporte_quota(dto)

        active = await self.requests.find_active_for_duplicates(
            dto.representativeDni, dto.studentName, dto.requestType, ACTIVE_STATUSES)
        if active:
            active_subjects = {s for r in active for s in _subjects(r)}
            duplicates = [s for s in dto.payload["subjects"] if s in active_subjects]
            if duplicates:
                raise Conflict(f"Ya existe una solicitud activa para las materias: {', '.join(duplicates)}")

        request_number = await self._next_request_number(dto.requestType)
        # Orden de campos igual al que produce Mongoose (schema order).
        doc = {
            "requestNumber": request_number,
            "requestType": dto.requestType,
            "status": metadata.initial_status.value,
            "representativeName": dto.representativeName,
            "representativeDni": dto.representativeDni,
            "studentName": dto.studentName,
            "studentDni": dto.studentDni,
            "course": dto.course,
            "paralelo": dto.paralelo,
            "campus": CAMPUS_BY_COURSE[dto.course].value,  # nunca el del cliente
            "payload": dto.payload,
            "pdfAccessToken": secrets.token_hex(24),
            **(extra or {}),
        }
        return await self.requests.insert(doc)

    # ── consultas ───────────────────────────────────────────────────────────
    async def find_all(self, filters: FilterRequestsDTO) -> dict[str, Any]:
        query: dict[str, Any] = {}
        if filters.status:
            query["status"] = filters.status
        if filters.course:
            query["course"] = filters.course
        if filters.paralelo:
            query["paralelo"] = filters.paralelo
        if filters.subject:
            query["payload.subjects"] = filters.subject
        if filters.search:
            regex = {"$regex": escape_regexp(filters.search), "$options": "i"}
            query["$or"] = [{"studentName": regex}, {"representativeName": regex}, {"requestNumber": regex},
                            {"representativeDni": regex}, {"studentDni": regex}]
        page, page_size = filters.page, filters.pageSize
        data, total = await self.requests.find_page(query, page, page_size)
        return {"data": data, "page": page, "pageSize": page_size, "total": total,
                "totalPages": max(1, -(-total // page_size))}

    async def find_by_id_raw(self, id_: str) -> dict[str, Any] | None:
        """Lectura sin efectos secundarios (la usa el control de acceso al PDF)."""
        if not is_valid_object_id(id_):
            return None
        return await self.requests.find_by_id(ObjectId(id_))

    async def find_one(self, id_: str, actor: Any = None) -> dict[str, Any]:
        request = await self.requests.find_by_id(ObjectId(id_))
        if request is None:
            raise NotFound(f"Solicitud con id {id_} no encontrada")
        meta = REQUEST_TYPE_METADATA.get(request.get("requestType"))
        # Auto-aprobación al abrir (solo si el tipo la tiene activada; proceso_mejora ya no).
        if meta and meta.auto_approve_on_view and request.get("status") == RequestStatus.EN_REVISION.value:
            updated = await self.requests.approve_if_status(
                request["_id"], RequestStatus.EN_REVISION.value, RequestStatus.APROBADO.value)
            if updated:
                from app.modules.audit.service import log_request_event
                await log_request_event(request["_id"], "AUTO_APPROVED", from_status="en_revision", to_status="aprobado",
                                        actor_id=actor.user_id if actor else None,
                                        actor_name=(actor.name or actor.username) if actor else "Sistema",
                                        actor_role=actor.role if actor else "SYSTEM")
            request = updated or await self.requests.find_by_id(request["_id"]) or request
        return request

    async def update_status(self, id_: str, status: str, reason: str | None, actor: Any = None,
                            ip: str | None = None) -> dict[str, Any]:
        """Cambio de estado del ADMIN: transición validada, condicionada al estado leído y auditada."""
        from app.modules.requests import workflow
        from app.modules.requests.state_machine import assert_admin_transition

        oid = ObjectId(id_)
        current = await self.requests.find_by_id(oid)
        if current is None:
            raise NotFound(f"Solicitud con id {id_} no encontrada")
        assert_admin_transition(current["status"], status)
        update: dict[str, Any] = {"status": status}
        # El motivo solo se graba al rechazar con uno; nunca se borra un motivo previo.
        if status == RequestStatus.RECHAZADO.value and reason:
            update["rejectionReason"] = reason
            update["rejectedAt"] = utcnow()
            if actor is not None:
                update["rejectedBy"] = actor.username
        event = workflow.status_event("STATUS_CHANGED", current["status"], status, actor, ip,
                                      reason if status == RequestStatus.RECHAZADO.value else None)
        return await workflow.update_with_event(oid, {"status": current["status"]}, update, event)
