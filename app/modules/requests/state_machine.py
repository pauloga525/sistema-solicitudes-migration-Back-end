"""Máquina de estados de las solicitudes (los 5 estados originales con reglas de transición)."""
from __future__ import annotations

from app.core.exceptions import AppError

PENDIENTE, EN_REVISION, APROBADO, RECHAZADO, CANCELADO = "pendiente", "en_revision", "aprobado", "rechazado", "cancelado"
ACTIVE = (PENDIENTE, EN_REVISION)
FINAL = (APROBADO, RECHAZADO, CANCELADO)
STATUS_LABELS = {PENDIENTE: "Pendiente", EN_REVISION: "En revisión", APROBADO: "Aprobada", RECHAZADO: "Negada",
                 CANCELADO: "Cancelada"}

TRANSITIONS: dict[str, frozenset[str]] = {
    PENDIENTE: frozenset({EN_REVISION, APROBADO, RECHAZADO, CANCELADO}),
    EN_REVISION: frozenset({APROBADO, RECHAZADO, CANCELADO}),
    APROBADO: frozenset(), RECHAZADO: frozenset(), CANCELADO: frozenset(),
}
# Solo el SUPERADMIN, con motivo: reabrir un estado final.
REOPEN: dict[str, frozenset[str]] = {s: frozenset({PENDIENTE, EN_REVISION}) for s in FINAL}
MIN_CORRECTION_REASON = 10


def superadmin_targets(current: str) -> list[str]:
    return sorted(TRANSITIONS.get(current, frozenset()) | REOPEN.get(current, frozenset()))


def assert_admin_transition(current: str, target: str) -> None:
    """ADMIN: transiciones normales. Repetir el mismo estado se permite (p. ej. agregar el motivo de una negación)."""
    if target == current:
        return
    if target not in TRANSITIONS.get(current, frozenset()):
        raise AppError(409, f"No se puede pasar de «{STATUS_LABELS.get(current, current)}» a "
                            f"«{STATUS_LABELS.get(target, target)}». Las solicitudes {STATUS_LABELS.get(current, current).lower()}s "
                            "solo las puede corregir el SUPERADMIN." if current in FINAL else
                       f"Transición no permitida: {current} → {target}", code="INVALID_TRANSITION")


def assert_superadmin_transition(current: str, target: str) -> None:
    if target == current:
        raise AppError(409, "La solicitud ya tiene ese estado", code="INVALID_TRANSITION")
    if target not in superadmin_targets(current):
        raise AppError(409, f"Transición no permitida: {current} → {target}", code="INVALID_TRANSITION")
