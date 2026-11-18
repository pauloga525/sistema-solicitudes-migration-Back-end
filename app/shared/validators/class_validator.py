"""Motor de validación declarativo compatible con el de NestJS/class-validator.

¿Por qué existe? El frontend muestra el campo `message` de los errores 400, y el
backend original devuelve los textos exactos de class-validator
(p. ej. "course must be one of the following values: ..."). Para no cambiar ese
contrato, cada DTO declara aquí sus reglas en el mismo orden que los decoradores
del original y este motor reproduce:

- whitelist + forbidNonWhitelisted  → "property x should not exist" (primero)
- restricciones de cada propiedad en orden inverso de declaración (como class-validator)
- validación anidada con prefijo de ruta ("trimestres.0.name ...")
- @IsOptional: solo omite null/undefined

Tras pasar este motor, los datos se cargan en un modelo Pydantic v2 estricto
(`extra='forbid'`), que es el objeto tipado que reciben los servicios.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from pydantic import BaseModel, ValidationError

from app.core.exceptions import BadRequest


class _Missing:
    def __repr__(self) -> str:  # pragma: no cover
        return "MISSING"


MISSING: Any = _Missing()


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


# ISO 8601 tal como lo valida validator.js (isISO8601, modo no estricto).
_ISO8601 = re.compile(
    r"^([\+-]?\d{4}(?!\d{2}\b))((-?)((0[1-9]|1[0-2])(\3([12]\d|0[1-9]|3[01]))?|W([0-4]\d|5[0-3])(-?[1-7])?"
    r"|(00[1-9]|0[1-9]\d|[12]\d{2}|3([0-5]\d|6[1-6])))([T\s]((([01]\d|2[0-3])((:?)[0-5]\d)?|24:?00)"
    r"([\.,]\d+(?!:))?)?(\17[0-5]\d([\.,]\d+)?)?([zZ]|([\+-])([01]\d|2[0-3]):?([0-5]\d)?)?)?)?$"
)


@dataclass(frozen=True)
class Rule:
    check: Callable[[Any], bool]
    message: Callable[[str], str]


def is_string() -> Rule:
    return Rule(lambda v: isinstance(v, str), lambda p: f"{p} must be a string")


def is_not_empty() -> Rule:
    return Rule(lambda v: v is not MISSING and v is not None and v != "", lambda p: f"{p} should not be empty")


def is_enum(values: Sequence[str]) -> Rule:
    allowed = tuple(values)
    return Rule(lambda v: isinstance(v, str) and v in allowed,
                lambda p: f"{p} must be one of the following values: {', '.join(allowed)}")


def is_enum_each_of_array_constant(values: Sequence[str]) -> Rule:
    """@IsEnum(ALLOWED_SUBJECTS, {each: true}) del original.

    ALLOWED_SUBJECTS es un arreglo, y class-validator lista los valores de un
    "enum" filtrando claves numéricas, por eso el mensaje original sale con la
    lista vacía. Se conserva el texto exacto (es lo que ve el usuario).
    """
    allowed = tuple(values)

    def check(v: Any) -> bool:
        if isinstance(v, list):
            return all(isinstance(x, str) and x in allowed for x in v)
        return isinstance(v, str) and v in allowed

    return Rule(check, lambda p: f"each value in {p} must be one of the following values: ")


def is_in(values: Sequence[Any]) -> Rule:
    allowed = tuple(values)
    return Rule(lambda v: _is_number(v) and v in allowed,
                lambda p: f"{p} must be one of the following values: {', '.join(str(x) for x in allowed)}")


def is_array() -> Rule:
    return Rule(lambda v: isinstance(v, list), lambda p: f"{p} must be an array")


def array_min_size(n: int) -> Rule:
    return Rule(lambda v: isinstance(v, list) and len(v) >= n, lambda p: f"{p} must contain at least {n} elements")


def is_date_string() -> Rule:
    return Rule(lambda v: isinstance(v, str) and bool(_ISO8601.match(v)),
                lambda p: f"{p} must be a valid ISO 8601 date string")


def is_int() -> Rule:
    return Rule(lambda v: _is_number(v) and not (isinstance(v, float) and (math.isnan(v) or not v.is_integer())),
                lambda p: f"{p} must be an integer number")


def min_value(n: float) -> Rule:
    return Rule(lambda v: _is_number(v) and not math.isnan(v) and v >= n, lambda p: f"{p} must not be less than {n:g}")


def max_value(n: float) -> Rule:
    return Rule(lambda v: _is_number(v) and not math.isnan(v) and v <= n, lambda p: f"{p} must not be greater than {n:g}")


def custom(check: Callable[[Any], bool], message: str) -> Rule:
    """Regla propia de esta migración (no existía en el original).

    Solo se evalúa sobre strings no vacíos para no duplicar los mensajes de
    "should not be empty"/"must be a string".
    """
    return Rule(lambda v: not (isinstance(v, str) and v != "") or check(v), lambda p: message.format(property=p))


@dataclass(frozen=True)
class Field:
    name: str
    rules: tuple[Rule, ...] = ()  # en orden de declaración (como los decoradores)
    optional: bool = False
    nested: "Schema | None" = None
    nested_each: bool = False


@dataclass(frozen=True)
class Schema:
    fields: tuple[Field, ...]
    transforms: dict[str, Callable[[Any], Any]] = field(default_factory=dict)

    @property
    def names(self) -> set[str]:
        return {f.name for f in self.fields}


def collect_errors(schema: Schema, data: Any, prefix: str = "") -> list[str]:
    if not isinstance(data, dict):
        return [f"{prefix}an unknown value was passed to the validate function"]
    errors: list[str] = []
    for key in data:
        if key not in schema.names:
            errors.append(f"{prefix}property {key} should not exist")
    for f in schema.fields:
        value = data.get(f.name, MISSING)
        if f.optional and (value is MISSING or value is None):
            continue
        for rule in reversed(f.rules):
            if not rule.check(value):
                errors.append(prefix + rule.message(f.name))
        if f.nested is not None:
            errors.extend(_nested_errors(f, value, prefix))
    return errors


def _nested_errors(f: Field, value: Any, prefix: str) -> list[str]:
    assert f.nested is not None
    if f.nested_each:
        if not isinstance(value, list):
            return [f"{prefix}each value in nested property {f.name} must be either object or array"]
        out: list[str] = []
        for i, item in enumerate(value):
            if not isinstance(item, dict):
                out.append(f"{prefix}each value in nested property {f.name} must be either object or array")
            else:
                out.extend(collect_errors(f.nested, item, f"{prefix}{f.name}.{i}."))
        return out
    if not isinstance(value, dict):
        return [f"{prefix}nested property {f.name} must be either object or array"]
    return collect_errors(f.nested, value, f"{prefix}{f.name}.")


def validate_dto(schema: Schema, data: Any, model: type[BaseModel]) -> BaseModel:
    """Valida con reglas class-validator y devuelve el modelo Pydantic estricto."""
    if isinstance(data, dict):
        data = {k: (schema.transforms[k](v) if k in schema.transforms else v) for k, v in data.items()}
    errors = collect_errors(schema, data)
    if errors:
        raise BadRequest(errors)
    try:
        return model.model_validate(data, strict=True)
    except ValidationError as exc:  # no debería ocurrir: el motor ya filtró los datos
        raise BadRequest([f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()]) from exc


def js_number(value: Any) -> Any:
    """Conversión `Number(x)` de JavaScript (usada por @Type(() => Number) en query)."""
    if isinstance(value, list):
        return float("nan")
    if _is_number(value):
        return value
    if not isinstance(value, str):
        return float("nan")
    s = value.strip()
    if s == "":
        return 0
    try:
        if re.fullmatch(r"[+-]?\d+", s):
            return int(s)
        if s.lower().startswith(("0x", "0b", "0o")):
            return int(s, 0)
        f = float(s)
        return int(f) if f.is_integer() and "e" not in s.lower() and "." not in s else f
    except ValueError:
        return float("nan")
