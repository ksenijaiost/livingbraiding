"""Создание и правка карточки комплекта (админ, склад)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile

from app.db.models import Kit, KitAuthorStaff, User, UserRole


def _g_str(form: Any, name: str, default: str = "") -> str:
    v = form.get(name)
    if v is None:
        return default
    if isinstance(v, UploadFile):
        return default
    if isinstance(v, (bytes, bytearray)):
        return v.decode().strip()
    return str(v).strip()


def _g_bool(form: Any, name: str) -> bool:
    v = form.get(name)
    if v is None:
        return False
    if isinstance(v, UploadFile):
        return False
    s = v.decode() if isinstance(v, (bytes, bytearray)) else str(v)
    return s.lower() in ("on", "true", "1", "yes")


def _g_int(form: Any, name: str, default: int = 0) -> int:
    try:
        return int(_g_str(form, name, str(default)) or default)
    except ValueError:
        return default


def _g_float_opt(form: Any, name: str) -> float | None:
    raw = _g_str(form, name, "")
    if not raw:
        return None
    try:
        return float(raw.replace(",", "."))
    except ValueError:
        return None


@dataclass
class KitAdminFormData:
    sku: str
    title: str
    blank_type_de: bool
    blank_type_se: bool
    pieces_total: int
    pieces_available: int
    weight_grams: float | None
    length_cm: float | None
    has_decorations: bool
    materials_text: str | None
    color_text: str | None
    blanks_kinds_text: str | None
    notes: str | None
    description: str | None
    stock_price_total: float | None
    cost_total: float | None
    author_cost_total: float | None


def parse_kit_admin_form(form: Any, *, for_create: bool) -> KitAdminFormData:
    pieces_initial = _g_int(form, "pieces_initial", 0) if for_create else 0
    pieces_total = _g_int(form, "pieces_total", 0) if not for_create else pieces_initial
    pieces_available = _g_int(form, "pieces_available", 0) if not for_create else pieces_initial
    if for_create:
        pieces_total = max(0, pieces_initial)
        pieces_available = pieces_total

    return KitAdminFormData(
        sku=_g_str(form, "sku"),
        title=_g_str(form, "title"),
        blank_type_de=_g_bool(form, "blank_type_de"),
        blank_type_se=_g_bool(form, "blank_type_se"),
        pieces_total=max(0, pieces_total),
        pieces_available=max(0, pieces_available),
        weight_grams=_g_float_opt(form, "weight_grams"),
        length_cm=_g_float_opt(form, "length_cm"),
        has_decorations=_g_bool(form, "has_decorations"),
        materials_text=_g_str(form, "materials_text") or None,
        color_text=_g_str(form, "color_text") or None,
        blanks_kinds_text=_g_str(form, "blanks_kinds_text") or None,
        notes=_g_str(form, "notes") or None,
        description=_g_str(form, "description") or None,
        stock_price_total=_g_float_opt(form, "stock_price_total"),
        cost_total=_g_float_opt(form, "cost_total"),
        author_cost_total=_g_float_opt(form, "author_cost_total"),
    )


def validate_kit_admin_form(d: KitAdminFormData, *, for_create: bool) -> None:
    if not d.sku:
        raise ValueError("Укажите артикул")
    if not d.title:
        raise ValueError("Укажите название")
    if not d.blank_type_de and not d.blank_type_se:
        raise ValueError("Выберите тип заготовок: D.E и/или S.E.")
    if d.pieces_total <= 0:
        raise ValueError("Укажите количество заготовок (больше 0)")
    if not for_create and d.pieces_available > d.pieces_total:
        raise ValueError("Остаток не может быть больше количества заготовок")
    if d.stock_price_total is None:
        raise ValueError("Укажите цену комплекта на складе (всего), ₽")
    if d.stock_price_total <= 0:
        raise ValueError("Цена на складе должна быть больше 0")
    if len(d.sku) > 80:
        raise ValueError("Артикул слишком длинный")
    if len(d.title) > 200:
        raise ValueError("Название слишком длинное")


def parse_kit_author_user_ids_from_form(form: Any) -> list[int]:
    raw: list[Any] = []
    if hasattr(form, "getlist"):
        raw = list(form.getlist("kit_author_on"))
    else:
        v = form.get("kit_author_on")
        if v is not None:
            raw = [v]
    seen: set[int] = set()
    out: list[int] = []
    for x in raw:
        if isinstance(x, UploadFile):
            continue
        try:
            s = x.decode().strip() if isinstance(x, (bytes, bytearray)) else str(x).strip()
            i = int(s)
        except (ValueError, AttributeError):
            continue
        if i <= 0 or i in seen:
            continue
        seen.add(i)
        out.append(i)
    return out


def sync_kit_authors(db: Session, kit: Kit, form: Any) -> None:
    uids = parse_kit_author_user_ids_from_form(form)
    kit.author_external = _g_bool(form, "author_external")
    for uid in uids:
        u = db.get(User, uid)
        if not u or not u.is_active:
            raise ValueError("Выберите авторов только из активных сотрудников.")
        if u.role not in (UserRole.MASTER, UserRole.ADMIN, UserRole.ADMIN_SUPER):
            raise ValueError("Автор комплекта — только сотрудник студии.")
    db.execute(delete(KitAuthorStaff).where(KitAuthorStaff.kit_id == kit.id))
    for i, uid in enumerate(uids):
        db.add(KitAuthorStaff(kit_id=kit.id, user_id=uid, sort_order=i))


def kit_new_error_prefill(form: Any) -> dict[str, Any]:
    """Восстановление полей формы «новый комплект» после ошибки валидации."""
    d = parse_kit_admin_form(form, for_create=True)
    pi = _g_int(form, "pieces_initial", 0)
    return {
        "sku": d.sku,
        "title": d.title,
        "blank_type_de": "on" if d.blank_type_de else "",
        "blank_type_se": "on" if d.blank_type_se else "",
        "pieces_initial": pi if pi > 0 else "",
        "weight_grams": _g_str(form, "weight_grams"),
        "length_cm": _g_str(form, "length_cm"),
        "has_decorations": "on" if d.has_decorations else "",
        "materials_text": d.materials_text or "",
        "color_text": d.color_text or "",
        "blanks_kinds_text": d.blanks_kinds_text or "",
        "description": d.description or "",
        "notes": d.notes or "",
        "stock_price_total": _g_str(form, "stock_price_total"),
        "cost_total": _g_str(form, "cost_total"),
        "author_cost_total": _g_str(form, "author_cost_total"),
        "author_external": "on" if _g_bool(form, "author_external") else "",
        "kit_author_ids": parse_kit_author_user_ids_from_form(form),
    }


def kit_edit_error_prefill(form: Any) -> dict[str, Any]:
    d = parse_kit_admin_form(form, for_create=False)
    return {
        "sku": d.sku,
        "title": d.title,
        "blank_type_de": "on" if d.blank_type_de else "",
        "blank_type_se": "on" if d.blank_type_se else "",
        "pieces_total": d.pieces_total,
        "pieces_available": d.pieces_available,
        "weight_grams": _g_str(form, "weight_grams"),
        "length_cm": _g_str(form, "length_cm"),
        "has_decorations": "on" if d.has_decorations else "",
        "materials_text": d.materials_text or "",
        "color_text": d.color_text or "",
        "blanks_kinds_text": d.blanks_kinds_text or "",
        "description": d.description or "",
        "notes": d.notes or "",
        "stock_price_total": _g_str(form, "stock_price_total"),
        "cost_total": _g_str(form, "cost_total"),
        "author_cost_total": _g_str(form, "author_cost_total"),
        "author_external": "on" if _g_bool(form, "author_external") else "",
        "kit_author_ids": parse_kit_author_user_ids_from_form(form),
    }


def kit_to_form_prefill(kit: Kit) -> dict[str, Any]:
    """Значения для шаблона формы редактирования (чекбоксы — 'on' или '')."""
    w = "" if kit.weight_grams is None else str(kit.weight_grams).replace(",", ".")
    ln = "" if kit.length_cm is None else str(kit.length_cm).replace(",", ".")
    sp = "" if kit.stock_price_total is None else str(kit.stock_price_total).replace(",", ".")
    ct = "" if kit.cost_total is None else str(kit.cost_total).replace(",", ".")
    ac = "" if kit.author_cost_total is None else str(kit.author_cost_total).replace(",", ".")
    return {
        "sku": kit.sku,
        "title": kit.title,
        "blank_type_de": "on" if kit.blank_type_de else "",
        "blank_type_se": "on" if kit.blank_type_se else "",
        "pieces_initial": "",
        "pieces_total": kit.pieces_total,
        "pieces_available": kit.pieces_available,
        "weight_grams": w,
        "length_cm": ln,
        "has_decorations": "on" if kit.has_decorations else "",
        "materials_text": kit.materials_text or "",
        "color_text": kit.color_text or "",
        "blanks_kinds_text": kit.blanks_kinds_text or "",
        "description": kit.description or "",
        "notes": kit.notes or "",
        "stock_price_total": sp,
        "cost_total": ct,
        "author_cost_total": ac,
        "author_external": "on" if kit.author_external else "",
        "kit_author_ids": [
            l.user_id
            for l in sorted(
                kit.author_staff_links or [],
                key=lambda x: (x.sort_order, x.id),
            )
        ],
    }


def apply_kit_admin_form(kit: Kit, d: KitAdminFormData) -> None:
    kit.sku = d.sku[:80]
    kit.title = d.title[:200]
    kit.blank_type_de = d.blank_type_de
    kit.blank_type_se = d.blank_type_se
    kit.pieces_total = d.pieces_total
    kit.pieces_available = d.pieces_available
    kit.weight_grams = d.weight_grams
    kit.length_cm = d.length_cm
    kit.has_decorations = d.has_decorations
    kit.materials_text = d.materials_text
    kit.color_text = (d.color_text[:200] if d.color_text else None)
    kit.blanks_kinds_text = d.blanks_kinds_text
    kit.notes = d.notes
    kit.description = d.description
    kit.stock_price_total = d.stock_price_total
    kit.cost_total = d.cost_total
    kit.author_cost_total = d.author_cost_total
    if kit.pieces_available <= 0:
        kit.is_in_stock = False
    else:
        kit.is_in_stock = True
