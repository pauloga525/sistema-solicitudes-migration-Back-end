"""Normalización de los nombres del distributivo (curso, especialidad, materia).

La usan la malla por curso (tools/gen_curriculum.py) y la importación de docentes, para que una misma
materia se escriba igual en los dos lados.
"""
from __future__ import annotations

import re
import unicodedata

COURSE_MAP = {"1": "1ro_Basica", "2": "2do_Basica", "3": "3ro_Basica", "4": "4to_Basica", "5": "5to",
              "6": "6to", "7": "7mo", "8": "8vo", "9": "9no", "10": "10mo"}
BACH = {"1": "1ro_Bach", "2": "2do_Bach", "3": "3ro_Bach"}
SPECIALTY_MAP = {
    "Desarrollo de Software": "Desarrollo de Software",
    "Informática": "Desarrollo de Software",
    "Mecatrónica": "Mecatrónica",
    "Mecánica Industrial": "Mecánica Industrial",
    "Mecanizado y Construcciones Metálicas": "Mecánica Industrial",
    "Electromecanica Automotriz": "Electromecánica Automotriz",
    "Ciencias": "Ciencias",
    "Instalaciones Eléctricas y Automatización": "Instalaciones Eléctricas y Automatización",
    "Instalaciones, Equipos y Máquinas Eléctricas": "Instalaciones Eléctricas y Automatización",
}
# Registros que no son asignaturas evaluables con examen (calificación cualitativa/comportamiento).
EXCLUDED = {"comportamental", "convivencia", "participacionestudiantil"}

SMALL = {"y", "e", "o", "u", "de", "del", "la", "las", "el", "los", "en", "a", "al", "para", "por", "con"}
KEEP_UPPER = {"ECA", "CAD", "PLC", "CNC", "CAV", "MCAV", "PMC", "EYA", "SEE", "AES", "D.D.W.", "S.T.", "I", "II", "III", "CAV-CNC"}
ACCENTS = {"tecnico": "técnico", "calculo": "cálculo", "mecanico": "mecánico", "robotica": "robótica",
           "moviles": "móviles", "neumaticos": "neumáticos", "automatizacion": "automatización",
           "electrica": "eléctrica", "energia": "energía", "ofimaticas": "ofimáticas", "combustion": "combustión",
           "maquinas": "máquinas", "practico": "práctico", "vehiculos": "vehículos", "participacion": "participación",
           "transforamación": "transformación", "mantentimiento": "mantenimiento", "autotronica": "autotrónica"}


def key(s):
    s = unicodedata.normalize("NFD", s.lower())
    return re.sub(r"[^a-z0-9]", "", "".join(c for c in s if unicodedata.category(c) != "Mn"))


def word(w, first):
    if w.upper() in KEEP_UPPER:
        return w.upper()
    lw = w.lower()
    pre = ""
    while lw and lw[0] in "(/-":
        pre, lw = pre + lw[0], lw[1:]
    m = re.match(r"^([^\W\d_]+)(.*)$", lw)
    if not m:
        return pre + lw
    core, tail = m.groups()
    core = ACCENTS.get(core, core)
    if tail.strip(").,").upper() in KEEP_UPPER and tail:
        pass
    if not first and core in SMALL and not pre:
        return core + tail
    return pre + core[:1].upper() + core[1:] + tail


def pretty(raw):
    s = re.sub(r"\s+", " ", raw.strip())
    s = re.sub(r"^MOD\.?\s*", "Mod. ", s, flags=re.I)
    s = re.sub(r"(\w)\(", r"\1 (", s)
    if s.count("(") > s.count(")"):
        s += ")"
    s = s.replace(" / ", " ")
    parts = s.split(" ")
    out = [word(w, i == 0 or (i == 1 and parts[0] == "Mod.")) for i, w in enumerate(parts)]
    # Acrónimos dentro de paréntesis o con guion
    res = " ".join(out)
    res = re.sub(r"\((\w+)\)", lambda m: f"({m.group(1).upper()})" if m.group(1).upper() in KEEP_UPPER else m.group(0), res)
    res = re.sub(r"- (Eya|See)\)", lambda m: f"- {m.group(1).upper()})", res)
    res = re.sub(r"\b(Eya|See)\b", lambda m: m.group(1).upper(), res)
    res = res.replace("Cav-cnc", "CAV-CNC").replace("Pmc", "PMC").replace("Cnc", "CNC")
    res = re.sub(r"([-/])([a-záéíóú])", lambda m: m.group(1) + m.group(2).upper(), res)
    res = re.sub(r"\b2d\b", "2D", res)
    res = re.sub(r"\(([A-Za-z](?:\.[A-Za-z])+\.)\)", lambda m: "(" + m.group(1).upper() + ")", res)
    return res


def course_and_specialty(etapa: str, especialidad: str, curso: str) -> tuple[str, str]:
    """("BACHILLERATO", "Informática", "3 I") → ("3ro_Bach", "Desarrollo de Software"). KeyError si no se reconoce."""
    if etapa.strip().upper() == "BACHILLERATO":
        return BACH[curso.split()[0]], SPECIALTY_MAP[especialidad.strip()]
    return COURSE_MAP[curso.strip()], ""
