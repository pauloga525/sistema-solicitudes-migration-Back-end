"""Fechas con la misma semántica que `new Date(string)` de JavaScript."""
from __future__ import annotations

import re
from datetime import date, datetime, timezone

_DATE_ONLY = re.compile(r"^([+-]?\d{4,6})(?:-(\d{2})(?:-(\d{2}))?)?$")


def utcnow() -> datetime:
    """Fecha actual en UTC con precisión de milisegundos (la que guarda MongoDB)."""
    now = datetime.now(timezone.utc)
    return now.replace(microsecond=(now.microsecond // 1000) * 1000)


def parse_js_date(value: str) -> datetime:
    """Convierte un string ISO 8601 a datetime UTC como lo haría JavaScript.

    - Solo fecha ("2026-01-01")      → medianoche UTC.
    - Fecha-hora sin zona             → hora local del servidor.
    - Fecha-hora con Z u offset       → se respeta la zona.
    """
    s = value.strip()
    m = _DATE_ONLY.match(s)
    if m:
        year, month, day = int(m.group(1)), int(m.group(2) or 1), int(m.group(3) or 1)
        return datetime(year, month, day, tzinfo=timezone.utc)
    norm = s.replace(" ", "T", 1)
    norm = re.sub(r"[zZ]$", "+00:00", norm)
    norm = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", norm)
    norm = re.sub(r"([+-]\d{2})$", r"\1:00", norm)
    norm = norm.replace(",", ".")
    dt = datetime.fromisoformat(norm)
    if dt.tzinfo is None:
        dt = dt.astimezone()  # hora local, como JS
    dt = dt.astimezone(timezone.utc)
    return dt.replace(microsecond=(dt.microsecond // 1000) * 1000)


def to_js_iso(value: datetime | date) -> str:
    """Formato de `Date.prototype.toJSON()`: 2026-11-11T16:10:46.123Z."""
    if not isinstance(value, datetime):
        value = datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    value = value.astimezone(timezone.utc)
    return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"
