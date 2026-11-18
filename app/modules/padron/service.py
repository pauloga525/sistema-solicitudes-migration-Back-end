"""Importación del padrón de estudiantes desde Excel (equivalente a PadronService)."""
from __future__ import annotations

import re
from typing import Any

from app.core.exceptions import BadRequest
from app.modules.padron.course_parser import parse_curso_texto
from app.modules.padron.excel_reader import read_first_sheet
from app.modules.padron.repository import StudentRepository
from app.shared.crypto.field_encryption import encrypt_field, hash_field

CEDULA_FORMAT = re.compile(r"^\d{6,15}$")


def _js_string(v: Any) -> str:
    return str(v) if v is not None else ""


class PadronService:
    def __init__(self, repo: StudentRepository):
        self.repo = repo

    async def import_from_excel(self, content: bytes) -> dict[str, Any]:
        try:
            rows = read_first_sheet(content)
        except Exception as exc:  # archivo dañado: 400 controlado en vez de 500
            raise BadRequest("No se pudo leer el archivo Excel.") from exc

        summary: dict[str, Any] = {"total": len(rows), "creados": 0, "actualizados": 0, "errores": []}
        for i, row in enumerate(rows):
            fila = i + 2  # la fila 1 es el encabezado
            full_name = _js_string(row.get("IDCApellidos", "")).strip()
            curso_texto = _js_string(row.get("IDCCurso", "")).strip()
            especialidad = _js_string(row.get("IDCEspecialidad", "")).strip()
            cedula = _js_string(row.get("IDCCedula", "")).strip()

            if not full_name:
                summary["errores"].append({"fila": fila, "motivo": "IDCApellidos vacío."})
                continue
            if not curso_texto:
                summary["errores"].append({"fila": fila, "motivo": "IDCCurso vacío."})
                continue
            if not CEDULA_FORMAT.match(cedula):
                summary["errores"].append({"fila": fila, "motivo": f'IDCCedula con formato inválido: "{cedula}".'})
                continue

            parsed = parse_curso_texto(curso_texto)
            cedula_hash = hash_field(cedula)
            data = {
                "fullName": full_name,
                "especialidad": especialidad or None,
                "cursoTexto": curso_texto,
                "course": parsed["course"],
                "paralelo": parsed["paralelo"],
                "cedulaCifrada": encrypt_field(cedula),
                "cedulaHash": cedula_hash,
            }
            existing = await self.repo.find_by_hash(cedula_hash)
            if existing:
                await self.repo.update(existing["_id"], {k: v for k, v in data.items() if k != "cedulaHash"})
                summary["actualizados"] += 1
            else:
                await self.repo.insert(data)
                summary["creados"] += 1
        return summary
