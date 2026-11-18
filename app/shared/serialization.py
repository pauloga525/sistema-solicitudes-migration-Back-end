"""Serializa documentos BSON igual que Mongoose `toJSON()` + `JSON.stringify`."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from bson import ObjectId

from app.shared.utilities.dates import to_js_iso


def to_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: to_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_json(v) for v in value]
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, datetime):
        return to_js_iso(value)
    return value
