from __future__ import annotations

from typing import Any

from app.core.exceptions import NotFound
from app.modules.curriculum import CURRICULUM_SUBJECTS
from app.modules.curriculum_data import CURRICULUM
from app.modules.solicitud_config.defaults import default_for
from app.modules.solicitud_config.repository import SolicitudConfigRepository

_CURRENT = set(CURRICULUM_SUBJECTS)
_ORDER = {s: i for i, s in enumerate(CURRICULUM_SUBJECTS)}
_ALL_COURSES_MARKER = "allCoursesActivatedAt"


class SolicitudConfigService:
    def __init__(self, repo: SolicitudConfigRepository):
        self.repo = repo

    async def _raw(self, request_type: str) -> dict[str, Any]:
        """Get-or-create: si no existe la configuración del tipo, la crea con los valores por defecto.

        Las configuraciones anteriores se completan (sin tocar ni borrar lo existente) con las
        materias de la malla y con la activación por curso, que parte del valor general de cada materia.
        """
        config = await self.repo.find_by_type(request_type)
        if config is None:
            config = await self.repo.create(request_type, default_for(request_type))
        if _ALL_COURSES_MARKER not in config:
            # Pedido de la institución (nov. 2026): habilitar todos los cursos. Se aplica una sola vez;
            # luego el administrador puede desactivar los que quiera y no se vuelve a tocar.
            config = await self.repo.activate_all_courses_once(config["_id"], _ALL_COURSES_MARKER) \
                or await self.repo.find_by_type(request_type) or config
        have = {i.get("name") for i in config.get("subjects", [])}
        missing = [{"name": s, "active": 1} for s in CURRICULUM_SUBJECTS if s not in have]
        if missing:
            config = await self.repo.add_subjects(config["_id"], missing) or await self.repo.find_by_type(request_type) or config
        general = {i.get("name"): i.get("active") for i in config.get("subjects", [])}
        pairs = {(i.get("key"), i.get("name")) for i in config.get("courseSubjects", [])}
        new = [{"key": k, "name": n, "active": 1 if general.get(n, 1) == 1 else 0}
               for k, names in CURRICULUM.items() for n in names if (k, n) not in pairs]
        if new:
            config = await self.repo.add_course_subjects(config["_id"], new) or await self.repo.find_by_type(request_type) or config
        return config

    async def get_config(self, request_type: str) -> dict[str, Any]:
        return _view(await self._raw(request_type))

    async def inactive_subjects(self, request_type: str, course: str, specialty: str | None) -> set[str]:
        """Materias desactivadas para ese curso/especialidad."""
        config = await self._raw(request_type)
        key = f"{course}|{specialty or ''}"
        return {i["name"] for i in config.get("courseSubjects", []) if i.get("key") == key and i.get("active") != 1}

    async def course_subjects(self, request_type: str) -> list[dict[str, Any]]:
        return _groups(await self._raw(request_type))

    async def update_course_subject(self, request_type: str, key: str, name: str, active: int) -> dict[str, Any]:
        if name not in CURRICULUM.get(key, ()):
            raise NotFound(f'Materia "{name}" no encontrada para ese curso')
        config = await self._raw(request_type)
        updated = await self.repo.set_course_subject(config["_id"], key, name, active)
        if updated is None:
            raise NotFound(f'Materia "{name}" no encontrada para ese curso')
        return next(g for g in _groups(updated) if g["key"] == key)

    async def update_course(self, request_type: str, name: str, active: int) -> dict[str, Any]:
        return await self._update(request_type, "courses", name, active, f'Curso "{name}" no encontrado')

    async def update_subject(self, request_type: str, name: str, active: int) -> dict[str, Any]:
        view = await self._update(request_type, "subjects", name, active, f'Materia "{name}" no encontrada')
        await self.repo.set_subject_all_courses(view["_id"], name, active)
        return view

    async def _update(self, request_type: str, field: str, name: str, active: int, not_found: str) -> dict[str, Any]:
        config = _view(await self._raw(request_type))
        if not any(i.get("name") == name for i in config.get(field, [])):
            raise NotFound(not_found)
        updated = await self.repo.set_item_active(config["_id"], field, name, active)
        if updated is None:
            raise NotFound(not_found)
        return _view(updated)


def _view(config: dict[str, Any]) -> dict[str, Any]:
    """Solo las materias de la malla vigente, en orden; las históricas quedan guardadas sin mostrarse.
    La activación por curso se consulta aparte (course_subjects)."""
    subjects = sorted((i for i in config.get("subjects", []) if i.get("name") in _CURRENT), key=lambda i: _ORDER[i["name"]])
    return {k: v for k, v in config.items() if k not in ("courseSubjects", _ALL_COURSES_MARKER)} | {"subjects": subjects}


def _groups(config: dict[str, Any]) -> list[dict[str, Any]]:
    state = {(i.get("key"), i.get("name")): i.get("active") for i in config.get("courseSubjects", [])}
    from app.modules.catalog import COURSES
    order = {c: i for i, c in enumerate(COURSES)}
    out = []
    for key in sorted(CURRICULUM, key=lambda k: (order[k.split("|")[0]], k)):
        course, spec = key.split("|")
        out.append({"key": key, "course": course, "specialty": spec or None,
                    "subjects": [{"name": n, "active": 1 if state.get((key, n), 1) == 1 else 0} for n in CURRICULUM[key]]})
    return out
