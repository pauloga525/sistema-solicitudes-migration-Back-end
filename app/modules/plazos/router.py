from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response

from app.core.dependencies import get_db, json_body, query_dict, require_admin
from app.core.rate_limit import Throttle
from app.modules.catalog import RequestType
from app.modules.plazos.repository import AnioLectivoRepository, RequestReadRepository
from app.modules.plazos.schemas import CUPO_QUERY_SCHEMA, UPSERT_ANIO_SCHEMA, CupoQueryDTO, UpsertAnioLectivoDTO
from app.modules.plazos.service import PlazosService
from app.shared.serialization import to_json
from app.shared.validators.class_validator import validate_dto
from app.shared.validators.object_id import parse_object_id

router = APIRouter(prefix="/plazos", tags=["plazos"])


def service(db=Depends(get_db)) -> PlazosService:
    return PlazosService(AnioLectivoRepository(db), RequestReadRepository(db))


# Rutas públicas primero para que "aporte-actual" y "cupo" no se confundan con ":id".
@router.get("/aporte-actual", dependencies=[Depends(Throttle("PlazosController.findAporteActual"))])
async def aporte_actual(svc: PlazosService = Depends(service)):
    result = await svc.find_aporte_actual()
    if result is None:
        # El original responde 200 con cuerpo vacío; el formulario público lo interpreta como "sin aporte".
        return Response(status_code=200)
    return to_json(result)


@router.get("/cupo", dependencies=[Depends(Throttle("PlazosController.getCupo"))])
async def cupo(request: Request, svc: PlazosService = Depends(service)):
    dto: CupoQueryDTO = validate_dto(CUPO_QUERY_SCHEMA, query_dict(request), CupoQueryDTO)  # type: ignore[assignment]
    return to_json(await svc.get_cupo_info(dto.studentDni, dto.course, RequestType.PROCESO_MEJORA.value))


@router.get("", dependencies=[Depends(Throttle("PlazosController.findAll")), Depends(require_admin)])
async def find_all(svc: PlazosService = Depends(service)):
    return to_json(await svc.find_all())


@router.post("", status_code=201, dependencies=[Depends(Throttle("PlazosController.create")), Depends(require_admin)])
async def create(body=Depends(json_body), svc: PlazosService = Depends(service)):
    dto: UpsertAnioLectivoDTO = validate_dto(UPSERT_ANIO_SCHEMA, body, UpsertAnioLectivoDTO)  # type: ignore[assignment]
    return JSONResponse(status_code=201, content=to_json(await svc.create(dto)))


@router.put("/{id_}", dependencies=[Depends(Throttle("PlazosController.update")), Depends(require_admin)])
async def update(id_: str, body=Depends(json_body), svc: PlazosService = Depends(service)):
    parse_object_id(id_)
    dto: UpsertAnioLectivoDTO = validate_dto(UPSERT_ANIO_SCHEMA, body, UpsertAnioLectivoDTO)  # type: ignore[assignment]
    return to_json(await svc.update(id_, dto))


@router.delete("/{id_}", dependencies=[Depends(Throttle("PlazosController.remove")), Depends(require_admin)])
async def remove(id_: str, svc: PlazosService = Depends(service)):
    parse_object_id(id_)
    return to_json(await svc.remove(id_))
