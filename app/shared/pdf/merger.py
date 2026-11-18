"""Infraestructura común de PDF: elegir el membrete del campus y fusionarlo con el contenido.

Equivalente a PdfService (pdf-lib): la página 1 del membrete queda de fondo y
la página 1 del contenido se dibuja encima, escalada al tamaño del membrete.
"""
from __future__ import annotations

import io
from pathlib import Path

from pypdf import PdfReader, PdfWriter, Transformation

LETTERHEAD_BY_CAMPUS = {
    "Yanuncay": "letterhead2.pdf",  # Membrete Yanuncay
}
DEFAULT_LETTERHEAD = "letterhead.pdf"  # Carlos Crespi / María Auxiliadora


def letterhead_path(assets_dir: Path, campus: str) -> Path:
    return assets_dir / LETTERHEAD_BY_CAMPUS.get(campus, DEFAULT_LETTERHEAD)


def merge_with_letterhead(content_pdf: bytes, letterhead_file: Path) -> bytes:
    letterhead = PdfReader(str(letterhead_file)).pages[0]
    content = PdfReader(io.BytesIO(content_pdf)).pages[0]

    writer = PdfWriter()
    page = writer.add_page(letterhead)
    lw, lh = float(page.mediabox.width), float(page.mediabox.height)
    cw, ch = float(content.mediabox.width), float(content.mediabox.height)
    page.merge_transformed_page(content, Transformation().scale(lw / cw, lh / ch))

    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()
