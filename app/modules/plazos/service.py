"""Plazos académicos: año lectivo → trimestres → aportes, aporte actual y cupo."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from bson import ObjectId

from app.core.exceptions import BadRequest, Conflict, NotFound
from app.modules.catalog import APORTE_QUOTA_BY_COURSE
from app.modules.plazos.repository import AnioLectivoRepository, RequestReadRepository
from app.modules.plazos.schemas import UpsertAnioLectivoDTO
from app.shared.utilities.dates import parse_js_date
from app.shared.validators.object_id import is_valid_object_id


def _utc(d: datetime) -> datetime:
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


class PlazosService:
    def __init__(self, anios: AnioLectivoRepository, requests: RequestReadRepository):
        self.anios = anios
        self.requests = requests

    async def find_all(self) -> list[dict[str, Any]]:
        return await self.anios.find_all()

    async def create(self, dto: UpsertAnioLectivoDTO) -> dict[str, Any]:
        await self._assert_name_available(dto.name)
        tree = self._to_ranges(dto)
        self._validate_tree(tree)
        return await self.anios.create(self._to_persistable(tree))

    async def update(self, id_: str, dto: UpsertAnioLectivoDTO) -> dict[str, Any]:
        if not is_valid_object_id(id_):
            raise NotFound(f"Año lectivo con id {id_} no encontrado")
        await self._assert_name_available(dto.name, exclude_id=id_)
        tree = self._to_ranges(dto)
        self._validate_tree(tree)
        updated = await self.anios.update(ObjectId(id_), self._to_persistable(tree))
        if updated is None:
            raise NotFound(f"Año lectivo con id {id_} no encontrado")
        return updated

    async def remove(self, id_: str) -> dict[str, Any]:
        if not is_valid_object_id(id_):
            raise NotFound(f"Año lectivo con id {id_} no encontrado")
        deleted = await self.anios.delete(ObjectId(id_))
        if deleted is None:
            raise NotFound(f"Año lectivo con id {id_} no encontrado")
        return deleted

    async def find_aporte_actual(self, date: datetime | None = None) -> dict[str, Any] | None:
        """El aporte cuya ventana [startDate, endDate] contiene la fecha (inclusive)."""
        date = date or datetime.now(timezone.utc)
        for anio in await self.anios.find_covering(date):
            for trimestre in anio.get("trimestres", []):
                if _utc(trimestre["startDate"]) > date or _utc(trimestre["endDate"]) < date:
                    continue
                for aporte in trimestre.get("aportes", []):
                    if _utc(aporte["startDate"]) <= date <= _utc(aporte["endDate"]):
                        return {"anioLectivo": anio, "trimestre": trimestre, "aporte": aporte}
        return None

    async def get_cupo_info(self, student_dni: str, course: str, request_type: str) -> dict[str, Any]:
        """Cupo de materias del alumno en el aporte actual (cuenta materias, no documentos)."""
        aporte_actual = await self.find_aporte_actual()
        if aporte_actual is None:
            raise BadRequest("No hay un periodo de aportes activo actualmente.")
        limite = APORTE_QUOTA_BY_COURSE[course]
        aporte = aporte_actual["aporte"]
        docs = await self.requests.find_for_quota(student_dni, request_type, aporte["startDate"], aporte["endDate"])
        consumido = 0
        for r in docs:
            payload = r.get("payload")
            subjects = payload.get("subjects") if isinstance(payload, dict) else None
            consumido += len(subjects) if isinstance(subjects, list) else 0
        return {"aporteActual": aporte_actual, "limite": limite, "consumido": consumido,
                "disponible": max(0, limite - consumido)}

    # ── validación del árbol ────────────────────────────────────────────────
    @staticmethod
    def _to_ranges(dto: UpsertAnioLectivoDTO) -> dict[str, Any]:
        def rng(o: Any) -> dict[str, Any]:
            try:
                return {"name": o.name, "startDate": parse_js_date(o.startDate), "endDate": parse_js_date(o.endDate)}
            except ValueError as exc:
                raise BadRequest(f'"{o.name}": fecha inválida.') from exc

        return {**rng(dto), "trimestres": [
            {**rng(t), "aportes": [rng(a) for a in (t.aportes or [])]} for t in (dto.trimestres or [])
        ]}

    @staticmethod
    def _to_persistable(tree: dict[str, Any]) -> dict[str, Any]:
        # Mismo orden de campos que Mongoose: name, startDate, endDate, [aportes], _id.
        return {
            "name": tree["name"], "startDate": tree["startDate"], "endDate": tree["endDate"],
            "trimestres": [{
                "name": t["name"], "startDate": t["startDate"], "endDate": t["endDate"],
                "aportes": [{"name": a["name"], "startDate": a["startDate"], "endDate": a["endDate"], "_id": ObjectId()}
                            for a in t["aportes"]],
                "_id": ObjectId(),
            } for t in tree["trimestres"]],
        }

    async def _assert_name_available(self, name: str, exclude_id: str | None = None) -> None:
        existing = await self.anios.find_by_name(name)
        if existing and str(existing["_id"]) != exclude_id:
            raise Conflict(f'Ya existe un año lectivo llamado "{name}".')

    def _validate_tree(self, anio: dict[str, Any]) -> None:
        self._assert_valid_range(anio)
        trimestres = anio["trimestres"]
        self._assert_no_overlaps(trimestres, f'los trimestres de "{anio["name"]}"')
        for t in trimestres:
            self._assert_valid_range(t)
            self._assert_within(t, anio, "trimestre", f'el año "{anio["name"]}"')
            self._assert_no_overlaps(t["aportes"], f'los aportes del trimestre "{t["name"]}"')
            for a in t["aportes"]:
                self._assert_valid_range(a)
                self._assert_within(a, t, "aporte", f'el trimestre "{t["name"]}"')

    @staticmethod
    def _assert_valid_range(r: dict[str, Any]) -> None:
        if r["startDate"] > r["endDate"]:
            raise BadRequest(f'"{r["name"]}": la fecha de inicio no puede ser posterior a la de fin.')

    @staticmethod
    def _assert_within(child: dict, parent: dict, child_label: str, parent_label: str) -> None:
        if child["startDate"] < parent["startDate"] or child["endDate"] > parent["endDate"]:
            raise BadRequest(f'"{child["name"]}" ({child_label}) debe estar dentro de las fechas de {parent_label}.')

    @staticmethod
    def _assert_no_overlaps(items: list[dict], label: str) -> None:
        ordered = sorted(items, key=lambda x: x["startDate"])  # orden estable, como Array.sort
        for i in range(1, len(ordered)):
            if ordered[i]["startDate"] <= ordered[i - 1]["endDate"]:
                raise BadRequest(
                    f'Las fechas de "{ordered[i - 1]["name"]}" y "{ordered[i]["name"]}" se solapan ({label}).')
