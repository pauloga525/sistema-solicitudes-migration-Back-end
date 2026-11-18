"""Catálogos del dominio (fuente única, equivalente a requests/schemas/request.schema.ts)."""
from __future__ import annotations

from enum import StrEnum
from typing import Literal


class RequestType(StrEnum):
    PROCESO_MEJORA = "proceso_mejora"


class RequestStatus(StrEnum):
    PENDIENTE = "pendiente"
    APROBADO = "aprobado"
    RECHAZADO = "rechazado"
    EN_REVISION = "en_revision"
    CANCELADO = "cancelado"


class Campus(StrEnum):
    CARLOS_CRESPI = "Carlos_Crespi"
    MARIA_AUXILIADORA = "Maria_Auxiliadora"
    YANUNCAY = "Yanuncay"


REQUEST_TYPES: tuple[str, ...] = tuple(t.value for t in RequestType)
REQUEST_STATUSES: tuple[str, ...] = tuple(s.value for s in RequestStatus)

COURSES: tuple[str, ...] = (
    "1ro_Basica", "2do_Basica", "3ro_Basica", "4to_Basica", "5to", "6to", "7mo",
    "8vo", "9no", "10mo", "1ro_Bach", "2do_Bach", "3ro_Bach",
)
# ── Paralelos por curso ───────────────────────────────────────────────────────
# 1ro de Básica: A–I · 2do a 7mo de Básica: A–G · 8vo a 10mo: A–J.
# Bachillerato: el paralelo depende de la especialidad (el código identifica la especialidad).
_LETTERS = tuple("ABCDEFGHIJ")
SPECIALTIES: dict[str, tuple[str, ...]] = {
    "Desarrollo de Software": ("E1", "E2"),
    "Mecatrónica": ("D1", "D2"),
    "Mecánica Industrial": ("B",),
    "Electromecánica Automotriz": ("C1", "C2"),
    "Ciencias": ("F1", "F2", "F3"),
    "Instalaciones Eléctricas y Automatización": ("A1", "A2"),
}
BACH_COURSES: tuple[str, ...] = ("1ro_Bach", "2do_Bach", "3ro_Bach")


def paralelos_for(course: str, specialty: str | None = None) -> tuple[str, ...]:
    if course == "1ro_Basica":
        return _LETTERS[:9]
    if course in ("8vo", "9no", "10mo"):
        return _LETTERS
    if course in BACH_COURSES:
        return SPECIALTIES.get(specialty or "", ())
    return _LETTERS[:7]


def specialty_for_paralelo(paralelo: str) -> str | None:
    return next((name for name, codes in SPECIALTIES.items() if paralelo in codes), None)


# Todos los valores posibles (A–G del original + H, I, J y los códigos de especialidad).
PARALELOS: tuple[str, ...] = _LETTERS + tuple(c for codes in SPECIALTIES.values() for c in codes if c not in _LETTERS)

ALLOWED_SUBJECTS: tuple[str, ...] = (
    "Matemática",
    "Lengua y literatura",
    "Science",
    "Estudios Sociales",
    "Inglés",
    "ECA",
    "Computación",
    "Animación a la lectura",
    "Educación física",
    "ERE",
    "Razonamiento lógico Matemático",
    "Acompañamiento integral en el Aula",
)

CAMPUS_BY_COURSE: dict[str, Campus] = {
    **{c: Campus.CARLOS_CRESPI for c in ("1ro_Basica", "2do_Basica", "3ro_Basica", "4to_Basica")},
    **{c: Campus.MARIA_AUXILIADORA for c in ("5to", "6to", "7mo")},
    **{c: Campus.YANUNCAY for c in ("8vo", "9no", "10mo", "1ro_Bach", "2do_Bach", "3ro_Bach")},
}

# Cupo de materias por aporte y por alumno: 4 de 1ro de Básica a 10mo, 6 en Bachillerato.
APORTE_QUOTA_BY_COURSE: dict[str, int] = {c: (6 if c.endswith("_Bach") else 4) for c in COURSES}

RequestTypeLiteral = Literal[REQUEST_TYPES]  # type: ignore[valid-type]
RequestStatusLiteral = Literal[REQUEST_STATUSES]  # type: ignore[valid-type]
CourseLiteral = Literal[COURSES]  # type: ignore[valid-type]
ParaleloLiteral = Literal[PARALELOS]  # type: ignore[valid-type]
