"""Виды консультации (чекбоксы + «другое»). Расширение — новые строки в CONSULTATION_TYPE_CHOICES."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select

from app.db.models import ConsultationKind, ServiceCategory

CONSULTATION_TYPE_OTHER = "OTHER"

CONSULTATION_TYPE_CHOICES: list[tuple[str, str]] = [
    ("BRAIDING", "Плетение"),
    ("EXTENSION", "Наращивание"),
    (CONSULTATION_TYPE_OTHER, "Другое"),
]

_CONSULTATION_TYPE_LABELS = dict(CONSULTATION_TYPE_CHOICES)

_EXTENSION_CATEGORY_NAMES = frozenset({"Наращивание"})
_OTHER_CATEGORY_NAMES = frozenset({"Снятие", "Уход", "Обучение"})


def consultation_kind_for_category_name(name: str) -> ConsultationKind:
    nm = (name or "").strip()
    if nm in _EXTENSION_CATEGORY_NAMES:
        return ConsultationKind.EXTENSION
    if nm in _OTHER_CATEGORY_NAMES:
        return ConsultationKind.OTHER
    return ConsultationKind.BRAIDING


def consultation_kind_label(kind: ConsultationKind | str | None) -> str:
    code = kind.value if isinstance(kind, ConsultationKind) else str(kind or "")
    return _CONSULTATION_TYPE_LABELS.get(code, code or "—")


def parse_types_from_form(form_types: list[str], other_text: str | None) -> dict[str, Any]:
    """Собрать types_json из полей формы."""
    data: dict[str, Any] = {}
    for code, _label in CONSULTATION_TYPE_CHOICES:
        if code in form_types:
            data[code] = True
    other = (other_text or "").strip()
    if CONSULTATION_TYPE_OTHER in form_types and other:
        data["other_text"] = other[:500]
    elif other and CONSULTATION_TYPE_OTHER not in form_types:
        pass
    return data


def types_json_dumps(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False)


def types_json_loads(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        v = json.loads(raw)
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def format_types_display(raw: str | None) -> str:
    data = types_json_loads(raw)
    parts: list[str] = []
    for code, label in CONSULTATION_TYPE_CHOICES:
        if code == CONSULTATION_TYPE_OTHER:
            continue
        if data.get(code):
            parts.append(label)
    if data.get(CONSULTATION_TYPE_OTHER) or data.get("other_text"):
        ot = str(data.get("other_text") or "").strip()
        parts.append(f"Другое" + (f": {ot}" if ot else ""))
    return ", ".join(parts) if parts else "—"


def list_consultation_services_catalog(db) -> list[dict]:
    """Каталог услуг для консультации с consultation_kind у каждой категории."""
    from app.kit_inlay_visit import list_master_visit_services_catalog

    kinds = {
        int(c.id): c.consultation_kind
        for c in db.scalars(select(ServiceCategory)).all()
    }
    full = list_master_visit_services_catalog(db)
    for c in full:
        kind = kinds.get(int(c.get("id") or 0), ConsultationKind.BRAIDING)
        c["consultation_kind"] = kind.value if isinstance(kind, ConsultationKind) else str(kind)
    return full


def filter_consultation_catalog_by_types(catalog: list[dict], selected_types: list[str]) -> list[dict]:
    """Оставить категории, чей consultation_kind совпадает с выбранными видами консультации."""
    allowed = {str(t).strip().upper() for t in (selected_types or []) if str(t).strip()}
    if not allowed:
        return []
    out: list[dict] = []
    for c in catalog or []:
        kind = str(c.get("consultation_kind") or "").upper()
        if kind not in allowed:
            continue
        out.append(c)
    return out


def validate_types_selected(data: dict[str, Any]) -> str | None:
    """None если ок, иначе текст ошибки."""
    has_type = any(data.get(code) for code, _ in CONSULTATION_TYPE_CHOICES if code != CONSULTATION_TYPE_OTHER)
    if data.get(CONSULTATION_TYPE_OTHER):
        has_type = True
        if not str(data.get("other_text") or "").strip():
            return "Укажите текст для вида «Другое»."
    if not has_type:
        return "Выберите хотя бы один вид консультации."
    return None
