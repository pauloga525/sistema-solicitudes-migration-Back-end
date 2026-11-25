"""Lectura de un único archivo multipart COMPLETAMENTE en memoria.

Starlette guarda los archivos grandes en disco (SpooledTemporaryFile). El
padrón contiene cédulas de menores, así que aquí se parsea el stream a mano
y nunca se escribe nada en disco. Reproduce los errores de multer
(FileInterceptor('file', {limits, fileFilter})) del original.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from fastapi import Request
from python_multipart.multipart import MultipartParser, parse_options_header

from app.core.exceptions import BadRequest, PayloadTooLarge

MAX_TEXT_FIELDS_BYTES = 1024 * 1024


@dataclass
class UploadedFile:
    filename: str
    content: bytes


async def read_single_file(request: Request, field: str, max_size: int, allowed_ext: re.Pattern,
                           ext_error: str) -> UploadedFile | None:
    files = await read_files(request, (field,), max_size, allowed_ext, ext_error)
    return None if files is None else files.get(field)


async def read_files(request: Request, fields: tuple[str, ...], max_size: int, allowed_ext: re.Pattern,
                     ext_error: str) -> dict[str, UploadedFile] | None:
    """Varios archivos (uno por campo), todos en memoria. None si la petición no es multipart."""
    ctype, params = parse_options_header(request.headers.get("content-type", ""))
    if ctype != b"multipart/form-data" or b"boundary" not in params:
        return None  # multer ignora peticiones no multipart → "No se recibió ningún archivo."

    state: dict = {"headers": {}, "hfield": b"", "hvalue": b"", "current": None, "text_bytes": 0}
    result: dict[str, UploadedFile] = {}
    errors: list[Exception] = []

    def on_part_begin():
        state["headers"] = {}

    def on_header_field(data, start, end):
        state["hfield"] += data[start:end]

    def on_header_value(data, start, end):
        state["hvalue"] += data[start:end]

    def on_header_end():
        state["headers"][state["hfield"].decode("latin-1").lower()] = state["hvalue"]
        state["hfield"] = b""
        state["hvalue"] = b""

    def on_headers_finished():
        if errors:
            return
        _, disp = parse_options_header(state["headers"].get("content-disposition", b""))
        name = disp.get(b"name", b"").decode("utf-8", "replace")
        filename = disp.get(b"filename")
        if filename is None:
            state["current"] = {"kind": "text", "size": 0}
            return
        fname = filename.decode("utf-8", "replace")
        if name not in fields or name in result:
            errors.append(BadRequest(f"Unexpected field - {name}"))
            return
        if not allowed_ext.search(fname):
            errors.append(BadRequest(ext_error))
            return
        state["current"] = {"kind": "file", "field": name, "filename": fname, "buf": bytearray()}

    def on_part_data(data, start, end):
        cur = state["current"]
        if errors or cur is None:
            return
        chunk = data[start:end]
        if cur["kind"] == "file":
            cur["buf"].extend(chunk)
            if len(cur["buf"]) > max_size:
                errors.append(PayloadTooLarge("File too large"))
        else:
            state["text_bytes"] += len(chunk)
            if state["text_bytes"] > MAX_TEXT_FIELDS_BYTES:
                errors.append(PayloadTooLarge("Field value too long"))

    def on_part_end():
        cur = state["current"]
        if not errors and cur is not None and cur["kind"] == "file":
            result[cur["field"]] = UploadedFile(cur["filename"], bytes(cur["buf"]))
        state["current"] = None

    parser = MultipartParser(params[b"boundary"], callbacks={
        "on_part_begin": on_part_begin, "on_part_data": on_part_data, "on_part_end": on_part_end,
        "on_header_field": on_header_field, "on_header_value": on_header_value,
        "on_header_end": on_header_end, "on_headers_finished": on_headers_finished,
    })
    async for chunk in request.stream():
        parser.write(chunk)
        if errors:
            raise errors[0]
    parser.finalize()
    if errors:
        raise errors[0]
    return result
