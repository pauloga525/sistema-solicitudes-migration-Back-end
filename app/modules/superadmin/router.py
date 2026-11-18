"""Panel SUPERADMIN: todas las rutas exigen rol SUPERADMIN exacto, verificado en el servidor."""
from fastapi import APIRouter, Depends

from app.core.dependencies import require_superadmin
from app.modules.superadmin import access, config, monitor, requests, students, users

router = APIRouter(prefix="/superadmin", tags=["superadmin"], dependencies=[Depends(require_superadmin)])
for sub in (monitor.router, users.router, access.router, students.router, requests.router, config.router):
    router.include_router(sub)
