from __future__ import annotations

from app.modules.auth.schemas import StrictModel
from app.modules.catalog import COURSES, CourseLiteral
from app.shared.validators import class_validator as cv


class AporteDTO(StrictModel):
    name: str
    startDate: str
    endDate: str


class TrimestreDTO(StrictModel):
    name: str
    startDate: str
    endDate: str
    aportes: list[AporteDTO] | None = None


class UpsertAnioLectivoDTO(StrictModel):
    name: str
    startDate: str
    endDate: str
    trimestres: list[TrimestreDTO] | None = None


class CupoQueryDTO(StrictModel):
    studentDni: str
    course: CourseLiteral


_RANGE_FIELDS = (
    cv.Field("name", (cv.is_string(), cv.is_not_empty())),
    cv.Field("startDate", (cv.is_date_string(),)),
    cv.Field("endDate", (cv.is_date_string(),)),
)
APORTE_SCHEMA = cv.Schema(_RANGE_FIELDS)
TRIMESTRE_SCHEMA = cv.Schema(_RANGE_FIELDS + (
    cv.Field("aportes", (cv.is_array(),), optional=True, nested=APORTE_SCHEMA, nested_each=True),
))
UPSERT_ANIO_SCHEMA = cv.Schema(_RANGE_FIELDS + (
    cv.Field("trimestres", (cv.is_array(),), optional=True, nested=TRIMESTRE_SCHEMA, nested_each=True),
))
CUPO_QUERY_SCHEMA = cv.Schema((
    cv.Field("studentDni", (cv.is_string(), cv.is_not_empty())),
    cv.Field("course", (cv.is_enum(COURSES),)),
))
