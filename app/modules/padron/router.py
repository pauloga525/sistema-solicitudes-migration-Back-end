import re

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from app.core.dependencies import get_db, require_admin
from app.core.exceptions import BadRequest
from app.core.rate_limit import Throttle
from app.modules.padron.repository import StudentRepository
from app.modules.padron.service import PadronService
from app.shared.middleware.memory_multipart import read_single_file

router = APIRouter(prefix="/padron", tags=["padron"])

MAX_FILE_SIZE_BYTES = 5 * 1024 * 1024
ALLOWED_EXTENSIONS = re.compile(r"\.(xlsx|xls)$", re.IGNORECASE)


# Solo existe la importación. No se agregan endpoints de lectura del padrón (decisión deliberada).
@router.post("/import", status_code=201,
             dependencies=[Depends(Throttle("PadronController.import", 5)), Depends(require_admin)])
async def import_padron(request: Request, db=Depends(get_db)):
    file = await read_single_file(request, "file", MAX_FILE_SIZE_BYTES, ALLOWED_EXTENSIONS,
                                  "Solo se aceptan archivos .xlsx o .xls.")
    if file is None:
        raise BadRequest("No se recibió ningún archivo.")
    summary = await PadronService(StudentRepository(db)).import_from_excel(file.content)
    return JSONResponse(status_code=201, content=summary)
