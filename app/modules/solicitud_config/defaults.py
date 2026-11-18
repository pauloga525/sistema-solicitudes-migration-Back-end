"""Configuración inicial por tipo de trámite (solicitud-config.schema.ts → DEFAULT_CONFIG)."""
from app.modules.catalog import COURSES
from app.modules.curriculum import CURRICULUM_SUBJECTS

INITIAL_ACTIVE_COURSES = set(COURSES)  # todos los cursos habilitados (pedido de la institución, nov. 2026)

DEFAULT_CONFIG: dict[str, dict[str, list[dict]]] = {
    "proceso_mejora": {
        "courses": [{"name": c, "active": 1 if c in INITIAL_ACTIVE_COURSES else 0} for c in COURSES],
        "subjects": [{"name": s, "active": 1} for s in CURRICULUM_SUBJECTS],
    },
}


def default_for(request_type: str) -> dict[str, list[dict]]:
    seed = DEFAULT_CONFIG.get(request_type)
    if seed is None:
        seed = {"courses": [{"name": c, "active": 1} for c in COURSES],
                "subjects": [{"name": s, "active": 1} for s in CURRICULUM_SUBJECTS]}
    return {"courses": [dict(i) for i in seed["courses"]], "subjects": [dict(i) for i in seed["subjects"]]}
