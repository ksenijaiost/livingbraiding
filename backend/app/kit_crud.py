"""Создание и правка карточки комплекта (админ, склад)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile

from app.db.models import CatalogProduct, Kit, KitAuthorStaff, KitBlanksCondition, User, UserRole
from app.kit_composition import kit_inventory_piece_count
from app.user_roles import select_users_with_role, user_has_role

_KIT_QTY_FIELD = re.compile(r"^kit_qty_(\d+)_(.+)$")

# Ключи, исключённые из цены клиента до появления флагов в каталоге (оставляем для совместимости).
LEGACY_KIT_CLIENT_PRICE_EXCLUDE_KEYS: frozenset[str] = frozenset(
    {"SE_TIP_ADDON", "SE_TRIM_SHORT", "SE_TRIM_LONG", "DE_TRIM"}
)


def kit_key_excluded_from_client_price(meta: dict[str, Any], kit_key: str) -> bool:
    """Не суммировать прайсовую цену этой заготовки в «цену для клиента» по комплекту."""
    if kit_key in LEGACY_KIT_CLIENT_PRICE_EXCLUDE_KEYS:
        return True
    if bool(meta.get("ignore_in_calc")):
        return True
    if bool(meta.get("is_bu")):
        return True
    return False


def list_masters_for_kit_author_pick(db: Session) -> list[User]:
    """Активные пользователи с ролью MASTER (выбор авторов комплекта)."""

    return list(
        db.scalars(
            select_users_with_role(UserRole.MASTER).order_by(
                User.display_name.asc(), User.id.asc()
            )
        ).all()
    )


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


def _parse_kit_blanks_condition_from_form(form: Any, name: str = "blanks_condition") -> KitBlanksCondition:
    raw = (_g_str(form, name, "") or "").strip().upper()
    if not raw:
        return KitBlanksCondition.NEW
    mapping = {
        "NEW": KitBlanksCondition.NEW,
        "USED": KitBlanksCondition.USED,
        "MIXED": KitBlanksCondition.MIXED,
    }
    if raw in mapping:
        return mapping[raw]
    raise ValueError("Состояние заготовок в комплекте: выберите «Новый», «Б/У» или «50 на 50».")


def _g_float_opt(form: Any, name: str) -> float | None:
    raw = _g_str(form, name, "")
    if not raw:
        return None
    try:
        return float(raw.replace(",", "."))
    except ValueError:
        return None


def _g_float_nonneg(form: Any, name: str, default: float = 0.0) -> float:
    raw = _g_str(form, name, "")
    if not raw:
        return default
    try:
        return max(0.0, float(raw.replace(",", ".")))
    except ValueError:
        return default


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
    materials_text: str | None
    color_text: str | None
    notes: str | None
    description: str | None
    stock_price_total: float | None
    cost_total: float | None
    discount_percent: int
    blanks_condition: KitBlanksCondition = KitBlanksCondition.NEW
    is_active: bool = True
    composition_totals: dict[str, int] = field(default_factory=dict)
    composition_lines: list[Any] = field(default_factory=list)


def parse_kit_qty_totals_from_form(form: Any) -> dict[str, int]:
    """Суммирует kit_qty_<userId>_<itemKey> по itemKey (как в «Работа с товарами»)."""
    totals: dict[str, int] = {}
    try:
        keys_iter = list(form.keys())
    except Exception:
        keys_iter = []
    for name in keys_iter:
        if not isinstance(name, str):
            continue
        m = _KIT_QTY_FIELD.match(name)
        if not m:
            continue
        item_key = m.group(2)
        if not item_key:
            continue
        raw = _g_str(form, name, "0")
        try:
            q = int(raw or "0")
        except ValueError:
            q = 0
        q = max(0, q)
        if q > 0:
            totals[item_key] = totals.get(item_key, 0) + q
    return totals


def parse_kit_admin_form(form: Any, *, for_create: bool) -> KitAdminFormData:
    from app.kit_composition_lines import (
        infer_blanks_condition,
        inventory_piece_count,
        lines_from_form,
        lines_to_legacy_totals,
    )

    composition_lines = lines_from_form(form)
    if not composition_lines:
        composition_lines = []
    composition_totals: dict[str, int] = {}
    if for_create:
        if composition_lines:
            composition_totals = lines_to_legacy_totals(composition_lines)
        else:
            composition_totals = parse_kit_qty_totals_from_form(form)
        inv = (
            inventory_piece_count(composition_lines)
            if composition_lines
            else kit_inventory_piece_count(composition_totals)
        )
        if composition_totals and any(q > 0 for q in composition_totals.values()):
            pieces_total = pieces_available = inv
        else:
            pieces_initial = _g_int(form, "pieces_initial", 0)
            pieces_total = max(0, pieces_initial)
            pieces_available = pieces_total
    else:
        pieces_total = _g_int(form, "pieces_total", 0)
        pieces_available = _g_int(form, "pieces_available", 0)

    return KitAdminFormData(
        sku=_g_str(form, "sku"),
        title=_g_str(form, "title"),
        blank_type_de=_g_bool(form, "blank_type_de"),
        blank_type_se=_g_bool(form, "blank_type_se"),
        pieces_total=max(0, pieces_total),
        pieces_available=max(0, pieces_available),
        weight_grams=_g_float_opt(form, "weight_grams"),
        length_cm=_g_float_opt(form, "length_cm"),
        materials_text=_g_str(form, "materials_text") or None,
        color_text=_g_str(form, "color_text") or None,
        notes=_g_str(form, "notes") or None,
        description=_g_str(form, "description") or None,
        stock_price_total=_g_float_opt(form, "stock_price_total"),
        cost_total=_g_float_opt(form, "cost_total"),
        discount_percent=_g_discount_percent_from_form_field(form, "discount_percent", 0),
        blanks_condition=(
            infer_blanks_condition(composition_lines)
            if composition_lines
            else _parse_kit_blanks_condition_from_form(form)
        ),
        is_active=(
            True
            if for_create and form.get("is_active") is None
            else _g_bool(form, "is_active")
        ),
        composition_totals=dict(composition_totals),
        composition_lines=list(composition_lines),
    )


def infer_blank_types_from_composition_totals(totals: dict[str, int]) -> tuple[bool, bool]:
    """По ключам DE_*, SE_* (или устаревшим DE / SE) определить типы заготовок D.E. / S.E."""
    de = False
    se = False
    for k, q in (totals or {}).items():
        try:
            qn = int(q)
        except (TypeError, ValueError):
            continue
        if qn <= 0:
            continue
        kk = str(k).strip().upper()
        if kk.startswith("DE_") or kk == "DE":
            de = True
        elif kk.startswith("SE_") or kk == "SE":
            se = True
    return de, se


def try_fill_kit_admin_blank_types_from_composition(
    d: KitAdminFormData,
    *,
    composition_totals: dict[str, int] | None = None,
) -> None:
    """Если D.E./S.E. не отмечены, выставить по составу (ключи kit_key в composition)."""
    if d.blank_type_de or d.blank_type_se:
        return
    totals = composition_totals if composition_totals is not None else (d.composition_totals or {})
    ide, ise = infer_blank_types_from_composition_totals(totals)
    d.blank_type_de = ide
    d.blank_type_se = ise


def max_kit_discount_percent_allowed(stock_price: float, cost_total: float) -> int:
    """Макс. целые % скидки: итоговая цена не ниже себестоимости (с ЗП)."""
    price = float(stock_price)
    cost = float(cost_total)
    if price <= 0:
        return 0
    margin = price - cost
    if margin <= 0:
        return 0
    return int(margin / price * 100 + 1e-9)


def calc_kit_stock_price_total_from_composition(
    db: Session, kit: Kit
) -> tuple[float | None, list[str]]:
    """
    Рассчитать «цену на складе (всего)» по составу комплекта (composition_json)
    и прайсу `catalog_products` (категория «Заказ» → «Заготовки поштучно»).

    Возвращает: (price_total_or_none, missing_keys).
    """
    from app.kit_composition_lines import client_price_for_lines, lines_from_json

    raw = getattr(kit, "composition_json", None)
    if not raw:
        return None, []
    lines = lines_from_json(str(raw))
    if lines:
        total, missing = client_price_for_lines(db, lines, extra_costs_amount=0.0)
        if missing:
            return None, missing
        return float(total), []

    try:
        payload = json.loads(str(raw))
    except Exception:
        return None, ["<composition_json invalid>"]
    return None, ["<composition_json invalid>"]


def try_fill_kit_admin_stock_price_total_from_composition(
    db: Session,
    d: KitAdminFormData,
    *,
    composition_totals: dict[str, int],
) -> None:
    """Если цена не введена, подставить сумму по прайсу «Заказ» → «Заготовки поштучно» по составу."""
    cur = d.stock_price_total
    if cur is not None and float(cur) > 0:
        return
    if d.composition_lines:
        from app.kit_composition_lines import client_price_for_lines

        total, missing = client_price_for_lines(db, d.composition_lines, extra_costs_amount=0.0)
        if missing:
            raise ValueError(
                "Цена на складе не указана; автоподстановка по прайсу невозможна — нет цен для ключей: "
                + ", ".join(missing)
            )
        if total > 0:
            d.stock_price_total = float(total)
        return
    totals = {str(k): int(v) for k, v in (composition_totals or {}).items() if int(v) > 0}
    if not totals:
        return
    tmp = Kit()
    tmp.composition_json = json.dumps(totals, ensure_ascii=False, sort_keys=True)
    price, missing = calc_kit_stock_price_total_from_composition(db, tmp)
    if missing:
        raise ValueError(
            "Цена на складе не указана; автоподстановка по прайсу невозможна — нет цен для ключей: "
            + ", ".join(missing)
        )
    if price is None or float(price) <= 0:
        return
    d.stock_price_total = float(price)


def estimate_kit_admin_cost_total(
    db: Session,
    lines: list[Any],
    *,
    weight_grams: float | None = None,
) -> float:
    """Примерная себестоимость: работа по видам (+ % для б/у) + вес × цена канекалона."""
    from app.kit_composition_lines import BlankCondition, filter_nonempty, _work_pay_for_key
    from app.work_products import _material_prices_per_gram

    total = 0.0
    for ln in filter_nonempty(lines):
        q = ln.total_qty()
        if q <= 0:
            continue
        rate = _work_pay_for_key(db, ln.key)
        if rate <= 0:
            continue
        if ln.condition == BlankCondition.USED:
            pct = max(1, min(100, int(ln.used_price_pct or 100)))
            total += rate * (float(pct) / 100.0) * float(q)
        else:
            total += rate * float(q)
    wg = float(weight_grams or 0.0)
    if wg > 0:
        kpg, _kudpg = _material_prices_per_gram(db)
        total += wg * float(kpg)
    return float(total)


def try_fill_kit_admin_cost_total_from_composition(
    db: Session,
    d: KitAdminFormData,
) -> None:
    """Если себестоимость не введена — подставить примерный расчёт по составу и весу."""
    cur = d.cost_total
    if cur is not None and float(cur) > 0:
        return
    if not d.composition_lines:
        return
    est = estimate_kit_admin_cost_total(
        db, d.composition_lines, weight_grams=d.weight_grams
    )
    if est > 0:
        d.cost_total = float(est)


def _g_discount_percent_from_form_field(form: Any, name: str, default: int = 0) -> int:
    raw = _g_str(form, name, "")
    if not raw:
        return default
    raw = raw.replace(",", ".").strip()
    try:
        v = float(raw)
    except ValueError:
        raise ValueError("Скидка: укажите целое число процентов от 0 до 100.")
    if v < 0 or v > 100:
        raise ValueError("Скидка — целое число процентов от 0 до 100.")
    if abs(v - round(v)) > 1e-6:
        raise ValueError("Скидка указывается целым числом процентов, без десятых.")
    return int(round(v))


def validate_kit_admin_form(d: KitAdminFormData, *, for_create: bool) -> None:
    if not d.sku:
        raise ValueError("Укажите артикул")
    if not d.title:
        raise ValueError("Укажите название")
    if not d.blank_type_de and not d.blank_type_se:
        raise ValueError("Выберите тип заготовок: D.E и/или S.E.")
    if for_create:
        if not d.composition_lines and not d.composition_totals and d.pieces_total <= 0:
            raise ValueError(
                "Для нового комплекта укажите в таблице видов заготовок хотя бы одно ненулевое количество."
            )
    if d.pieces_total <= 0:
        raise ValueError(
            "Количество заготовок на складе по комплекту должно быть больше 0 "
            "(заполните виды в таблице; стрижки в состав не входят)."
        )
    if not for_create and d.pieces_available > d.pieces_total:
        raise ValueError("Остаток не может быть больше количества заготовок")
    if d.stock_price_total is None:
        raise ValueError(
            "Укажите цену комплекта на складе (₽) или заполните состав для автоподстановки по прайсу "
            "«Заказ» → «Заготовки поштучно» (все ключи состава должны иметь цену в каталоге)."
        )
    if d.stock_price_total <= 0:
        raise ValueError("Цена на складе должна быть больше 0")
    if d.cost_total is None or d.cost_total <= 0:
        raise ValueError(
            "Укажите себестоимость комплекта (всего), ₽ — сумма затрат и ЗП авторов за весь комплект."
        )
    max_pct = max_kit_discount_percent_allowed(d.stock_price_total, d.cost_total)
    if d.discount_percent > max_pct:
        raise ValueError(
            f"Скидка не больше {max_pct}% от цены: итог не ниже себестоимости (с ЗП мастеров)."
        )
    if len(d.sku) > 80:
        raise ValueError("Артикул слишком длинный")
    if len(d.title) > 200:
        raise ValueError("Название слишком длинное")


def parse_discount_percent_from_form(form: Any) -> int:
    """Поле скидки в % (inline-форма в списке/карточке)."""
    return _g_discount_percent_from_form_field(form, "discount_percent", 0)


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
    sync_kit_authors_from_user_ids(
        db,
        kit,
        author_user_ids=parse_kit_author_user_ids_from_form(form),
        author_external=_g_bool(form, "author_external"),
    )


def sync_kit_authors_from_user_ids(
    db: Session,
    kit: Kit,
    *,
    author_user_ids: list[int] | None,
    author_external: bool,
) -> None:
    """Те же правила, что у формы: только активные мастера."""
    uids: list[int] = []
    seen: set[int] = set()
    for x in author_user_ids or []:
        try:
            i = int(x)
        except (TypeError, ValueError):
            continue
        if i <= 0 or i in seen:
            continue
        seen.add(i)
        uids.append(i)
    kit.author_external = bool(author_external)
    for uid in uids:
        u = db.get(User, uid)
        if not u or not u.is_active:
            raise ValueError("Выберите авторов только из активных сотрудников.")
        if not user_has_role(db, uid, UserRole.MASTER):
            raise ValueError("Автор комплекта указывается только среди мастеров.")
    db.execute(delete(KitAuthorStaff).where(KitAuthorStaff.kit_id == kit.id))
    for i, uid in enumerate(uids):
        db.add(KitAuthorStaff(kit_id=kit.id, user_id=uid, sort_order=i))


def kit_new_error_prefill(form: Any) -> dict[str, Any]:
    """Восстановление полей формы «новый комплект» после ошибки валидации."""
    d = parse_kit_admin_form(form, for_create=True)
    out: dict[str, Any] = {
        "sku": d.sku,
        "title": d.title,
        "blank_type_de": "on" if d.blank_type_de else "",
        "blank_type_se": "on" if d.blank_type_se else "",
        "weight_grams": _g_str(form, "weight_grams"),
        "length_cm": _g_str(form, "length_cm"),
        "materials_text": d.materials_text or "",
        "color_text": d.color_text or "",
        "description": d.description or "",
        "notes": d.notes or "",
        "stock_price_total": _g_str(form, "stock_price_total"),
        "cost_total": _g_str(form, "cost_total"),
        "discount_percent": _g_str(form, "discount_percent"),
        "blanks_condition": d.blanks_condition.value,
        "author_external": "on" if _g_bool(form, "author_external") else "",
        "kit_author_ids": parse_kit_author_user_ids_from_form(form),
    }
    try:
        for name in list(form.keys()):
            if isinstance(name, str) and name.startswith("kit_qty_"):
                out[name] = _g_str(form, name, "0")
    except Exception:
        pass
    return out


def kit_edit_error_prefill(form: Any) -> dict[str, Any]:
    d = parse_kit_admin_form(form, for_create=False)
    out: dict[str, Any] = {
        "sku": d.sku,
        "title": d.title,
        "blank_type_de": "on" if d.blank_type_de else "",
        "blank_type_se": "on" if d.blank_type_se else "",
        "pieces_total": d.pieces_total,
        "pieces_available": d.pieces_available,
        "weight_grams": _g_str(form, "weight_grams"),
        "length_cm": _g_str(form, "length_cm"),
        "materials_text": d.materials_text or "",
        "color_text": d.color_text or "",
        "description": d.description or "",
        "notes": d.notes or "",
        "stock_price_total": _g_str(form, "stock_price_total"),
        "cost_total": _g_str(form, "cost_total"),
        "discount_percent": _g_str(form, "discount_percent"),
        "blanks_condition": d.blanks_condition.value,
        "author_external": "on" if _g_bool(form, "author_external") else "",
        "kit_author_ids": parse_kit_author_user_ids_from_form(form),
    }
    try:
        for name in list(form.keys()):
            if isinstance(name, str) and name.startswith("blank_stock_qty__"):
                out[name] = _g_str(form, name)
    except Exception:
        pass
    return out


def kit_to_form_prefill(kit: Kit) -> dict[str, Any]:
    """Значения для шаблона формы редактирования (чекбоксы — 'on' или '')."""
    w = "" if kit.weight_grams is None else str(kit.weight_grams).replace(",", ".")
    ln = "" if kit.length_cm is None else str(kit.length_cm).replace(",", ".")
    sp = "" if kit.stock_price_total is None else str(kit.stock_price_total).replace(",", ".")
    ct = "" if kit.cost_total is None else str(kit.cost_total).replace(",", ".")
    disc = str(int(kit.discount_percent or 0))
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
        "materials_text": kit.materials_text or "",
        "color_text": kit.color_text or "",
        "description": kit.description or "",
        "notes": kit.notes or "",
        "stock_price_total": sp,
        "cost_total": ct,
        "discount_percent": disc,
        "blanks_condition": getattr(kit, "blanks_condition", KitBlanksCondition.NEW).value,
        "is_active": "on" if kit.is_active else "",
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
    kit.blanks_condition = d.blanks_condition
    kit.blank_type_de = d.blank_type_de
    kit.blank_type_se = d.blank_type_se
    kit.pieces_total = d.pieces_total
    kit.pieces_available = d.pieces_available
    kit.weight_grams = d.weight_grams
    kit.length_cm = d.length_cm
    kit.materials_text = d.materials_text
    kit.color_text = (d.color_text[:200] if d.color_text else None)
    kit.notes = d.notes
    kit.description = d.description
    kit.stock_price_total = d.stock_price_total
    kit.cost_total = d.cost_total
    kit.discount_percent = int(d.discount_percent or 0)
    kit.is_active = bool(d.is_active)
    kit.author_cost_total = None
    if kit.pieces_available <= 0:
        kit.is_in_stock = False
    else:
        kit.is_in_stock = True
