import hmac

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response

from app.core.config import get_settings
from app.core.dependencies import AuthUser, client_ip, get_db, json_body, query_dict, require_admin, require_student, resolve_session
from app.core.exceptions import AppError, Unauthorized
from app.core.rate_limit import Throttle
from app.modules.plazos.repository import AnioLectivoRepository, RequestReadRepository
from app.modules.plazos.service import PlazosService
from app.modules.requests.pdf_service import PdfService
from app.modules.requests.repository import CounterRepository, RequestRepository
from app.modules.requests.schemas import (
    FILTER_REQUESTS_SCHEMA, UPDATE_STATUS_SCHEMA, CreateRequestDTO, FilterRequestsDTO, UpdateStatusDTO,
    create_request_schema,
)
from app.modules.requests.service import RequestsService
from app.modules.solicitud_config.repository import SolicitudConfigRepository
from app.modules.solicitud_config.service import SolicitudConfigService
from app.shared.serialization import to_json
from app.shared.validators.class_validator import validate_dto
from app.shared.validators.object_id import is_valid_object_id, parse_object_id

router = APIRouter(prefix="/requests", tags=["requests"])
PDF_FORBIDDEN = "No autorizado para acceder a este documento."


def service(db=Depends(get_db)) -> RequestsService:
    return RequestsService(
        RequestRepository(db), CounterRepository(db),
        SolicitudConfigService(SolicitudConfigRepository(db)),
        PlazosService(AnioLectivoRepository(db), RequestReadRepository(db)),
    )


def pdf_service() -> PdfService:
    return PdfService(get_settings().assets_dir)


def _create_dto(body) -> CreateRequestDTO:
    rt = body.get("requestType") if isinstance(body, dict) else None
    return validate_dto(create_request_schema(rt), body, CreateRequestDTO)  # type: ignore[return-value]


async def _assert_identity(user: AuthUser, dto: CreateRequestDTO) -> None:
    """Contrato original del formulario, ahora con sesión de estudiante: la identidad debe coincidir con su perfil."""
    from app.database.connection import get_database
    from app.modules.students import service as st
    from app.shared.crypto.field_encryption import hash_field

    prof = await st.get_profile(user.oid)
    if not prof or not prof.get("completed"):
        raise AppError(400, "Completa tus datos y los de tu representante antes de enviar solicitudes.", code="PROFILE_INCOMPLETE")
    rep_ids = [l["representativeId"] async for l in get_database()["student_representatives"].find(
        {"studentUserId": user.oid, "active": True})]
    rep = await get_database()["representatives"].find_one({"_id": {"$in": rep_ids}, "cedulaHash": hash_field(dto.representativeDni)})
    if prof.get("cedulaHash") != hash_field(dto.studentDni) or not rep:
        raise AppError(403, "Los datos de la solicitud no coinciden con tu perfil o tus representantes.", code="IDENTITY_MISMATCH")


@router.post("", status_code=201, dependencies=[Depends(Throttle("RequestsController.create", 10))])
async def create(request: Request, body=Depends(json_body), svc: RequestsService = Depends(service)):
    user = await require_student(request, await _active(request))
    dto = _create_dto(body)
    await _assert_identity(user, dto)
    from app.modules.requests.workflow import create_request
    return JSONResponse(status_code=201, content=to_json(await create_request(svc, dto, user, client_ip(request))))


@router.post("/preview", status_code=201, dependencies=[Depends(Throttle("RequestsController.previewPdf"))])
async def preview(request: Request, body=Depends(json_body), pdf: PdfService = Depends(pdf_service)):
    user = await require_student(request, await _active(request))
    from app.modules.system.submission_window import assert_open
    await assert_open()
    dto = _create_dto(body)
    await _assert_identity(user, dto)
    return Response(content=await pdf.generate_preview_pdf(dto), status_code=201, media_type="application/pdf",
                    headers={"Content-Disposition": 'inline; filename="preview.pdf"'})


async def _active(request: Request) -> AuthUser:
    from app.core.dependencies import _check_restrictions, current_user
    user = await current_user(request)
    await _check_restrictions(user, False)
    return user


@router.get("", dependencies=[Depends(Throttle("RequestsController.findAll")), Depends(require_admin)])
async def find_all(request: Request, svc: RequestsService = Depends(service)):
    filters: FilterRequestsDTO = validate_dto(FILTER_REQUESTS_SCHEMA, query_dict(request), FilterRequestsDTO)  # type: ignore[assignment]
    result = await svc.find_all(filters)
    # Seguimiento docente por materia (programada, evaluado, no se presentó…), además del estado administrativo.
    rows = result.get("data") if isinstance(result, dict) else None
    if rows:
        from app.modules.teachers.service import evaluation_summary
        ev = await evaluation_summary(rows)
        for r in rows:
            r["evaluations"] = ev.get(str(r["_id"]), [])
    return to_json(result)


@router.get("/{id_}", dependencies=[Depends(Throttle("RequestsController.findOne"))])
async def find_one(id_: str, request: Request, svc: RequestsService = Depends(service)):
    user = await require_admin(request)
    parse_object_id(id_)
    return to_json(await svc.find_one(id_, actor=user))


@router.get("/{id_}/history", dependencies=[Depends(Throttle("RequestsController.history"))])
async def history(id_: str, request: Request):
    """Historial de auditoría para el ADMIN (solo lectura; no dispara la auto-aprobación)."""
    from app.modules.audit.service import request_history
    await require_admin(request)
    return to_json(await request_history(parse_object_id(id_)))


@router.patch("/{id_}/status", dependencies=[Depends(Throttle("RequestsController.updateStatus"))])
async def update_status(id_: str, request: Request, svc: RequestsService = Depends(service)):
    user = await require_admin(request)
    parse_object_id(id_)
    body = await json_body(request)
    dto: UpdateStatusDTO = validate_dto(UPDATE_STATUS_SCHEMA, body, UpdateStatusDTO)  # type: ignore[assignment]
    return to_json(await svc.update_status(id_, dto.status, dto.reason, user, client_ip(request)))


@router.get("/{id_}/pdf", dependencies=[Depends(Throttle("RequestsController.getPdf"))])
async def get_pdf(id_: str, request: Request, svc: RequestsService = Depends(service),
                  pdf: PdfService = Depends(pdf_service)):
    # Acceso: (a) sesión de ADMIN/SUPERADMIN, (b) sesión del estudiante dueño, o (c) ?token= igual al pdfAccessToken.
    session_user = await resolve_session(request)
    owner_ok = False
    if session_user is not None and session_user.role == "STUDENT" and is_valid_object_id(id_):
        raw = await svc.find_by_id_raw(id_)
        owner_ok = bool(raw and raw.get("studentUserId") == session_user.oid)
    if not (owner_ok or (session_user is not None and session_user.role in ("ADMIN", "SUPERADMIN")
                         and not session_user.force_password_change)):
        token = request.query_params.get("token")
        doc = await svc.find_by_id_raw(id_) if (token and is_valid_object_id(id_)) else None
        if not (doc and doc.get("pdfAccessToken") and hmac.compare_digest(str(doc["pdfAccessToken"]), token)):
            raise Unauthorized(PDF_FORBIDDEN)
    parse_object_id(id_)
    actor = session_user if session_user is not None and session_user.role != "STUDENT" else None
    if session_user is not None and session_user.role == "STUDENT":
        req = await svc.find_by_id_raw(id_)
    else:
        req = await svc.find_one(id_, actor=actor)  # incluye la auto-aprobación, igual que el original
    return Response(content=await pdf.generate_request_pdf(req), media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="solicitud-{req["requestNumber"]}.pdf"'})
