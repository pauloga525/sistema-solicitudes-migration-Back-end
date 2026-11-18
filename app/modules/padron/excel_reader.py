"""Lee la primera hoja de un Excel en memoria como lista de dicts (≈ XLSX.utils.sheet_to_json).

- La primera fila son los encabezados; las celdas vacías valen "" (defval).
- Las filas totalmente vacías se omiten (como SheetJS).
- .xlsx con openpyxl, .xls con xlrd; si no es ninguno se interpreta como
  texto delimitado (SheetJS hace lo mismo con archivos que no reconoce).
"""
from __future__ import annotations

import csv
import io
from datetime import date, datetime
from typing import Any


def _cell(v: Any) -> Any:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return v


def _rows_to_dicts(rows: list[list[Any]]) -> list[dict[str, Any]]:
    rows = [[_cell(c) for c in r] for r in rows]
    while rows and all(c == "" for c in rows[0]):
        rows.pop(0)
    if not rows:
        return []
    headers = [str(h) for h in rows[0]]
    out = []
    for r in rows[1:]:
        if all(c == "" for c in r):
            continue
        out.append({h: (r[i] if i < len(r) else "") for i, h in enumerate(headers) if h != ""})
    return out


def read_first_sheet(content: bytes) -> list[dict[str, Any]]:
    if content[:2] == b"PK":
        import openpyxl

        wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        ws = wb.worksheets[0]
        rows = [list(r) for r in ws.iter_rows(values_only=True)]
        wb.close()
        return _rows_to_dicts(rows)
    if content[:8] == bytes.fromhex("D0CF11E0A1B11AE1"):
        import xlrd

        book = xlrd.open_workbook(file_contents=content)
        sh = book.sheet_by_index(0)
        return _rows_to_dicts([sh.row_values(i) for i in range(sh.nrows)])
    text = content.decode("utf-8", errors="replace")
    return _rows_to_dicts([list(r) for r in csv.reader(io.StringIO(text))])
