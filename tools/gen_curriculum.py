"""Genera app/modules/curriculum_data.py desde la hoja BD2 del Excel de materias.
Uso: python3 -I gen_curriculum.py <excel> <salida.py>
Solo lee Etapa/Especialidad/Curso/Grupo/Materia (no profesores ni estudiantes)."""
import sys, re, unicodedata, collections, openpyxl

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
from app.modules.curriculum_names import (  # noqa: E402
    BACH, COURSE_MAP, EXCLUDED, SPECIALTY_MAP, key, pretty,
)


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
