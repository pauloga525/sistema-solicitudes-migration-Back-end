from fastapi import APIRouter, Depends

from app.core.dependencies import get_db, json_body, require_admin
from app.core.exceptions import BadRequest
from app.core.rate_limit import Throttle
from app.modules.catalog import REQUEST_TYPES
from app.modules.solicitud_config.repository import SolicitudConfigRepository
from app.modules.solicitud_config.schemas import (
    UPDATE_COURSE_SUBJECT_SCHEMA, UPDATE_ITEM_SCHEMA, UpdateConfigItemDTO, UpdateCourseSubjectDTO,
)
from app.modules.solicitud_config.service import SolicitudConfigService
from app.shared.serialization import to_json
from app.shared.validators.class_validator import validate_dto

router = APIRouter(prefix="/solicitud-config", tags=["solicitud-config"])


def parse_request_type(request_type: str) -> str:
    """Equivalente a ParseEnumPipe(RequestType): impide crear configuraciones arbitrarias."""
    if request_type not in REQUEST_TYPES:
        raise BadRequest("Validation failed (enum string is expected)")
    return request_type


@router.get("/{request_type}", dependencies=[Depends(Throttle("SolicitudConfigController.getConfig"))])
async def get_config(request_type: str, db=Depends(get_db)):
    rt = parse_request_type(request_type)
    return to_json(await SolicitudConfigService(SolicitudConfigRepository(db)).get_config(rt))


@router.get("/{request_type}/curriculum",
            dependencies=[Depends(Throttle("SolicitudConfigController.curriculum")), Depends(require_admin)])
async def get_curriculum(request_type: str):
    """Materias por curso (unión de especialidades en bachillerato) y nombres históricos, para filtros."""
    parse_request_type(request_type)
    from app.modules.catalog import ALLOWED_SUBJECTS, COURSES
    from app.modules.curriculum import CURRICULUM_SUBJECTS
    from app.modules.curriculum_data import CURRICULUM
    order = {n: i for i, n in enumerate(CURRICULUM_SUBJECTS)}
    by_course = {c: sorted({s for k, v in CURRICULUM.items() if k.split("|")[0] == c for s in v}, key=order.__getitem__)
                 for c in COURSES}
    return {"byCourse": by_course, "bySpecialty": {k: list(v) for k, v in CURRICULUM.items()},
            "legacy": [s for s in ALLOWED_SUBJECTS if s not in order]}


@router.get("/{request_type}/course-subjects",
            dependencies=[Depends(Throttle("SolicitudConfigController.courseSubjects")), Depends(require_admin)])
async def get_course_subjects(request_type: str, db=Depends(get_db)):
    """Materias de cada curso (y especialidad en bachillerato) con su activación propia."""
    rt = parse_request_type(request_type)
    return {"groups": await SolicitudConfigService(SolicitudConfigRepository(db)).course_subjects(rt)}


@router.patch("/{request_type}/course-subjects",
              dependencies=[Depends(Throttle("SolicitudConfigController.updateCourseSubject")), Depends(require_admin)])
async def update_course_subject(request_type: str, body=Depends(json_body), db=Depends(get_db)):
    rt = parse_request_type(request_type)
    dto: UpdateCourseSubjectDTO = validate_dto(UPDATE_COURSE_SUBJECT_SCHEMA, body, UpdateCourseSubjectDTO)  # type: ignore[assignment]
    return await SolicitudConfigService(SolicitudConfigRepository(db)).update_course_subject(rt, dto.key, dto.name, int(dto.active))


@router.patch("/{request_type}/courses",
              dependencies=[Depends(Throttle("SolicitudConfigController.updateCourse")), Depends(require_admin)])
async def update_course(request_type: str, body=Depends(json_body), db=Depends(get_db)):
    rt = parse_request_type(request_type)
    dto: UpdateConfigItemDTO = validate_dto(UPDATE_ITEM_SCHEMA, body, UpdateConfigItemDTO)  # type: ignore[assignment]
    return to_json(await SolicitudConfigService(SolicitudConfigRepository(db)).update_course(rt, dto.name, int(dto.active)))


@router.patch("/{request_type}/subjects",
              dependencies=[Depends(Throttle("SolicitudConfigController.updateSubject")), Depends(require_admin)])
async def update_subject(request_type: str, body=Depends(json_body), db=Depends(get_db)):
    rt = parse_request_type(request_type)
    dto: UpdateConfigItemDTO = validate_dto(UPDATE_ITEM_SCHEMA, body, UpdateConfigItemDTO)  # type: ignore[assignment]
    return to_json(await SolicitudConfigService(SolicitudConfigRepository(db)).update_subject(rt, dto.name, int(dto.active)))
