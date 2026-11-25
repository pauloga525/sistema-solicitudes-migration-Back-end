"""Lectura del distributivo docente y de la lista de correos institucionales.

- Distributivo ("Profesores - Materias - UETS.xlsx"): solo la hoja BD2 y solo las columnas Etapa, Especialidad,
  Curso, Grupo, Materia y Profesor. La hoja BD1 (con datos de estudiantes) nunca se lee.
- Correos ("CORREOS DOCENTES UETS.xlsx"): exportación de Google Workspace con First Name, Last Name y Email.

El cruce docente ↔ correo es por nombre (sin tildes ni mayúsculas): exacto, luego por inclusión de palabras y al
final aproximado (≥ 0.9). Si un mismo nombre tiene varios correos se elige el que sigue el formato institucional
(primer nombre + iniciales de los apellidos); si no hay uno claro, queda "ambiguo" para elegirlo a mano.
"""
from __future__ import annotations

import difflib
import io
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

import openpyxl

from app.core.exceptions import BadRequest
from app.modules.curriculum import CURRICULUM_SUBJECTS
from app.modules.curriculum_names import EXCLUDED, course_and_specialty, key, pretty

DIST_COLUMNS = ("Etapa", "Especialidad", "Curso", "Grupo", "Materia", "Profesor")
EMAIL_RE = re.compile(r"^[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}$")
_BY_KEY = {key(s): s for s in CURRICULUM_SUBJECTS}


def tokens(s: Any) -> list[str]:
    t = unicodedata.normalize("NFD", str(s or "")).encode("ascii", "ignore").decode().upper()
    return re.sub(r"[^A-Z ]", " ", t).split()


def name_key(s: Any) -> str:
    """Clave estable del docente: palabras del nombre sin tildes, en el orden del distributivo."""
    return " ".join(tokens(s))


def title(s: str) -> str:
    small = {"de", "del", "la", "las", "los", "y"}
    words = " ".join(str(s).split()).lower().split(" ")
    return " ".join(w if (i and w in small) else w[:1].upper() + w[1:] for i, w in enumerate(words))


@dataclass
class TeacherRow:
    name_key: str
    name: str                      # para mostrar (nombres + apellidos cuando hay correo; si no, como en el distributivo)
    email: str | None = None
    match: str = "sin_correo"      # exacto | coincidencia | aproximado | ambiguo | sin_correo
    candidates: list[str] = field(default_factory=list)
    assignments: set[tuple[str, str, str, str]] = field(default_factory=set)   # (curso, especialidad, paralelo, materia)


def _sheet_rows(content: bytes, sheet: str | None, required: tuple[str, ...], what: str):
    try:
        wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001
        raise BadRequest(f"No se pudo leer el archivo de {what}. ¿Es un Excel (.xlsx)?") from exc
    if sheet:
        if sheet not in wb.sheetnames:
            raise BadRequest(f'El archivo de {what} no tiene la hoja "{sheet}".')
        ws = wb[sheet]
    else:
        ws = wb.worksheets[0]
    rows = ws.iter_rows(values_only=True)
    header = [str(h or "").strip() for h in next(rows, ())]
    missing = [c for c in required if c not in header]
    if missing:
        raise BadRequest(f"Al archivo de {what} le faltan columnas: {', '.join(missing)}.")
    idx = [header.index(c) for c in required]
    for r in rows:
        yield [r[i] if i < len(r) else None for i in idx]


def parse_distributivo(content: bytes) -> tuple[dict[str, TeacherRow], dict[str, int]]:
    teachers: dict[str, TeacherRow] = {}
    stats = {"filas": 0, "excluidas": 0, "no_reconocidas": 0}
    for etapa, esp, curso, grupo, materia, prof in _sheet_rows(content, "BD2", DIST_COLUMNS, "distributivo"):
        if not materia or not prof:
            continue
        stats["filas"] += 1
        try:
            course, spec = course_and_specialty(str(etapa or ""), str(esp or ""), str(curso or ""))
        except (KeyError, IndexError):
            stats["no_reconocidas"] += 1
            continue
        subj = pretty(str(materia))
        k = key(subj)
        if k in EXCLUDED or key(str(materia)) in EXCLUDED:
            stats["excluidas"] += 1
            continue
        subj = _BY_KEY.get(k, subj)
        nk = name_key(prof)
        t = teachers.setdefault(nk, TeacherRow(name_key=nk, name=title(" ".join(str(prof).split()))))
        t.assignments.add((course, spec, str(grupo or "").strip(), subj))
    if not teachers:
        raise BadRequest("El distributivo no tiene docentes con materias en la hoja BD2.")
    return teachers, stats


def parse_emails(content: bytes) -> list[tuple[str, str, str]]:
    cols = ("First Name [Required]", "Last Name [Required]", "Email Address [Required]")
    out = []
    for first, last, email in _sheet_rows(content, None, cols, "correos"):
        e = str(email or "").strip().lower()
        if EMAIL_RE.match(e):
            out.append((str(first or "").strip(), str(last or "").strip(), e))
    if not out:
        raise BadRequest("El archivo de correos no tiene direcciones válidas.")
    return out


def _institutional_pick(first: str, last: str, emails: list[str]) -> str | None:
    """nombre + iniciales de los apellidos (freddyac@…): el formato de los correos personales de los docentes."""
    f = tokens(first)[:1]
    ln = tokens(last)
    if not f or not ln:
        return None
    local = (f[0] + "".join(w[0] for w in ln)).lower()
    hits = [e for e in emails if e.split("@")[0] == local]
    return hits[0] if len(hits) == 1 else None


def match_emails(teachers: dict[str, TeacherRow], email_rows: list[tuple[str, str, str]], domain: str) -> None:
    by_set: dict[frozenset, list[tuple[str, str, str]]] = defaultdict(list)
    full: list[tuple[str, str, str, str]] = []  # (apellidos+nombres normalizado, email, first, last)
    for first, last, email in email_rows:
        if not email.endswith("@" + domain):
            continue
        toks = tokens(last) + tokens(first)
        by_set[frozenset(toks)].append((first, last, email))
        full.append((" ".join(toks), email, first, last))

    for t in teachers.values():
        ts = frozenset(t.name_key.split())
        exact = by_set.get(ts, [])
        if len(exact) == 1:
            t.email, t.match = exact[0][2], "exacto"
            t.name = title(f"{exact[0][0]} {exact[0][1]}")
            continue
        if len(exact) > 1:
            pick = _institutional_pick(exact[0][0], exact[0][1], [e[2] for e in exact])
            t.candidates = sorted({e[2] for e in exact})
            t.name = title(f"{exact[0][0]} {exact[0][1]}")
            if pick:
                t.email, t.match = pick, "exacto"
            else:
                t.match = "ambiguo"
            continue
        sub = [r for k, rows in by_set.items() if k <= ts or ts <= k for r in rows]
        if len({r[2] for r in sub}) == 1:
            t.email, t.match = sub[0][2], "coincidencia"
            t.name = title(f"{sub[0][0]} {sub[0][1]}")
            continue
        best, ratio = None, 0.0
        for norm, email, first, last in full:
            r = difflib.SequenceMatcher(None, t.name_key, norm).ratio()
            if r > ratio:
                best, ratio = (email, first, last), r
        if best and ratio >= 0.9:
            t.email, t.match = best[0], "aproximado"
            t.name = title(f"{best[1]} {best[2]}")


def build_preview(dist: bytes, emails: bytes, domain: str) -> dict[str, Any]:
    teachers, stats = parse_distributivo(dist)
    match_emails(teachers, parse_emails(emails), domain)
    rows = sorted(teachers.values(), key=lambda t: t.name_key)
    count = defaultdict(int)
    for t in rows:
        count[t.match] += 1
    return {
        "teachers": [{"nameKey": t.name_key, "name": t.name, "email": t.email, "match": t.match, "candidates": t.candidates,
                      "assignments": sorted([list(a) for a in t.assignments])} for t in rows],
        "summary": {"docentes": len(rows), "asignaciones": len({a for t in rows for a in t.assignments}),
                    "conCorreo": sum(1 for t in rows if t.email), **{k: count[k] for k in
                    ("exacto", "coincidencia", "aproximado", "ambiguo", "sin_correo")}, **stats},
    }
