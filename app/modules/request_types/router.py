from fastapi import APIRouter, Depends

from app.core.rate_limit import Throttle
from app.modules.catalog import REQUEST_TYPES
from app.modules.requests.request_type_config import REQUEST_TYPE_METADATA

router = APIRouter(prefix="/request-types", tags=["request-types"])


@router.get("", dependencies=[Depends(Throttle("RequestTypesController.list"))])
async def list_request_types():
    """Lista pública de tipos de trámite (lee la metadata, sin acceso a base de datos)."""
    return [{"type": t, "label": REQUEST_TYPE_METADATA[t].label,
             "usesAcademicCatalog": REQUEST_TYPE_METADATA[t].uses_academic_catalog} for t in REQUEST_TYPES]
