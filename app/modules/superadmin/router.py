"""Panel SUPERADMIN: todas las rutas exigen rol SUPERADMIN exacto, verificado en el servidor."""
from fastapi import APIRouter, Depends

from app.core.dependencies import require_superadmin
from app.modules.superadmin import access, config, mail, monitor, requests, students, teachers, users

router = APIRouter(prefix="/superadmin", tags=["superadmin"], dependencies=[Depends(require_superadmin)])
for sub in (monitor.router, users.router, access.router, teachers.router, students.router, requests.router, config.router, mail.router):
    router.include_router(sub)
