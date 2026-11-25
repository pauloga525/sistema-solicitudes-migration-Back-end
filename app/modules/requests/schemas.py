from __future__ import annotations

from typing import Any

from app.modules.auth.schemas import StrictModel
from app.modules.catalog import (
    COURSES, PARALELOS, REQUEST_STATUSES, REQUEST_TYPES,
    CourseLiteral, ParaleloLiteral, RequestStatusLiteral, RequestTypeLiteral,
)
from app.modules.curriculum import ALL_SUBJECTS, TEST_SUBJECT
from app.shared.validators import class_validator as cv
from app.shared.validators.cedula import validate_ecuadorian_dni, validate_person_name


class ProcesoMejoraPayloadDTO(StrictModel):
    subjects: list[str]


class CreateRequestDTO(StrictModel):
    requestType: RequestTypeLiteral
    representativeName: str
    representativeDni: str
    studentName: str
    studentDni: str
    course: CourseLiteral
    paralelo: ParaleloLiteral
    payload: dict[str, Any]


class UpdateStatusDTO(StrictModel):
    status: RequestStatusLiteral
    reason: str | None = None


class FilterRequestsDTO(StrictModel):
    status: RequestStatusLiteral | None = None
    course: CourseLiteral | None = None
    paralelo: ParaleloLiteral | None = None
    subject: str | None = None
    search: str | None = None
    page: int = 1
    pageSize: int = 20


# create-request.dto.ts → ProcesoMejoraPayloadDto: @IsArray @IsEnum(each) @ArrayMinSize(1)
PROCESO_MEJORA_PAYLOAD_SCHEMA = cv.Schema((
    cv.Field("subjects", (cv.is_array(), cv.is_enum_each_of_array_constant(ALL_SUBJECTS + (TEST_SUBJECT,)), cv.array_min_size(1))),
))

# Qué esquema valida `payload` según `requestType` (PAYLOAD_DTO_BY_TYPE del original).
PAYLOAD_SCHEMA_BY_TYPE: dict[str, cv.Schema] = {"proceso_mejora": PROCESO_MEJORA_PAYLOAD_SCHEMA}

_DNI_MSG = "{property} debe ser una cédula ecuatoriana válida."
_NAME_MSG = "{property} debe contener al menos un nombre y dos apellidos (solo letras)."


def _text(validator=None, message: str = "") -> tuple:
    # Las reglas propias (validación de identidad, requisito 6.11) se declaran primero
    # para que aparezcan después de los mensajes estándar de class-validator.
    extra = (cv.custom(validator, message),) if validator else ()
    return extra + (cv.is_string(), cv.is_not_empty())


def create_request_schema(request_type: Any) -> cv.Schema:
    payload_schema = PAYLOAD_SCHEMA_BY_TYPE.get(request_type, PROCESO_MEJORA_PAYLOAD_SCHEMA)
    return cv.Schema((
        cv.Field("requestType", (cv.is_enum(REQUEST_TYPES),)),
        cv.Field("representativeName", _text(validate_person_name, _NAME_MSG)),
        cv.Field("representativeDni", _text(validate_ecuadorian_dni, _DNI_MSG)),
        cv.Field("studentName", _text(validate_person_name, _NAME_MSG)),
        cv.Field("studentDni", _text(validate_ecuadorian_dni, _DNI_MSG)),
        cv.Field("course", (cv.is_enum(COURSES),)),
        cv.Field("paralelo", (cv.is_enum(PARALELOS),)),
        cv.Field("payload", (), nested=payload_schema),
    ))


UPDATE_STATUS_SCHEMA = cv.Schema((
    cv.Field("status", (cv.is_enum(REQUEST_STATUSES),)),
    cv.Field("reason", (cv.is_string(),), optional=True),
))

# filter-requests.dto.ts (query string; page/pageSize pasan por Number() como @Type)
FILTER_REQUESTS_SCHEMA = cv.Schema(
    (
        cv.Field("status", (cv.is_enum(REQUEST_STATUSES),), optional=True),
        cv.Field("course", (cv.is_enum(COURSES),), optional=True),
        cv.Field("paralelo", (cv.is_enum(PARALELOS),), optional=True),
        cv.Field("subject", (cv.is_string(),), optional=True),
        cv.Field("search", (cv.is_string(),), optional=True),
        cv.Field("page", (cv.is_int(), cv.min_value(1)), optional=True),
        cv.Field("pageSize", (cv.is_int(), cv.min_value(1), cv.max_value(200)), optional=True),
    ),
    transforms={"page": cv.js_number, "pageSize": cv.js_number},
)
