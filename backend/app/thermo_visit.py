"""
Термозамещение: шаг 2 визита (основной блок + шаблон), шаблоны клиента в БД.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile

from app.db.models import ClientThermoTemplate, Service
from app.questionnaire.schemas import ThermoTemplateNumbers, ThermoVisitDetails


def service_requires_thermo_flow(service: Service | None) -> bool:
    if service is None:
        return False
    sub = service.subcategory
    return bool(sub and sub.show_thermo_visit)


@dataclass
class ThermoFormParsed:
    curls_material: str
    material_length: str
    shade: str
    bases_total: int
    weight_with_margin: float
    template_mode: str
    old_template_id: int | None
    algorithm_changes: str
    tpl: ThermoTemplateNumbers


def _g_str(form: Any, name: str, default: str = "") -> str:
    v = form.get(name)
    if v is None or isinstance(v, UploadFile):
        return default
    if isinstance(v, (bytes, bytearray)):
        return v.decode().strip()
    return str(v).strip()


def _g_int0(form: Any, name: str) -> int:
    s = _g_str(form, name, "")
    if not s:
        return 0
    try:
        return int(float(s.replace(",", ".")))
    except ValueError:
        return 0


def _g_float0(form: Any, name: str) -> float:
    s = _g_str(form, name, "")
    if not s:
        return 0.0
    try:
        return float(s.replace(",", "."))
    except ValueError:
        return 0.0


def parse_thermo_from_form(form: Any) -> ThermoFormParsed:
    oid = _g_int0(form, "thermo_old_template_id")
    tpl = ThermoTemplateNumbers(
        strand_weight_avg=_g_float0(form, "thermo_tpl_strand_weight_avg"),
        row_1=_g_int0(form, "thermo_tpl_row_1"),
        row_2=_g_int0(form, "thermo_tpl_row_2"),
        row_3=_g_int0(form, "thermo_tpl_row_3"),
        other_rows_text=_g_str(form, "thermo_tpl_other_rows"),
        temples=_g_int0(form, "thermo_tpl_temples"),
        triangles=_g_int0(form, "thermo_tpl_triangles"),
        bird=_g_int0(form, "thermo_tpl_bird"),
        square=_g_int0(form, "thermo_tpl_square"),
        comment=_g_str(form, "thermo_tpl_comment"),
    )
    return ThermoFormParsed(
        curls_material=_g_str(form, "thermo_curls"),
        material_length=_g_str(form, "thermo_length"),
        shade=_g_str(form, "thermo_shade"),
        bases_total=_g_int0(form, "thermo_bases_total"),
        weight_with_margin=_g_float0(form, "thermo_weight"),
        template_mode=_g_str(form, "thermo_template_mode", "").upper(),
        old_template_id=oid if oid > 0 else None,
        algorithm_changes=_g_str(form, "thermo_algorithm_changes"),
        tpl=tpl,
    )


def collect_thermo_prefill_from_form(form: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    for k in form.keys():
        if not isinstance(k, str) or not k.startswith("thermo_"):
            continue
        vs = [v for v in form.getlist(k) if not isinstance(v, UploadFile)]
        if not vs:
            continue
        v = vs[-1]
        out[k] = v.decode() if isinstance(v, (bytes, bytearray)) else str(v)
    return out


def list_client_thermo_templates_for_visit(db: Session, client_id: int | None) -> list[ClientThermoTemplate]:
    if not client_id:
        return []
    return list(
        db.scalars(
            select(ClientThermoTemplate)
            .where(ClientThermoTemplate.client_id == client_id)
            .order_by(ClientThermoTemplate.created_at.desc(), ClientThermoTemplate.id.desc())
        ).all()
    )


def _template_from_db_row(row: ClientThermoTemplate) -> ThermoTemplateNumbers:
    try:
        raw = json.loads(row.template_json or "{}")
    except (json.JSONDecodeError, TypeError):
        raw = {}
    return ThermoTemplateNumbers.model_validate(raw)


def build_thermo_visit_details(
    parsed: ThermoFormParsed,
    db: Session,
    *,
    client_id: int | None,
) -> ThermoVisitDetails:
    mode: Literal["NEW", "OLD"]
    if parsed.template_mode == "NEW":
        mode = "NEW"
    elif parsed.template_mode == "OLD":
        mode = "OLD"
    else:
        raise ValueError("Выберите тип шаблона: «Новый» или «Старый».")

    templates = list_client_thermo_templates_for_visit(db, client_id)
    if mode == "OLD":
        if not templates:
            raise ValueError("У клиента нет сохранённых шаблонов термозамещения. Выберите «Новый» или сохраните визит после первого нового шаблона.")
        if not parsed.old_template_id:
            raise ValueError("Выберите сохранённый шаблон из списка.")
        row = db.get(ClientThermoTemplate, parsed.old_template_id)
        if row is None or row.client_id != client_id:
            raise ValueError("Сохранённый шаблон не найден или принадлежит другому клиенту.")
        snap = _template_from_db_row(row)
        return ThermoVisitDetails(
            curls_material=parsed.curls_material,
            material_length=parsed.material_length,
            shade=parsed.shade,
            bases_total=parsed.bases_total,
            weight_with_margin=parsed.weight_with_margin,
            template_mode="OLD",
            old_template_id=row.id,
            algorithm_changes=parsed.algorithm_changes or None,
            filled_template=None,
            saved_template_snapshot=snap,
        )

    return ThermoVisitDetails(
        curls_material=parsed.curls_material,
        material_length=parsed.material_length,
        shade=parsed.shade,
        bases_total=parsed.bases_total,
        weight_with_margin=parsed.weight_with_margin,
        template_mode="NEW",
        old_template_id=None,
        algorithm_changes=None,
        filled_template=parsed.tpl,
        saved_template_snapshot=None,
    )


def persist_new_thermo_template_if_needed(
    db: Session,
    *,
    client_id: int,
    details: ThermoVisitDetails,
    label_suffix: str,
) -> None:
    if details.template_mode != "NEW" or details.filled_template is None:
        return
    payload = details.filled_template.model_dump(mode="json")
    label = (label_suffix or "").strip()[:200] or "Термозамещение"
    db.add(
        ClientThermoTemplate(
            client_id=client_id,
            label=label,
            template_json=json.dumps(payload, ensure_ascii=False),
        )
    )
