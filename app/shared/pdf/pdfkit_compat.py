"""Pequeña capa sobre ReportLab que reproduce la tipografía de pdfkit.

El PDF original se dibujaba con pdfkit (origen arriba‑izquierda, alto de línea
= 1.156 × tamaño para Helvetica + lineGap, ajuste de línea "greedy", justify
por espaciado entre palabras). Para que el documento migrado sea visualmente
equivalente se reproduce ese modelo sobre un canvas de ReportLab, en vez de
usar los Paragraph de ReportLab (que miden y reparten el texto distinto).
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from reportlab.pdfbase._fontdata import encodings
from reportlab.pdfbase.pdfmetrics import stringWidth as _rl_string_width
from reportlab.pdfgen.canvas import Canvas

A4_PDFKIT = (595.28, 841.89)


@dataclass
class Run:
    text: str
    font: str


# Métricas AFM de las fuentes estándar (ascender, descender, bbox lly, bbox ury), como las usa pdfkit.
_AFM = {
    "Helvetica": (718, -207, -225, 931),
    "Helvetica-Bold": (718, -207, -228, 962),
    "Helvetica-Oblique": (718, -207, -225, 931),
    "Helvetica-BoldOblique": (718, -207, -228, 962),
}


_AFM_DIR = Path(__file__).parent / "afm"
_WINANSI = encodings["WinAnsiEncoding"]


@lru_cache
def _kern_pairs(font: str) -> dict[tuple[str, str], int]:
    """Pares de kerning (KPX) del AFM de Adobe: pdfkit los aplica a las fuentes estándar."""
    pairs: dict[tuple[str, str], int] = {}
    path = _AFM_DIR / f"{font}.afm"
    if path.exists():
        for line in path.read_text(encoding="latin-1").splitlines():
            if line.startswith("KPX "):
                _, a, b, v = line.split()
                pairs[(a, b)] = int(v)
    return pairs


def _glyph(ch: str) -> str | None:
    try:
        return _WINANSI[ch.encode("cp1252")[0]]
    except (UnicodeEncodeError, IndexError):
        return None


def _kerns(text: str, font: str) -> list[int]:
    """kern[i] = ajuste (1/1000 em) entre text[i] y text[i+1]."""
    pairs = _kern_pairs(font)
    return [pairs.get((_glyph(text[i]), _glyph(text[i + 1])), 0) for i in range(len(text) - 1)]


def stringWidth(text: str, font: str, size: float) -> float:  # noqa: N802 (misma firma que ReportLab)
    return _rl_string_width(text, font, size) + sum(_kerns(text, font)) / 1000 * size


def _pdf_string(text: str) -> str:
    """Literal de cadena PDF en WinAnsi (codificación de las fuentes estándar de ReportLab)."""
    out = []
    for b in text.encode("cp1252", errors="replace"):
        ch = chr(b)
        if ch in "()\\":
            out.append("\\" + ch)
        elif 32 <= b < 127:
            out.append(ch)
        else:
            out.append(f"\\{b:03o}")
    return "(" + "".join(out) + ")"


def _ascender(font: str, size: float) -> float:
    return _AFM[font][0] / 1000 * size


def _line_height(font: str, size: float) -> float:
    """pdfkit: (ascender + lineGap - descender) / 1000 * size, lineGap = altoBBox - (asc - desc)."""
    asc, desc, lly, ury = _AFM[font]
    gap = (ury - lly) - (asc - desc)
    return (asc + gap - desc) / 1000 * size


class PdfkitDocument:
    def __init__(self, size: tuple[float, float] = A4_PDFKIT):
        self.buffer = io.BytesIO()
        self.width, self.height = size
        self.c = Canvas(self.buffer, pagesize=size)
        self.font_name = "Helvetica"
        self.font_size = 12.0
        self.y = 0.0

    # ── estado ──────────────────────────────────────────────────────────────
    def font(self, name: str) -> "PdfkitDocument":
        self.font_name = name
        return self

    def size(self, size: float) -> "PdfkitDocument":
        self.font_size = size
        return self

    def fill_color(self, hex_color: str) -> "PdfkitDocument":
        self.c.setFillColor(hex_color)
        return self

    def stroke_color(self, hex_color: str) -> "PdfkitDocument":
        self.c.setStrokeColor(hex_color)
        return self

    def line_width(self, w: float) -> "PdfkitDocument":
        self.c.setLineWidth(w)
        return self

    # ── dibujo ──────────────────────────────────────────────────────────────
    def _ry(self, y: float) -> float:
        return self.height - y

    def rect_stroke(self, x: float, y: float, w: float, h: float) -> None:
        self.c.rect(x, self._ry(y + h), w, h, stroke=1, fill=0)

    def polyline_stroke(self, points: list[tuple[float, float]]) -> None:
        p = self.c.beginPath()
        p.moveTo(points[0][0], self._ry(points[0][1]))
        for x, y in points[1:]:
            p.lineTo(x, self._ry(y))
        self.c.drawPath(p, stroke=1, fill=0)

    def _draw_kerned(self, text: str, x: float, baseline: float, font: str, size: float,
                     word_spacing: float = 0.0) -> float:
        """Dibuja con el operador TJ (como pdfkit): kerning AFM y espaciado entre palabras.

        Al ser un único arreglo TJ, el texto se puede seleccionar/copiar/buscar normalmente.
        """
        kerns = _kerns(text, font)
        parts: list[str] = []
        start = 0
        width = 0.0
        for i, ch in enumerate(text):
            adjust = (kerns[i] / 1000 * size if i < len(kerns) else 0.0) + (word_spacing if ch == " " else 0.0)
            if adjust and i < len(text) - 1:
                seg = text[start:i + 1]
                parts.append(_pdf_string(seg))
                parts.append(f"{-adjust * 1000 / size:.3f}")
                width += _rl_string_width(seg, font, size) + adjust
                start = i + 1
        seg = text[start:]
        if seg:
            parts.append(_pdf_string(seg))
            width += _rl_string_width(seg, font, size)
        t = self.c.beginText(x, baseline)
        t.setFont(font, size)
        t._code.append(f"[{' '.join(parts)}] TJ")  # noqa: SLF001 (operador TJ no expuesto por ReportLab)
        self.c.drawText(t)
        return x + width

    def text_line(self, text: str, x: float, y: float) -> None:
        """Equivalente a pdfkit text(..., {lineBreak: false})."""
        self._draw_kerned(text, x, self._ry(y + _ascender(self.font_name, self.font_size)),
                          self.font_name, self.font_size)

    def text(self, runs: str | list[Run], x: float, y: float, *, width: float, align: str = "left",
             line_gap: float = 0.0) -> float:
        """Texto con ajuste de línea (admite tramos con distinta fuente, como `continued: true`).

        Devuelve y deja en self.y la posición siguiente, como `doc.y` de pdfkit.
        """
        if isinstance(runs, str):
            runs = [Run(runs, self.font_name)]
        size = self.font_size
        tokens: list[tuple[str, str]] = []
        for run in runs:
            tokens.extend((tok, run.font) for tok in re.findall(r"\S+\s*|\s+", run.text))

        lines: list[list[tuple[str, str]]] = [[]]
        used = 0.0
        for tok, font in tokens:
            w_full = stringWidth(tok, font, size)
            if lines[-1] and used + w_full > width + 1e-6:  # pdfkit mide la palabra con su espacio final
                lines.append([])
                used = 0.0
                if not tok.strip():
                    continue
            lines[-1].append((tok, font))
            used += w_full

        cur_y = y
        for i, line in enumerate(lines):
            # pdfkit avanza con el alto de línea de la fuente activa al cortar la línea:
            # la del primer tramo de la línea siguiente, o la del último tramo al terminar.
            if i + 1 < len(lines) and lines[i + 1]:
                line_h = _line_height(lines[i + 1][0][1], size)
            elif line:
                line_h = _line_height(line[-1][1], size)
            else:
                line_h = _line_height(self.font_name, size)
            if line:
                # quita el espacio final de la línea para medir/alinear
                last_tok, last_font = line[-1]
                line = line[:-1] + [(last_tok.rstrip(), last_font)]
                natural = sum(stringWidth(t, f, size) for t, f in line)
                is_last = i == len(lines) - 1
                spaces = sum(t.count(" ") for t, _ in line)
                word_spacing = 0.0
                start_x = x
                if align == "right":
                    start_x = x + width - natural
                elif align == "center":
                    start_x = x + (width - natural) / 2
                elif align == "justify" and not is_last and spaces:
                    word_spacing = (width - natural) / spaces
                baseline = self._ry(cur_y + _ascender(line[0][1], size))
                cx = start_x
                # agrupa tokens consecutivos de la misma fuente (pdfkit codifica cada tramo entero)
                segments: list[list[str]] = []
                for tok, font in line:
                    if segments and segments[-1][1] == font:
                        segments[-1][0] += tok
                    else:
                        segments.append([tok, font])
                for seg_text, font in segments:
                    cx = self._draw_kerned(seg_text, cx, baseline, font, size, word_spacing)
            cur_y += line_h + line_gap
        self.y = cur_y
        return cur_y

    def finish(self) -> bytes:
        self.c.showPage()
        self.c.save()
        return self.buffer.getvalue()
