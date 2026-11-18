"""Interpreta el texto crudo de IDCCurso (port de padron-course-parser.ts).

Best-effort: si no reconoce el patrón devuelve course=None sin error; la fila
se guarda igual con cursoTexto tal cual vino.
"""
from __future__ import annotations

import re
import unicodedata

ORDINAL_TO_INDEX = {
    "primero": 1, "primer": 1, "segundo": 2, "tercero": 3, "tercer": 3, "cuarto": 4, "quinto": 5,
    "sexto": 6, "septimo": 7, "séptimo": 7, "octavo": 8, "noveno": 9, "decimo": 10, "décimo": 10,
}
BASICA_BY_INDEX = {1: "1ro_Basica", 2: "2do_Basica", 3: "3ro_Basica", 4: "4to_Basica", 5: "5to", 6: "6to",
                   7: "7mo", 8: "8vo", 9: "9no", 10: "10mo"}
BACH_BY_INDEX = {1: "1ro_Bach", 2: "2do_Bach", 3: "3ro_Bach"}

_NUMERIC = re.compile(r"\b(\d{1,2})\s*(?:ro|do|to|vo|no|mo)?\b", re.ASCII)


def _normalize(text: str) -> str:
    nfd = unicodedata.normalize("NFD", text.lower())
    return re.sub(r"[̀-ͯ]", "", nfd).strip()


def parse_curso_texto(curso_texto: str) -> dict[str, str | None]:
    normalized = _normalize(curso_texto)
    is_bach = re.search(r"\bbach", normalized, re.ASCII) is not None
    index: int | None = None
    m = _NUMERIC.search(normalized)
    if m:
        index = int(m.group(1))
    else:
        for word, value in ORDINAL_TO_INDEX.items():
            if re.search(rf"\b{word}\b", normalized, re.ASCII):
                index = value
                break
    course = (BACH_BY_INDEX if is_bach else BASICA_BY_INDEX).get(index) if index else None
    return {"course": course, "paralelo": _extract_paralelo(curso_texto)}


def _extract_paralelo(curso_texto: str) -> str | None:
    tokens = re.split(r"\s+", curso_texto.strip())
    last = tokens[-1] if tokens else ""
    if last and re.fullmatch(r"[A-Za-z]\d*", last):
        return last.upper()
    return None
