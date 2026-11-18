"""Validaciones de identidad (equivalentes a frontend-publico/src/pages/utils/validators.ts).

En el original solo se ejecutaban en el navegador; aquí el backend también las
aplica (requisito 6.11 de la migración), con exactamente el mismo algoritmo.
"""
import re

_LETTERS = re.compile(r"^[a-zA-ZáéíóúüñÁÉÍÓÚÜÑ\s]+$")


def validate_ecuadorian_dni(dni: str) -> bool:
    """Algoritmo módulo 10 del Registro Civil ecuatoriano."""
    if not re.fullmatch(r"\d{10}", dni or ""):
        return False
    province = int(dni[0:2])
    if province < 1 or province > 24:
        return False
    if int(dni[2]) >= 6:
        return False
    coefficients = [2, 1, 2, 1, 2, 1, 2, 1, 2]
    total = 0
    for i in range(9):
        val = int(dni[i]) * coefficients[i]
        if val >= 10:
            val -= 9
        total += val
    check = 0 if total % 10 == 0 else 10 - (total % 10)
    return check == int(dni[9])


def validate_full_name(value: str) -> bool:
    """Al menos un nombre y dos apellidos (≥3 palabras de ≥2 caracteres)."""
    words = [w for w in (value or "").strip().split() if len(w) >= 2]
    return len(words) >= 3


def is_letters_only(value: str) -> bool:
    return bool(_LETTERS.match(value or ""))


def validate_person_name(value: str) -> bool:
    return is_letters_only(value) and validate_full_name(value)
