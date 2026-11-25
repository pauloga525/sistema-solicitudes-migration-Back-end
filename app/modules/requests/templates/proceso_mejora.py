"""Contenido del PDF de "Evaluación de Mejora" (proceso_mejora).

Port fiel de templates/proceso-mejora.pdf-template.ts: mismas coordenadas,
fuentes, tamaños y textos. Solo dibuja el contenido; el membrete y la fusión
son infraestructura común (app/shared/pdf/merger.py).
"""
from __future__ import annotations

from typing import Any

from app.modules.catalog import ALLOWED_SUBJECTS
from app.modules.curriculum import TEST_SUBJECT, subjects_for
from app.shared.pdf.pdfkit_compat import PdfkitDocument, Run

PW = 595.28
ML = 60   # izquierda
MR = 49   # derecha
MT = 128  # superior: libera el espacio del membrete
CW = PW - ML - MR
CB = 9    # lado del checkbox


def _check(doc: PdfkitDocument, x: float, y: float) -> None:
    doc.line_width(1).polyline_stroke([(x + 2, y + 5.5), (x + 4, y + 8), (x + 8, y + 3)])


def build_proceso_mejora_pdf_content(request: dict[str, Any]) -> bytes:
    doc = PdfkitDocument()
    payload = request.get("payload") or {}
    selected = set(payload.get("subjects") or [])
    course = str(request.get("course", "")).replace("_", " ")

    # N° de solicitud (derecha, 9 pt, gris)
    doc.font("Helvetica").size(9).fill_color("#444444")
    y = doc.text(f"N° {request.get('requestNumber', '')}", ML, MT, width=CW, align="right") + 14

    # Párrafo introductorio
    doc.font("Helvetica").size(11).fill_color("#000000")
    y = doc.text("Estimado representante:", ML, y, width=CW) + 6
    y = doc.text(
        "Antes de continuar, recuerde que el proceso de evaluación de MEJORA es totalmente "
        "VOLUNTARIO y está a disposición de todos los estudiantes. "
        "Para evitar el desperdicio de papel o la sobrecarga de actividades al estudiante, "
        "si su representado no requiere la evaluación de MEJORA, por favor, no llene el "
        "formulario. En caso de requerir la Mejora, continúe con el registro.",
        ML, y, width=CW, align="justify", line_gap=4,
    ) + 14

    # Texto de solicitud con nombres en negrita
    R, B = "Helvetica", "Helvetica-Bold"
    y = doc.text([
        Run("Yo, ", R), Run(str(request.get("representativeName", "")), B),
        Run(" representante del estudiante ", R), Run(str(request.get("studentName", "")), B),
        Run(", del ", R), Run(course, B), Run(" paralelo ", R), Run(str(request.get("paralelo", "")), B),
        Run(" solicito encarecidamente que mi representado pueda rendir la evaluación de "
            f"mejora del {trimestre_label(request.get('_trimestre'))} en las asignaturas de:", R),
    ], ML, y, width=CW, line_gap=4) + 12

    own = subjects_for(str(request.get("course", "")), str(request.get("paralelo", "")))
    if TEST_SUBJECT in selected:
        own = own + (TEST_SUBJECT,)
    ordered = [s for s in own if s in selected] + sorted(s for s in selected if s not in own)

    # Nota sobre materias (itálica, 10 pt): solo si alguna de las elegidas se evalúa con examen.
    note = evaluation_note(ordered)
    if note:
        doc.font("Helvetica-Oblique").size(10).fill_color("#000000")
        y = doc.text(note, ML, y, width=CW, line_gap=2) + 12

    if own and selected and selected <= set(own):
        # Malla por curso: se listan las materias elegidas (pueden ser nombres largos de módulos).
        for subj in [s for s in own if s in selected]:
            doc.stroke_color("#000000").line_width(0.8).rect_stroke(ML, y + 1, CB, CB)
            _check(doc, ML, y)
            doc.font("Helvetica").size(10.5).fill_color("#000000")
            y = doc.text(subj, ML + CB + 5, y, width=CW - CB - 5, line_gap=2) + 5
        y += 9
    else:
        # Solicitudes con el catálogo histórico: grilla original de 2 columnas
        col_gap, row_h = 16, 18
        col_w = (CW - col_gap) / 2
        for i, subj in enumerate(ALLOWED_SUBJECTS):
            col, row = i % 2, i // 2
            sx = ML if col == 0 else ML + col_w + col_gap
            sy = y + row * row_h
            doc.stroke_color("#000000").line_width(0.8).rect_stroke(sx, sy + 1, CB, CB)
            if subj in selected:
                _check(doc, sx, sy)
            doc.font("Helvetica").size(10.5).fill_color("#000000").text_line(subj, sx + CB + 5, sy)
        y += -(-len(ALLOWED_SUBJECTS) // 2) * row_h + 14

    # Acepto (siempre marcado)
    doc.stroke_color("#000000").line_width(0.8).rect_stroke(ML, y + 1, CB, CB)
    _check(doc, ML, y)
    doc.font("Helvetica").size(11).fill_color("#000000")
    y = doc.text(
        "Acepto que la información proporcionada es verdadera y que mi representado "
        "se preparará adecuadamente para rendir las evaluaciones seleccionadas.",
        ML + CB + 6, y, width=CW - CB - 6, line_gap=3,
    ) + 36

    # Firma
    y = doc.text("Autorizado y solicitado por:", ML, y, width=CW, align="center") + 6
    doc.font("Helvetica-Bold").size(11)
    y = doc.text(str(request.get("representativeName", "")).upper(), ML, y, width=CW, align="center") + 4
    doc.font("Helvetica").size(11)
    doc.text(f"CI: {request.get('representativeDni', '')}", ML, y, width=CW, align="center")
    return doc.finish()


# Materias cuya MEJORA es un examen (las del texto original, con sus nombres en la malla actual).
EXAM_SUBJECTS = frozenset({"Matemática", "Lengua y Literatura", "Lengua y literatura", "Ciencias Naturales", "Science",
                           "Estudios Sociales", "Inglés"})


def _join(items: list[str]) -> str:
    if len(items) == 1:
        return items[0]
    last = items[-1]
    conj = " e " if last.lower().startswith(("i", "í", "hi")) and not last.lower().startswith(("hie", "ie")) else " y "
    return ", ".join(items[:-1]) + conj + last


def evaluation_note(subjects: list[str]) -> str:
    """Nota del PDF: qué materias elegidas se evalúan con examen. Sin ninguna, no hay nota.
    (Por pedido de la institución ya no se menciona "actividades prácticas".)"""
    exams = [s for s in subjects if s in EXAM_SUBJECTS]
    if not exams:
        return ""
    if len(exams) == 1:
        return f"(La MEJORA de {exams[0]} será mediante examen)"
    return f"(La MEJORA de las asignaturas de {_join(exams)} será mediante exámenes)"


_SMALL = {"de", "del", "la", "el", "y", "e"}


def trimestre_label(name: Any) -> str:
    """Nombre del trimestre activo como aparece en el calendario ("Primer trimestre" → "Primer Trimestre").

    Sin calendario (solicitudes anteriores a la migración) se mantiene el texto del sistema original.
    """
    text = " ".join(str(name or "").split())
    if not text:
        return "Tercer Trimestre"
    words = [w if (i and w.lower() in _SMALL) or w.isupper() else w[:1].upper() + w[1:] for i, w in enumerate(text.split())]
    label = " ".join(words)
    return label if "trimestre" in label.lower() else f"{label} Trimestre"
