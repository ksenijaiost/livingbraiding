"""Состав комплекта v2: строки с видом, NEW/USED, количеством по мастерам."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import CatalogProduct, KitBlanksCondition
from app.kit_composition import KIT_INVENTORY_PIECE_EXCLUDE_KEYS
from app.kit_crud import kit_key_excluded_from_client_price
from app.kit_blank_stock_core import load_catalog_kit_maps
from app.zakaz_blanks import zakaz_blank_def_by_key

def _line_key_re(prefix: str) -> re.Pattern[str]:
    return re.compile(rf"^{re.escape(prefix)}_(\d+)_")


def _line_qty_re(prefix: str) -> re.Pattern[str]:
    return re.compile(rf"^{re.escape(prefix)}_(\d+)_qty_(\d+)$")


class BlankCondition(str, Enum):
    NEW = "NEW"
    USED = "USED"


@dataclass
class CompositionLine:
    key: str
    condition: BlankCondition = BlankCondition.NEW
    used_price_pct: int = 100
    used_price_pct_explicit: bool = False
    by_staff: dict[int, int] = field(default_factory=dict)

    def total_qty(self) -> int:
        return sum(max(0, int(v)) for v in self.by_staff.values())

    def line_qty_for_master(self, master_id: int) -> int:
        return max(0, int(self.by_staff.get(int(master_id), 0)))


def line_is_empty(line: CompositionLine) -> bool:
    if not (line.key or "").strip():
        return True
    return line.total_qty() <= 0


def filter_nonempty(lines: list[CompositionLine]) -> list[CompositionLine]:
    return [ln for ln in lines if not line_is_empty(ln)]


def _normalize_condition(raw: str | None) -> BlankCondition:
    s = (raw or "NEW").strip().upper()
    if s in ("USED", "BU", "Б/У", "ON", "1", "TRUE"):
        return BlankCondition.USED
    return BlankCondition.NEW


def _legacy_key_to_base(key: str) -> tuple[str, BlankCondition | None]:
    """SE_BRAID_USED → (SE_BRAID, USED) если ключ заканчивается на _USED."""
    k = (key or "").strip()
    if k.endswith("_USED"):
        base = k[: -len("_USED")]
        if base:
            return base, BlankCondition.USED
    return k, None


def lines_from_json(
    raw: str | None,
    *,
    default_used_price_pct: int | None = None,
) -> list[CompositionLine]:
    if not raw:
        return []
    try:
        payload = json.loads(str(raw))
    except Exception:
        return []

    if isinstance(payload, list):
        out: list[CompositionLine] = []
        for it in payload:
            if not isinstance(it, dict):
                continue
            if "by_staff" in it or "condition" in it:
                k = str(it.get("key") or "").strip()
                if not k:
                    continue
                cond = _normalize_condition(str(it.get("condition") or "NEW"))
                pct_explicit = "used_price_pct" in it and str(it.get("used_price_pct") or "").strip() != ""
                pct = 100
                if pct_explicit:
                    try:
                        pct = int(it.get("used_price_pct") or 100)
                    except (TypeError, ValueError):
                        pct = 100
                    pct = max(1, min(100, pct))
                elif cond == BlankCondition.USED and default_used_price_pct is not None:
                    pct = max(1, min(100, int(default_used_price_pct)))
                elif cond == BlankCondition.USED:
                    pct = 100
                by_staff: dict[int, int] = {}
                bs = it.get("by_staff")
                if isinstance(bs, dict):
                    for uid, q in bs.items():
                        try:
                            qi = int(q)
                        except (TypeError, ValueError):
                            qi = 0
                        if qi > 0:
                            by_staff[int(uid)] = qi
                elif "qty" in it:
                    try:
                        q = int(it.get("qty") or 0)
                    except (TypeError, ValueError):
                        q = 0
                    if q > 0:
                        by_staff[0] = q
                if by_staff or k:
                    out.append(
                        CompositionLine(
                            key=k,
                            condition=cond,
                            used_price_pct=pct,
                            used_price_pct_explicit=pct_explicit,
                            by_staff=by_staff,
                        )
                    )
                continue
            k = str(it.get("key") or "").strip()
            if not k:
                continue
            try:
                q = int(it.get("qty") or 0)
            except (TypeError, ValueError):
                q = 0
            if q <= 0:
                continue
            base, leg_cond = _legacy_key_to_base(k)
            cond = leg_cond or BlankCondition.NEW
            use_k = base if leg_cond else k
            out.append(
                CompositionLine(
                    key=use_k,
                    condition=cond,
                    used_price_pct=100,
                    by_staff={0: q},
                )
            )
        return filter_nonempty(out)

    if isinstance(payload, dict):
        lines: list[CompositionLine] = []
        for k, v in payload.items():
            try:
                q = int(v)
            except (TypeError, ValueError):
                continue
            if q <= 0:
                continue
            base, leg_cond = _legacy_key_to_base(str(k))
            lines.append(
                CompositionLine(
                    key=base if leg_cond else str(k),
                    condition=leg_cond or BlankCondition.NEW,
                    used_price_pct=100,
                    by_staff={0: q},
                )
            )
        return filter_nonempty(lines)
    return []


def lines_to_json(lines: list[CompositionLine]) -> str | None:
    items: list[dict[str, Any]] = []
    for ln in filter_nonempty(lines):
        by_staff = {int(uid): int(q) for uid, q in ln.by_staff.items() if int(q) > 0}
        if not by_staff:
            continue
        row: dict[str, Any] = {
            "key": ln.key,
            "condition": ln.condition.value,
        }
        if len(by_staff) == 1 and 0 in by_staff:
            row["qty"] = int(by_staff[0])
        else:
            row["by_staff"] = {str(uid): int(q) for uid, q in by_staff.items()}
        if ln.condition == BlankCondition.USED and ln.used_price_pct_explicit:
            row["used_price_pct"] = int(ln.used_price_pct)
        items.append(row)
    return json.dumps(items, ensure_ascii=False) if items else None


def lines_to_legacy_totals(lines: list[CompositionLine]) -> dict[str, int]:
    """Агрегат qty по key (NEW+USED) для совместимости."""
    totals: dict[str, int] = {}
    for ln in filter_nonempty(lines):
        totals[ln.key] = totals.get(ln.key, 0) + ln.total_qty()
    return {k: int(v) for k, v in totals.items() if int(v) > 0}


def inventory_totals_by_key(lines: list[CompositionLine]) -> dict[str, int]:
    totals = lines_to_legacy_totals(lines)
    return {
        k: int(v)
        for k, v in totals.items()
        if int(v) > 0 and k not in KIT_INVENTORY_PIECE_EXCLUDE_KEYS
    }


def inventory_piece_count(lines: list[CompositionLine]) -> int:
    return sum(inventory_totals_by_key(lines).values())


def infer_blanks_condition(lines: list[CompositionLine]) -> KitBlanksCondition:
    has_new = False
    has_used = False
    for ln in filter_nonempty(lines):
        if ln.condition == BlankCondition.USED:
            has_used = True
        else:
            has_new = True
    if has_new and has_used:
        return KitBlanksCondition.MIXED
    if has_used:
        return KitBlanksCondition.USED
    return KitBlanksCondition.NEW


def _price_for_line_unit(
    db: Session,
    line: CompositionLine,
    *,
    price_map: dict[str, float],
    meta_by_key: dict[str, dict[str, Any]],
) -> float | None:
    if kit_key_excluded_from_client_price(meta_by_key.get(line.key) or {}, line.key):
        return None
    p = price_map.get(line.key)
    if p is None:
        z = zakaz_blank_def_by_key().get(line.key)
        if z and not z.ignore_in_client_calc:
            p = float(z.price)
    if p is None:
        return None
    if line.condition == BlankCondition.USED:
        pct = max(1, min(100, int(line.used_price_pct or 100)))
        return float(p) * (float(pct) / 100.0)
    return float(p)


def lines_as_new_for_pricing(lines: list[CompositionLine]) -> list[CompositionLine]:
    """Копии строк с NEW и 100% — расчёт «как новые»."""
    out: list[CompositionLine] = []
    for ln in filter_nonempty(lines):
        out.append(
            CompositionLine(
                key=ln.key,
                condition=BlankCondition.NEW,
                used_price_pct=100,
                by_staff=dict(ln.by_staff),
            )
        )
    return out


def apply_global_used_discount(
    lines: list[CompositionLine], discount_pct: int
) -> list[CompositionLine]:
    pct = max(1, min(100, int(discount_pct)))
    out: list[CompositionLine] = []
    for ln in filter_nonempty(lines):
        out.append(
            CompositionLine(
                key=ln.key,
                condition=BlankCondition.USED,
                used_price_pct=pct,
                by_staff=dict(ln.by_staff),
            )
        )
    return out


def client_price_new_equivalent(
    db: Session,
    lines: list[CompositionLine],
    *,
    extra_costs_amount: float = 0.0,
) -> tuple[float, list[str]]:
    return client_price_for_lines(
        db, lines_as_new_for_pricing(lines), extra_costs_amount=extra_costs_amount
    )


def stock_price_for_used_kit(
    db: Session,
    lines: list[CompositionLine],
    discount_pct: int,
    *,
    extra_costs_amount: float = 0.0,
) -> tuple[float, float, list[str]]:
    """(цена_как_новые, итог_на_склад, missing_keys)."""
    new_total, missing = client_price_new_equivalent(
        db, lines, extra_costs_amount=extra_costs_amount
    )
    if missing:
        return 0.0, 0.0, missing
    pct = max(1, min(100, int(discount_pct)))
    stock_total = float(new_total) * (float(pct) / 100.0)
    return float(new_total), float(stock_total), []


def stock_price_snapshot_for_used_kit(
    db: Session,
    lines: list[CompositionLine],
    *,
    discount_pct: int,
    extra_costs_amount: float = 0.0,
) -> str:
    """Текстовый снимок: позиции как новые, скидка Б/У, итог на склад."""
    price_map, meta_by_key, _labels = load_catalog_kit_maps(db)
    from app.zakaz_blanks import zakaz_blank_def_by_key as _zbd

    new_lines = lines_as_new_for_pricing(lines)
    lines_out = ["Расчёт цены комплекта (б/у на склад):"]
    subtotal = 0.0
    for ln in new_lines:
        q = ln.total_qty()
        if q <= 0:
            continue
        if kit_key_excluded_from_client_price(meta_by_key.get(ln.key) or {}, ln.key):
            continue
        p = price_map.get(ln.key)
        if p is None:
            z = _zbd().get(ln.key)
            if z and not z.ignore_in_client_calc:
                p = float(z.price)
        if p is None:
            continue
        line_total = float(p) * float(q)
        subtotal += line_total
        lbl = (meta_by_key.get(ln.key) or {}).get("name") or ln.key
        lines_out.append(
            f"{lbl} — {q} шт × {float(p):.2f} ₽ = {line_total:.2f} ₽"
        )
    if extra_costs_amount > 0:
        subtotal += float(extra_costs_amount)
        lines_out.append(f"Доп. расходы — {float(extra_costs_amount):.2f} ₽")
    lines_out.append(f"Цена как новые — {subtotal:.2f} ₽")
    pct = max(1, min(100, int(discount_pct)))
    stock_total = subtotal * (float(pct) / 100.0)
    lines_out.append(f"Скидка за Б/У — {pct}%")
    lines_out.append(f"Итоговая цена на склад — {stock_total:.2f} ₽")
    return "\n".join(lines_out)


def client_price_for_lines(
    db: Session,
    lines: list[CompositionLine],
    *,
    extra_costs_amount: float = 0.0,
) -> tuple[float, list[str]]:
    price_map, meta_by_key, _labels = load_catalog_kit_maps(db)
    missing: list[str] = []
    total = 0.0
    for ln in filter_nonempty(lines):
        unit = _price_for_line_unit(db, ln, price_map=price_map, meta_by_key=meta_by_key)
        q = ln.total_qty()
        if q <= 0:
            continue
        if unit is None:
            if not kit_key_excluded_from_client_price(meta_by_key.get(ln.key) or {}, ln.key):
                missing.append(ln.key)
            continue
        total += float(unit) * float(q)
    if missing:
        return 0.0, sorted(set(missing))
    return float(total) + float(max(0.0, extra_costs_amount)), []


def used_client_total_for_lines(db: Session, lines: list[CompositionLine]) -> float:
    price_map, meta_by_key, _labels = load_catalog_kit_maps(db)
    total = 0.0
    for ln in filter_nonempty(lines):
        if ln.condition != BlankCondition.USED:
            continue
        unit = _price_for_line_unit(db, ln, price_map=price_map, meta_by_key=meta_by_key)
        if unit is None:
            continue
        total += float(unit) * float(ln.total_qty())
    return float(total)


def _work_pay_for_key(db: Session, item_key: str) -> float:
    rows = list(
        db.scalars(
            select(CatalogProduct).where(
                CatalogProduct.category_name == "Заказ",
                CatalogProduct.subcategory_name == "Заготовки поштучно",
                CatalogProduct.is_active.is_(True),
            )
        ).all()
    )
    for r in rows:
        try:
            meta = json.loads(r.meta_json or "{}")
        except Exception:
            meta = {}
        if not isinstance(meta, dict):
            continue
        k = str(meta.get("kit_key") or "").strip()
        if k == item_key and not bool(meta.get("is_bu")):
            return float(meta.get("master_pay") or 0.0)
    z = zakaz_blank_def_by_key().get(item_key)
    if z:
        return float(z.work_pay)
    return 0.0


def work_pay_for_lines(db: Session, lines: list[CompositionLine]) -> dict[int, float]:
    """ЗП по мастерам: только NEW-строки."""
    out: dict[int, float] = {}
    for ln in filter_nonempty(lines):
        if ln.condition != BlankCondition.NEW:
            continue
        rate = _work_pay_for_key(db, ln.key)
        if rate <= 0:
            continue
        for uid, q in ln.by_staff.items():
            qi = int(q)
            if qi <= 0:
                continue
            out[int(uid)] = out.get(int(uid), 0.0) + rate * float(qi)
    return out


def lines_from_form(form: Any, *, prefix: str = "kit_line") -> list[CompositionLine]:
    key_re = _line_key_re(prefix)
    qty_re = _line_qty_re(prefix)
    indices: set[int] = set()
    for key in form.keys():
        m = key_re.match(str(key))
        if m:
            indices.add(int(m.group(1)))
    lines: list[CompositionLine] = []
    for i in sorted(indices):

        def g(name: str, default: str = "") -> str:
            raw = form.get(f"{prefix}_{i}_{name}")
            if raw is None:
                return default
            if hasattr(raw, "strip"):
                return str(raw).strip()
            return str(raw).decode() if isinstance(raw, (bytes, bytearray)) else str(raw).strip()

        key = g("key")
        is_used = g("is_used") in ("on", "1", "true", "yes") or g("condition") == "USED"
        cond = BlankCondition.USED if is_used else BlankCondition.NEW
        try:
            pct = int(g("used_pct", "100") or "100")
        except ValueError:
            pct = 100
        pct = max(1, min(100, pct))
        by_staff: dict[int, int] = {}
        for fk in form.keys():
            qm = qty_re.match(str(fk))
            if not qm or int(qm.group(1)) != i:
                continue
            mid = int(qm.group(2))
            raw = form.get(fk)
            s = raw.decode() if isinstance(raw, (bytes, bytearray)) else str(raw or "0").strip()
            try:
                q = int(s)
            except ValueError:
                q = 0
            if q > 0:
                by_staff[mid] = q
        lines.append(
            CompositionLine(
                key=key,
                condition=cond,
                used_price_pct=pct,
                by_staff=by_staff,
            )
        )
    return filter_nonempty(lines)


def lines_have_used(lines: list[CompositionLine]) -> bool:
    return any(ln.condition == BlankCondition.USED for ln in filter_nonempty(lines))


def kit_by_staff_from_lines(lines: list[CompositionLine]) -> tuple[dict[str, int], dict[int, dict[str, int]]]:
    """totals по key и by_staff как в details_json.kit (legacy totals)."""
    totals: dict[str, int] = {}
    by_staff: dict[int, dict[str, int]] = {}
    for ln in filter_nonempty(lines):
        totals[ln.key] = totals.get(ln.key, 0) + ln.total_qty()
        for uid, q in ln.by_staff.items():
            if int(q) <= 0:
                continue
            by_staff.setdefault(int(uid), {})
            by_staff[int(uid)][ln.key] = by_staff[int(uid)].get(ln.key, 0) + int(q)
    return totals, by_staff


def composition_has_v2_lines(raw: str | None) -> bool:
    if not raw:
        return False
    try:
        payload = json.loads(str(raw))
    except Exception:
        return False
    if not isinstance(payload, list):
        return False
    for it in payload:
        if isinstance(it, dict) and ("by_staff" in it or "condition" in it):
            return True
    return False


def unit_client_price_for_key(
    db: Session,
    lines: list[CompositionLine],
    key: str,
    *,
    price_map: dict[str, float],
    meta_by_key: dict[str, dict[str, Any]],
) -> float | None:
    matching = [ln for ln in filter_nonempty(lines) if ln.key == key]
    if not matching:
        p = price_map.get(key)
        return float(p) if p is not None else None
    total_q = sum(ln.total_qty() for ln in matching)
    if total_q <= 0:
        return None
    acc = 0.0
    for ln in matching:
        unit = _price_for_line_unit(db, ln, price_map=price_map, meta_by_key=meta_by_key)
        if unit is None:
            continue
        acc += float(unit) * float(ln.total_qty())
    return acc / float(total_q) if total_q > 0 else None


def keyed_client_price_selected_v2(
    db: Session,
    kit_raw_json: str | None,
    breakdown: dict[str, int],
    *,
    price_map: dict[str, float],
    meta_by_key: dict[str, dict[str, Any]],
) -> float:
    lines = lines_from_json(kit_raw_json or "")
    total = 0.0
    for k, n in breakdown.items():
        ni = int(n)
        if ni <= 0:
            continue
        if kit_key_excluded_from_client_price(meta_by_key.get(k) or {}, k):
            continue
        unit = unit_client_price_for_key(
            db, lines, k, price_map=price_map, meta_by_key=meta_by_key
        )
        if unit is None:
            raise ValueError(f"Нет цены в каталоге для ключа «{k}» (заготовки поштучно).")
        total += float(unit) * float(ni)
    return float(total)


def lines_dicts_for_details(lines: list[CompositionLine]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for ln in filter_nonempty(lines):
        out.append(
            {
                "key": ln.key,
                "condition": ln.condition.value,
                "used_price_pct": int(ln.used_price_pct) if ln.condition == BlankCondition.USED else None,
                "by_staff": {str(int(u)): int(q) for u, q in ln.by_staff.items() if int(q) > 0},
            }
        )
    return out
