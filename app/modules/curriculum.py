"""Materias que cursa cada estudiante según curso y especialidad (malla de la UETS).

La lista sale de app/modules/curriculum_data.py (generado desde el Excel de la institución).
Los paralelos de un mismo curso y especialidad comparten materias, así que la clave es
curso + especialidad (la especialidad se deduce del paralelo en bachillerato).
"""
from __future__ import annotations

import re
import unicodedata

from app.modules.catalog import ALLOWED_SUBJECTS, BACH_COURSES, specialty_for_paralelo
from app.modules.curriculum_data import CURRICULUM

# Unión de todas las materias de la malla (orden: troncales y luego módulos, alfabético).
CURRICULUM_SUBJECTS: tuple[str, ...] = tuple(dict.fromkeys(s for v in CURRICULUM.values() for s in v))


def _sort_key(name: str) -> tuple[bool, str]:
    plain = "".join(c for c in unicodedata.normalize("NFD", name.casefold()) if unicodedata.category(c) != "Mn")
    return name.startswith("Mod."), re.sub(r"[^a-z0-9]", "", plain)


CURRICULUM_SUBJECTS = tuple(sorted(CURRICULUM_SUBJECTS, key=_sort_key))
# Nombres válidos en datos: malla actual + catálogo histórico (solicitudes antiguas y filtros).
ALL_SUBJECTS: tuple[str, ...] = CURRICULUM_SUBJECTS + tuple(s for s in ALLOWED_SUBJECTS if s not in CURRICULUM_SUBJECTS)


# Materia ficticia para pruebas: no está en la malla ni en "Cursos y materias". Solo la ven las cuentas de
# estudiante marcadas como "de prueba" por el SUPERADMIN, y solo se asigna a mano a un docente.
TEST_SUBJECT = "Materia de prueba (sistema)"


def specialty_of(course: str, paralelo: str | None) -> str | None:
    """Especialidad (solo bachillerato), deducida del paralelo."""
    return specialty_for_paralelo(paralelo or "") if course in BACH_COURSES else None


def subjects_for(course: str, paralelo: str | None = None) -> tuple[str, ...]:
    """Materias del curso (y de la especialidad que indica el paralelo, en bachillerato)."""
    return CURRICULUM.get(f"{course}|{specialty_of(course, paralelo) or ''}", ())
