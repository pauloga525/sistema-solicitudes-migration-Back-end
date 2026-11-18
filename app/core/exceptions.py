"""Errores HTTP con el mismo formato de respuesta que NestJS.

El frontend lee siempre `message` (string o lista de strings), así que el
contrato de error se conserva exactamente: {"message", "error", "statusCode"}.
"""
from __future__ import annotations

import logging
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger("app.errors")


class AppError(Exception):
    """Excepción de dominio que se traduce a una respuesta HTTP estilo NestJS."""

    def __init__(self, status_code: int, message: str | list[str], *, include_error: bool = True,
                 headers: dict | None = None, code: str | None = None):
        super().__init__(message if isinstance(message, str) else "; ".join(message))
        self.status_code = status_code
        self.message = message
        self.include_error = include_error
        self.headers = headers or {}
        self.code = code

    def body(self) -> dict[str, Any]:
        body: dict[str, Any] = {"message": self.message}
        if self.include_error:
            body["error"] = _phrase(self.status_code)
        body["statusCode"] = self.status_code
        if self.code:
            body["code"] = self.code
        return body


def _phrase(status: int) -> str:
    try:
        return HTTPStatus(status).phrase
    except ValueError:  # pragma: no cover
        return "Error"


class BadRequest(AppError):
    def __init__(self, message: str | list[str]):
        super().__init__(400, message)


class NotFound(AppError):
    def __init__(self, message: str):
        super().__init__(404, message)


class Conflict(AppError):
    def __init__(self, message: str):
        super().__init__(409, message)


class Unauthorized(AppError):
    def __init__(self, message: str | None = None):
        # Sin mensaje propio, NestJS responde {"message":"Unauthorized","statusCode":401}
        if message is None:
            super().__init__(401, "Unauthorized", include_error=False)
        else:
            super().__init__(401, message)


class PayloadTooLarge(AppError):
    def __init__(self, message: str = "File too large"):
        super().__init__(413, message)


class TooManyRequests(AppError):
    def __init__(self, retry_after: int):
        super().__init__(429, "ThrottlerException: Too Many Requests", include_error=False,
                         headers={"Retry-After": str(retry_after)})

    def body(self) -> dict[str, Any]:
        return {"statusCode": 429, "message": self.message}


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError):
        return JSONResponse(status_code=exc.status_code, content=exc.body(), headers=exc.headers)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException):
        if exc.status_code in (404, 405):
            # NestJS/Express responde 404 tanto para ruta inexistente como para método no soportado.
            return JSONResponse(
                status_code=404,
                content={"message": f"Cannot {request.method} {request.url.path}", "error": "Not Found", "statusCode": 404},
            )
        return JSONResponse(
            status_code=exc.status_code,
            content={"message": exc.detail, "error": _phrase(exc.status_code), "statusCode": exc.status_code},
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        # Endpoints nuevos validados con Pydantic: mensaje por campo, sin exponer detalles internos.
        msgs = []
        for e in exc.errors():
            loc = ".".join(str(p) for p in e.get("loc", ()) if p not in ("body", "query", "path"))
            kind = e.get("type", "")
            if kind == "extra_forbidden":
                msgs.append(f"property {loc} should not exist")
            elif kind == "missing":
                msgs.append(f"{loc} es obligatorio")
            else:
                msgs.append(f"{loc}: {_spanish(kind, e.get('msg', ''), e.get('ctx') or {})}")
        return JSONResponse(status_code=400, content={"message": msgs or ["Datos de entrada inválidos"],
                                                      "error": "Bad Request", "statusCode": 400})

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        logger.exception("Error no controlado en %s %s", request.method, request.url.path)
        try:
            from app.modules.system.service import log_error

            user = getattr(request.state, "session_user", None)
            await log_error("ERROR", f"{request.method} {request.url.path}", f"{type(exc).__name__}: {exc}", exc=exc,
                            path=request.url.path, method=request.method, user_id=getattr(user, "user_id", None))
        except Exception:  # noqa: BLE001
            pass
        # Nunca se envía la traza al navegador.
        return JSONResponse(status_code=500, content={"statusCode": 500, "message": "Internal server error"})


def _spanish(kind: str, msg: str, ctx: dict) -> str:
    table = {
        "string_too_short": f"debe tener al menos {ctx.get('min_length')} caracteres",
        "string_too_long": f"no puede superar {ctx.get('max_length')} caracteres",
        "string_pattern_mismatch": "formato inválido",
        "literal_error": f"valor no permitido (permitidos: {ctx.get('expected')})",
        "int_parsing": "debe ser un número entero", "int_type": "debe ser un número entero",
        "greater_than_equal": f"debe ser mayor o igual a {ctx.get('ge')}",
        "less_than_equal": f"debe ser menor o igual a {ctx.get('le')}",
        "bool_type": "debe ser verdadero o falso", "bool_parsing": "debe ser verdadero o falso",
        "string_type": "debe ser texto", "value_error": msg.replace("Value error, ", ""),
        "datetime_from_date_parsing": "fecha y hora inválidas", "datetime_parsing": "fecha y hora inválidas",
        "date_from_datetime_parsing": "fecha inválida", "list_type": "debe ser una lista",
    }
    return table.get(kind, "valor inválido")
