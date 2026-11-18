from pydantic import BaseModel, ConfigDict, Field

from app.shared.validators import class_validator as cv


class StrictModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class LoginDTO(StrictModel):
    """`username` acepta el usuario del admin original o un correo (estudiantes y SUPERADMIN)."""

    username: str
    password: str


# Reglas en el mismo orden que login.dto.ts (@IsString, @IsNotEmpty).
LOGIN_SCHEMA = cv.Schema((
    cv.Field("username", (cv.is_string(), cv.is_not_empty())),
    cv.Field("password", (cv.is_string(), cv.is_not_empty())),
))


class RegisterIn(StrictModel):
    email: str = Field(min_length=5, max_length=254)
    password: str = Field(max_length=256)
    confirmPassword: str = Field(max_length=256)


class ChangePasswordIn(StrictModel):
    currentPassword: str = Field("", max_length=256)
    newPassword: str = Field(max_length=256)
    confirmPassword: str = Field(max_length=256)


class GoogleLoginIn(StrictModel):
    credential: str = Field(min_length=20, max_length=4096)
