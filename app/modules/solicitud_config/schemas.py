from app.modules.auth.schemas import StrictModel
from app.shared.validators import class_validator as cv


class UpdateConfigItemDTO(StrictModel):
    name: str
    active: int | float


# update-config-item.dto.ts: @IsString name; @IsIn([0, 1]) active
UPDATE_ITEM_SCHEMA = cv.Schema((
    cv.Field("name", (cv.is_string(),)),
    cv.Field("active", (cv.is_in([0, 1]),)),
))


class UpdateCourseSubjectDTO(StrictModel):
    key: str
    name: str
    active: int | float


# Activación de una materia en un curso (clave "<curso>|<especialidad>").
UPDATE_COURSE_SUBJECT_SCHEMA = cv.Schema((
    cv.Field("key", (cv.is_string(),)),
    cv.Field("name", (cv.is_string(),)),
    cv.Field("active", (cv.is_in([0, 1]),)),
))
