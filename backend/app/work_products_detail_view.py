"""Контекст для карточки «Работа с товарами — детали»."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import CatalogProduct, WorkForInventory, WorkKind, WorkScope
from app.zakaz_blanks import kit_composition_catalog_items, zakaz_blank_def_by_key

_RUBBER_TYPE_LABELS: dict[str, str] = {
    "TAIL_ELASTIC": "Хвост на резинке (1 крепление)",
    "TAIL_CRAB_MINI": "Хвост на крабе — mini",
    "TAIL_CRAB_STANDARD": "Хвост на крабе — standard",
    "TAIL_CRAB_MAX": "Хвост на крабе — max",
    "TAIL_NET_MINI": "Хвост на сетке — mini",
    "TAIL_NET_STANDARD": "Хвост на сетке — standard",
    "TAIL_NET_MAX": "Хвост на сетке — max",
    "TAIL_BUN_MINI": "Хвост на бублике — mini",
    "TAIL_BUN_STANDARD": "Хвост на бублике — standard",
    "TAIL_BUN_MAX": "Хвост на бублике — max",
    "BRAIDS_ELASTIC": "Косы на резинке",
}


def blank_key_label(db: Session | None, key: str) -> str:
    k = (key or "").strip()
    if not k:
        return "—"
    if db is not None:
        for item in kit_composition_catalog_items(db):
            if item.get("key") == k:
                return str(item.get("label") or k)
    z = zakaz_blank_def_by_key().get(k)
    return z.display_name if z else k


def blank_condition_label(condition: str | None) -> str:
    s = (condition or "NEW").strip().upper()
    if s == "USED":
        return "б/у"
    return "новая"


def rubber_type_label(rubber_type: str | None) -> str:
    rt = (rubber_type or "").strip()
    return _RUBBER_TYPE_LABELS.get(rt, rt or "—")


def build_composition_table_view(
    db: Session,
    *,
    lines: list[dict[str, Any]] | None,
    staff_rows: list[Any],
) -> dict[str, Any] | None:
    """Таблица состава: виды заготовок × мастера + итого."""
    if not lines:
        return None

    staff_columns: list[dict[str, Any]] = []
    staff_ids_ordered: list[int] = []
    for s in staff_rows or []:
        uid = int(s.user_id)
        if uid not in staff_ids_ordered:
            staff_ids_ordered.append(uid)
            staff_columns.append(
                {
                    "id": uid,
                    "name": s.user.display_name if s.user else str(uid),
                }
            )

    extra_staff_ids: set[int] = set()
    for ln in lines:
        by_staff = ln.get("by_staff") or {}
        for sid in by_staff:
            try:
                extra_staff_ids.add(int(sid))
            except (TypeError, ValueError):
                continue
    for uid in sorted(extra_staff_ids):
        if uid not in staff_ids_ordered:
            staff_ids_ordered.append(uid)
            staff_columns.append({"id": uid, "name": f"Мастер #{uid}"})

    rows: list[dict[str, Any]] = []
    grand_total = 0
    for ln in lines:
        key = str(ln.get("key") or "").strip()
        if not key:
            continue
        by_staff_raw = ln.get("by_staff") or {}
        qty_by_staff: dict[int, int] = {}
        for sid, q in by_staff_raw.items():
            try:
                iq = int(q)
            except (TypeError, ValueError):
                iq = 0
            if iq > 0:
                qty_by_staff[int(sid)] = iq
        total = sum(qty_by_staff.values())
        if total <= 0:
            total = int(ln.get("total") or 0)
        if total <= 0:
            continue
        grand_total += total
        cond = str(ln.get("condition") or "NEW")
        used_pct = ln.get("used_price_pct")
        rows.append(
            {
                "label": blank_key_label(db, key),
                "key": key,
                "condition": cond,
                "condition_label": blank_condition_label(cond),
                "used_price_pct": int(used_pct) if used_pct is not None else None,
                "qty_by_staff": qty_by_staff,
                "total": total,
            }
        )

    if not rows:
        return None

    return {
        "staff_columns": staff_columns,
        "rows": rows,
        "grand_total": grand_total,
    }


def catalog_product_name(db: Session, product_id: int | None) -> str | None:
    if not product_id:
        return None
    row = db.get(CatalogProduct, int(product_id))
    return (row.name or "").strip() if row else None


def work_profit_explanation(work: WorkForInventory, details: dict[str, Any]) -> list[str]:
    """Пояснение расчёта профита (снимок на момент записи)."""
    kind = work.kind
    scope = work.scope
    share = float(work.studio_share_snapshot or 0.0)
    share_pct = int(round(share * 100))

    if kind == WorkKind.KIT:
        kit = details.get("kit") or {}
        catalog_price = kit.get("catalog_client_price")
        lines = [
            "Цена — сумма полей «Цена» в прайсе «Заготовки поштучно» по составу (+ доп. расходы в цене).",
            "ЗП мастера — сумма полей «Работа» (master_pay) по заготовкам + оплата смешки, если «сама мешала».",
            "Себестоимость — материал + доп. расходы.",
            "ЗП студии = цена − себестоимость − ЗП мастера (не по проценту studio_share).",
        ]
        if work.scope == WorkScope.CUSTOM_ORDER and work.amount_from_client is not None:
            lines.append(
                f"На заказ указана своя сумма с клиента ({work.amount_from_client} ₽) — "
                "она используется как цена вместо расчёта по прайсу."
            )
        elif catalog_price is not None:
            lines.append(f"Расчётная цена по прайсу на момент записи: {catalog_price} ₽.")
        if work.scope == WorkScope.IN_STOCK:
            lines.append(
                "В режиме «в наличие» студия на этапе производства долю не получает — "
                "маржа учитывается при продаже комплекта."
            )
        return lines

    if kind == WorkKind.RUBBER:
        return [
            "Мастер и студия — по прайсу «Хвосты/резинки» (поля master_pay и studio_pay × количество).",
            "На заказ применяется множитель бонуса за индивидуальный заказ, если он задан в настройках.",
        ]

    if kind == WorkKind.OTHER:
        return [
            "Мастер и студия — по выбранной позиции прайса (master_pay и studio_pay).",
            "На заказ применяется множитель бонуса за индивидуальный заказ, если он задан в настройках.",
        ]

    if kind == WorkKind.KIT_CORRECTION:
        corr = details.get("correction") or {}
        if corr.get("use_custom_amount"):
            return [
                "Указана своя сумма с клиента: прибыль = сумма − себестоимость, "
                "далее деление по доле салона (как в визите).",
                "ЗП мастера — остаток после доли студии.",
            ]
        return [
            "Мастер и студия — по прайсу «Коррекция комплекта» "
            "(сумма master_pay и studio_pay по отмеченным операциям).",
            "На заказ применяется множитель бонуса за индивидуальный заказ, если он задан в настройках.",
        ]

    if kind == WorkKind.MIX:
        return [
            "ЗП мастера — граммы материала × ставка смешки по выбранной сложности.",
            "Студия на этапе смешки долю не получает.",
        ]

    return ["Профит зафиксирован снимком на момент сохранения записи."]
