"""Массовый импорт карточек комплектов из JSON (суперадмин)."""

from __future__ import annotations

import json
from typing import Any, TypedDict

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Kit
from app.kit_blank_stock_core import (
    composition_keys_intersection_catalog,
    load_catalog_kit_maps,
    parse_composition_totals,
    replace_blank_stock_for_kit,
)
from app.kit_crud import (
    KitAdminFormData,
    apply_kit_admin_form,
    sync_kit_authors_from_user_ids,
    validate_kit_admin_form,
)

MAX_BULK_JSON_BYTES = 512 * 1024
MAX_BULK_KITS = 500


class BulkImportRowResult(TypedDict):
    input_sku: str
    saved_sku: str
    ok: bool
    kit_id: int | None
    message: str


def _sku_suffix(attempt: int) -> str:
    if attempt <= 0:
        return ""
    if attempt == 1:
        return "❗повтор"
    return f"❗повтор{attempt}"


def allocate_unique_kit_sku(db: Session, base_sku: str, reserved: set[str]) -> str:
    """Уникальный артикул в БД и среди `reserved` этой загрузки; длина ≤ 80."""
    base = (base_sku or "").strip()
    if not base:
        raise ValueError("Пустой артикул.")
    for attempt in range(0, 400):
        suf = _sku_suffix(attempt)
        max_stem = max(0, 80 - len(suf))
        stem = base[:max_stem] if max_stem else ""
        candidate = (stem + suf)[:80]
        if not candidate.strip():
            continue
        if candidate in reserved:
            continue
        oid = db.scalar(select(Kit.id).where(Kit.sku == candidate))
        if oid:
            continue
        return candidate
    raise ValueError("Не удалось подобрать уникальный артикул (слишком много коллизий).")


def parse_bulk_kits_json(raw: str) -> list[dict[str, Any]]:
    s = (raw or "").strip()
    if not s:
        raise ValueError("Введите JSON (массив объектов).")
    if len(s.encode("utf-8")) > MAX_BULK_JSON_BYTES:
        raise ValueError(f"Слишком большой файл (лимит {MAX_BULK_JSON_BYTES // 1024} KB).")
    try:
        data = json.loads(s)
    except json.JSONDecodeError as e:
        raise ValueError(f"Некорректный JSON: {e}") from None
    if not isinstance(data, list):
        raise ValueError("Корень JSON должен быть массивом комплектов.")
    if len(data) > MAX_BULK_KITS:
        raise ValueError(f"Слишком много строк (лимит {MAX_BULK_KITS}).")
    out: list[dict[str, Any]] = []
    for i, row in enumerate(data):
        if not isinstance(row, dict):
            raise ValueError(f"Строка {i + 1}: ожидался объект, не {type(row).__name__}.")
        out.append(row)
    return out


def _composition_dict_from_json(val: Any) -> dict[str, int] | None:
    if val is None:
        return None
    if isinstance(val, dict):
        totals: dict[str, int] = {}
        for k, v in val.items():
            kk = str(k).strip()
            if not kk:
                continue
            try:
                q = int(v)
            except (TypeError, ValueError):
                continue
            if q > 0:
                totals[kk] = totals.get(kk, 0) + q
        return totals or None
    if isinstance(val, list):
        totals = {}
        for it in val:
            if not isinstance(it, dict):
                continue
            kk = str(it.get("key") or "").strip()
            if not kk:
                continue
            try:
                q = int(it.get("qty") or 0)
            except (TypeError, ValueError):
                q = 0
            if q > 0:
                totals[kk] = totals.get(kk, 0) + q
        return totals or None
    raise ValueError("Поле composition должно быть объектом или массивом {key, qty}.")


def _blank_stock_dict(val: Any) -> dict[str, int]:
    if val is None:
        return {}
    if not isinstance(val, dict):
        raise ValueError("Поле blank_stock должно быть объектом {КЛЮЧ: количество}.")
    out: dict[str, int] = {}
    for k, v in val.items():
        kk = str(k).strip()
        if not kk:
            continue
        try:
            n = int(v)
        except (TypeError, ValueError):
            raise ValueError(f"Некорректное количество для ключа «{kk}».") from None
        if n < 0:
            raise ValueError(f"Отрицательный остаток для ключа «{kk}».")
        if n > 0:
            out[kk[:80]] = n
    return out


def _bool_at(row: dict[str, Any], key: str, default: bool = False) -> bool:
    if key not in row:
        return default
    v = row[key]
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def _int_at(row: dict[str, Any], key: str) -> int:
    v = row.get(key)
    if v is None or v == "":
        raise ValueError(f"Не задано обязательное поле «{key}».")
    try:
        return int(v)
    except (TypeError, ValueError):
        raise ValueError(f"Поле «{key}» должно быть целым числом.") from None


def _float_at(row: dict[str, Any], key: str) -> float | None:
    v = row.get(key)
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", ".")
    try:
        return float(s)
    except ValueError:
        raise ValueError(f"Поле «{key}» должно быть числом.") from None


def _str_opt(row: dict[str, Any], key: str) -> str | None:
    v = row.get(key)
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def row_to_kit_admin_data(row: dict[str, Any], *, saved_sku: str) -> KitAdminFormData:
    sku = saved_sku.strip()
    title = _str_opt(row, "title")
    if not title:
        raise ValueError("Укажите название (title).")
    pieces_initial = _int_at(row, "pieces_initial")
    if pieces_initial <= 0:
        raise ValueError("Количество заготовок (pieces_initial) должно быть больше 0.")
    sp = _float_at(row, "stock_price_total")
    ct = _float_at(row, "cost_total")
    disc_raw = row.get("discount_percent", 0)
    try:
        disc = int(disc_raw) if disc_raw != "" and disc_raw is not None else 0
    except (TypeError, ValueError):
        raise ValueError("Скидка (discount_percent) — целое число от 0 до 100.") from None
    return KitAdminFormData(
        sku=sku,
        title=title,
        blank_type_de=_bool_at(row, "blank_type_de"),
        blank_type_se=_bool_at(row, "blank_type_se"),
        pieces_total=max(0, pieces_initial),
        pieces_available=max(0, pieces_initial),
        weight_grams=_float_at(row, "weight_grams"),
        length_cm=_float_at(row, "length_cm"),
        has_decorations=_bool_at(row, "has_decorations", False),
        materials_text=_str_opt(row, "materials_text"),
        color_text=_str_opt(row, "color_text"),
        blanks_kinds_text=_str_opt(row, "blanks_kinds_text"),
        notes=_str_opt(row, "notes"),
        description=_str_opt(row, "description"),
        stock_price_total=sp,
        cost_total=ct,
        discount_percent=disc,
        composition_totals={},
    )


def _author_user_ids(row: dict[str, Any]) -> list[int]:
    raw = row.get("author_user_ids")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("author_user_ids должен быть массивом целых id.")
    out: list[int] = []
    for x in raw:
        try:
            i = int(x)
        except (TypeError, ValueError):
            raise ValueError("author_user_ids: каждый элемент — целое id.") from None
        if i > 0:
            out.append(i)
    return list(dict.fromkeys(out))


def import_single_kit_row(
    db: Session,
    row: dict[str, Any],
    *,
    reserved_skus: set[str],
    changed_by_user_id: int,
) -> BulkImportRowResult:
    input_sku = str(row.get("sku") or "").strip()
    if not input_sku:
        return {
            "input_sku": "",
            "saved_sku": "",
            "ok": False,
            "kit_id": None,
            "message": "Пустой артикул (sku).",
        }
    try:
        saved_sku = allocate_unique_kit_sku(db, input_sku, reserved_skus)
        d = row_to_kit_admin_data(row, saved_sku=saved_sku)
        validate_kit_admin_form(d, for_create=True)

        comp = _composition_dict_from_json(row.get("composition"))
        blank_qty = _blank_stock_dict(row.get("blank_stock"))
        pieces_initial = int(d.pieces_total)

        if comp and pieces_initial > 0:
            if not blank_qty:
                raise ValueError(
                    "Задан состав (composition), но не указан остаток по видам (blank_stock). "
                    "Заполните blank_stock так же, как колонки «Остаток, шт.» в карточке комплекта."
                )

        kit = Kit()
        apply_kit_admin_form(kit, d)
        if comp:
            kit.composition_json = json.dumps(comp, ensure_ascii=False, sort_keys=True)
        kit.updated_by_user_id = changed_by_user_id
        db.add(kit)
        db.flush()

        sync_kit_authors_from_user_ids(
            db,
            kit,
            author_user_ids=_author_user_ids(row),
            author_external=_bool_at(row, "author_external", False),
        )

        if blank_qty:
            comp2 = parse_composition_totals(kit)
            _, meta, _ = load_catalog_kit_maps(db)
            allowed = set(composition_keys_intersection_catalog(comp2, meta)) if comp2 else set()
            if not allowed and comp2:
                allowed = set(comp2.keys())
            if not allowed:
                raise ValueError("Нет ключей для остатков по видам (проверьте composition и каталог).")
            replace_blank_stock_for_kit(db, kit, quantities=blank_qty, allowed_keys=allowed)

        db.commit()
        reserved_skus.add(saved_sku)
        msg = "Создан."
        if saved_sku != input_sku:
            msg = f"Создан; артикул изменён из-за коллизии: «{saved_sku}»."
        return {
            "input_sku": input_sku,
            "saved_sku": saved_sku,
            "ok": True,
            "kit_id": int(kit.id),
            "message": msg,
        }
    except ValueError as e:
        db.rollback()
        return {
            "input_sku": input_sku,
            "saved_sku": "",
            "ok": False,
            "kit_id": None,
            "message": str(e),
        }
    except Exception as e:
        db.rollback()
        return {
            "input_sku": input_sku,
            "saved_sku": "",
            "ok": False,
            "kit_id": None,
            "message": f"Ошибка: {e}",
        }
