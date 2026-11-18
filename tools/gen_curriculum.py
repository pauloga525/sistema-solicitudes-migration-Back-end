"""Genera app/modules/curriculum_data.py desde la hoja BD2 del Excel de materias.
Uso: python3 -I gen_curriculum.py <excel> <salida.py>
Solo lee Etapa/Especialidad/Curso/Grupo/Materia (no profesores ni estudiantes)."""
import sys, re, unicodedata, collections, openpyxl

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


def main(src, dst):
    ws = openpyxl.load_workbook(src, read_only=True, data_only=True)["BD2"]
    rows = ws.iter_rows(values_only=True)
    hdr = [str(h or "").strip() for h in next(rows)]
    idx = {h: hdr.index(h) for h in ("Etapa", "Especialidad", "Curso", "Grupo", "Materia")}
    groups = collections.OrderedDict()
    canon = {}  # clave → nombre a mostrar (primera forma vista)
    for r in rows:
        mat = r[idx["Materia"]]
        if not mat:
            continue
        etapa, esp, curso, grupo = (str(r[idx[k]] or "").strip() for k in ("Etapa", "Especialidad", "Curso", "Grupo"))
        if etapa.upper() == "BACHILLERATO":
            course, spec = BACH[curso.split()[0]], SPECIALTY_MAP[esp]
        else:
            course, spec = COURSE_MAP[curso], ""
        name = pretty(str(mat))
        k = key(name)
        if k in EXCLUDED or key(str(mat)) in EXCLUDED:
            continue
        canon.setdefault(k, name)
        g = groups.setdefault((course, spec), collections.OrderedDict()).setdefault(grupo, [])
        if k not in g:
            g.append(k)
    data = {}
    for (course, spec), par in groups.items():
        lists = list(par.values())
        common = [k for k in lists[0] if all(k in l for l in lists[1:])]
        data[f"{course}|{spec}"] = sorted((canon[k] for k in common), key=lambda n: (n.startswith("Mod."), key(n)))
        extras = sorted({canon[k] for l in lists for k in l} - {canon[k] for k in common})
        if extras:
            print("solo en algunos paralelos", course, spec, extras, file=sys.stderr)
    with open(dst, "w", encoding="utf-8") as f:
        f.write('"""Malla de materias por curso y especialidad (generado; no editar a mano).\n\n'
                'Fuente: hoja BD2 de "Profesores - Materias - UETS.xlsx" (solo curso/especialidad/materia).\n'
                'Regenerar con tools/gen_curriculum.py. Clave: "<curso>|<especialidad>" (vacía fuera de bachillerato).\n"""\n\n')
        f.write("CURRICULUM: dict[str, tuple[str, ...]] = {\n")
        for k, v in data.items():
            f.write(f"    {k!r}: (\n" + "".join(f"        {s!r},\n" for s in v) + "    ),\n")
        f.write("}\n")
    print(len(data), "cursos;", len({s for v in data.values() for s in v}), "materias")


if __name__ == "__main__":
    main(*sys.argv[1:3])
