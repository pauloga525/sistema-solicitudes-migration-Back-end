from bson import ObjectId

from app.core.exceptions import BadRequest


def is_valid_object_id(value: str) -> bool:
    """Mismo criterio que mongoose.isValidObjectId para strings: 24 hex o 12 bytes."""
    if not isinstance(value, str):
        return False
    return ObjectId.is_valid(value)


def parse_object_id(value: str) -> ObjectId:
    """Equivalente a ParseObjectIdPipe: 400 controlado en vez de un 500."""
    if not is_valid_object_id(value):
        raise BadRequest(f'"{value}" no es un id válido.')
    return ObjectId(value)
