"""
Остатки комплекта по ключам состава (composition_json / kit_key в каталоге «Заготовки поштучно»).

Пока в БД нет строк kit_blank_stock для комплекта — используется прежняя скалярная логика
(pieces_available). После появления хотя бы одной строки включается «ключевой» режим:
движение остатка и резервы ведутся по kit_key, pieces_available синхронизируется как sum(qty).
"""

from __future__ import annotations

import json
import math
from typing import Any

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session, selectinload
from starlette.datastructures import UploadFile

from app.db.models import CatalogProduct, Kit, KitBlankStock, KitBlanksCondition, KitReserve
from app.kit_composition import KIT_INVENTORY_PIECE_EXCLUDE_KEYS
from app.kit_crud import kit_key_excluded_from_client_price
from app.time_utils import utcnow_naive


def parse_composition_totals(kit: Kit) -> dict[str, int]:
    """Количества по ключу из composition_json (v2 строки или legacy)."""
    from app.kit_composition_lines import lines_from_json, lines_to_legacy_totals

    raw = getattr(kit, "composition_json", None)
    if not raw:
        return {}
    return lines_to_legacy_totals(lines_from_json(str(raw)))


def load_catalog_kit_maps(
    db: Session,
) -> tuple[dict[str, float], dict[str, dict[str, Any]], dict[str, str]]:
    """Прайс и meta по kit_key из каталога «Заказ» → «Заготовки поштучно»."""
    rows = list(
        db.scalars(
            select(CatalogProduct).where(
                CatalogProduct.category_name == "Заказ",
                CatalogProduct.subcategory_name == "Заготовки поштучно",
                CatalogProduct.is_active.is_(True),
            )
        ).all()
    )
    price_map: dict[str, float] = {}
    meta_by_key: dict[str, dict[str, Any]] = {}
    label_by_key: dict[str, str] = {}
    for r in rows:
        try:
            meta = json.loads(r.meta_json or "{}")
        except Exception:
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        k = (meta.get("kit_key") or "").strip()
        if not k or r.price is None:
            continue
        price_map[k] = float(r.price)
        meta_by_key[k] = meta
        label_by_key[k] = (r.name or k).strip() or k
    return price_map, meta_by_key, label_by_key


def catalog_kit_key_hint_rows(db: Session) -> list[dict[str, str]]:
    """Список {key, label} для подсказок (импорт комплектов и т.п.): только строки каталога с kit_key и ценой."""
    _, _, label_by_key = load_catalog_kit_maps(db)
    return [{"key": k, "label": v} for k, v in sorted(label_by_key.items(), key=lambda kv: kv[0])]


def composition_keys_intersection_catalog(
    comp: dict[str, int], meta_by_key: dict[str, dict[str, Any]]
) -> list[str]:
    """Ключи состава, по которым можно вести склад (есть в составе и в каталоге с kit_key)."""
    out: list[str] = []
    for k in sorted(comp.keys()):
        if k in meta_by_key:
            out.append(k)
    return out


def inventory_qty_by_key_from_kit(kit: Kit) -> dict[str, int]:
    """Количество заготовок по ключу из состава (без стрижек)."""
    comp = parse_composition_totals(kit)
    return {
        str(k): int(v)
        for k, v in (comp or {}).items()
        if int(v) > 0 and str(k) not in KIT_INVENTORY_PIECE_EXCLUDE_KEYS
    }


def infer_stock_remainder_mode(db: Session, kit: Kit) -> str:
    """all — остаток совпадает с составом; choose — задан отдельно по видам."""
    inv = inventory_qty_by_key_from_kit(kit)
    if kit.id is not None and kit_inventory_is_keyed(db, int(kit.id)):
        sm = {k: int(v) for k, v in blank_stock_qty_map(db, int(kit.id)).items() if int(v) > 0}
        inv_pos = {k: int(v) for k, v in inv.items() if int(v) > 0}
        if sm == inv_pos:
            return "all"
        return "choose"
    if int(kit.pieces_available or 0) == int(kit.pieces_total or 0):
        return "all"
    return "choose"


def apply_kit_admin_stock_remainder(
    db: Session,
    kit: Kit,
    *,
    mode: str,
    blank_qty: dict[str, int],
) -> None:
    """Записать остаток: весь состав или выбранные количества по видам."""
    inv = inventory_qty_by_key_from_kit(kit)
    if not inv:
        raise ValueError("Нет ключей состава для остатков по видам (заполните таблицу состава).")
    allowed = set(inv.keys())
    mode_n = (mode or "all").strip().lower()
    if mode_n != "choose":
        replace_blank_stock_for_kit(db, kit, quantities=inv, allowed_keys=allowed)
        return
    posted: dict[str, int] = {k: 0 for k in allowed}
    for k, v in (blank_qty or {}).items():
        if k in allowed:
            posted[k] = max(0, int(v))
    replace_blank_stock_for_kit(db, kit, quantities=posted, allowed_keys=allowed)


def blank_stock_edit_rows_for_kit(db: Session, kit: Kit) -> list[dict[str, Any]]:
    """Строки для таблицы «остаток по видам» в форме редактирования."""
    inv = inventory_qty_by_key_from_kit(kit)
    price_map, _, label_by_key = load_catalog_kit_maps(db)
    keys = sorted(inv.keys())
    sm = blank_stock_qty_map(db, int(kit.id)) if kit.id is not None else {}
    if kit.id is not None and inv and not kit_inventory_is_keyed(db, int(kit.id)):
        sm = distribute_scalar_to_keys(inv, int(kit.pieces_available or 0))
    rows: list[dict[str, Any]] = []
    for k in keys:
        rows.append(
            {
                "key": k,
                "qty": int(sm.get(k, 0)),
                "label": label_by_key.get(k, k),
                "price": price_map.get(k),
            }
        )
    return rows


def kit_inventory_is_keyed(db: Session, kit_id: int) -> bool:
    n = db.scalar(
        select(func.count()).select_from(KitBlankStock).where(KitBlankStock.kit_id == int(kit_id))
    )
    return int(n or 0) > 0


def blank_stock_qty_map(db: Session, kit_id: int) -> dict[str, int]:
    rows = list(db.scalars(select(KitBlankStock).where(KitBlankStock.kit_id == int(kit_id))).all())
    return {str(r.kit_key): int(r.qty or 0) for r in rows}


def sync_kit_pieces_available_from_blank_lines(db: Session, kit: Kit) -> None:
    """Выставить pieces_available = сумма kit_blank_stock.

    Сессия приложения с autoflush=False: без flush SUM(qty) читает старые значения
    из БД и откатывает остаток на карточке после списания.
    """
    if kit.id is None:
        return
    db.flush()
    if not kit_inventory_is_keyed(db, int(kit.id)):
        return
    total = sum(int(v) for v in blank_stock_qty_map(db, int(kit.id)).values())
    kit.pieces_available = total
    kit.is_in_stock = total > 0


def _get_or_create_blank_line(db: Session, kit_id: int, kit_key: str) -> KitBlankStock:
    row = db.scalar(
        select(KitBlankStock).where(KitBlankStock.kit_id == int(kit_id), KitBlankStock.kit_key == kit_key)
    )
    if row:
        return row
    row = KitBlankStock(kit_id=int(kit_id), kit_key=kit_key, qty=0)
    db.add(row)
    db.flush()
    return row


def distribute_integer_by_weights(weights: dict[str, int], total: int) -> dict[str, int]:
    """Распределить целое total по ключам пропорционально весам (остаток — по убыванию веса)."""
    if total <= 0:
        return {k: 0 for k in weights}
    s = sum(max(0, int(v)) for v in weights.values())
    if s <= 0:
        keys = sorted(weights.keys())
        if not keys or total <= 0:
            return {k: 0 for k in weights}
        base, rem = divmod(total, len(keys))
        out = {k: base for k in keys}
        for i, k in enumerate(keys):
            if i < rem:
                out[k] += 1
        return out
    raw: dict[str, float] = {}
    for k, w in weights.items():
        raw[k] = total * max(0, int(w)) / float(s)
    floored = {k: int(math.floor(raw[k])) for k in raw}
    assigned = sum(floored.values())
    rem = total - assigned
    order = sorted(raw.keys(), key=lambda x: (-(raw[x] - floored[x]), x))
    out = dict(floored)
    i = 0
    while rem > 0 and order:
        out[order[i % len(order)]] += 1
        rem -= 1
        i += 1
    return out


def distribute_scalar_to_keys(comp: dict[str, int], total: int) -> dict[str, int]:
    """Сколько штук на каждый ключ при добавлении total «безтиповых» заготовок."""
    return distribute_integer_by_weights(comp, total)


def increment_blank_stock_keys(db: Session, kit_id: int, deltas: dict[str, int]) -> None:
    for k, dq in deltas.items():
        n = int(dq)
        if n == 0:
            continue
        line = _get_or_create_blank_line(db, kit_id, str(k))
        line.qty = int(line.qty or 0) + n


def decrement_blank_stock_keys(db: Session, kit_id: int, dec: dict[str, int]) -> None:
    for k, dq in dec.items():
        n = int(dq)
        if n <= 0:
            continue
        line = db.scalar(
            select(KitBlankStock).where(KitBlankStock.kit_id == int(kit_id), KitBlankStock.kit_key == str(k))
        )
        if not line:
            raise ValueError(f"Нет строки склада для ключа «{k}».")
        new_q = int(line.qty or 0) - n
        if new_q < 0:
            raise ValueError(f"Недостаточно остатка по ключу «{k}».")
        line.qty = new_q


def sum_reserved_by_key_for_client(db: Session, *, kit_id: int, client_id: int) -> dict[str, int]:
    out: dict[str, int] = {}
    rows = list(
        db.scalars(
            select(KitReserve).where(
                KitReserve.kit_id == int(kit_id),
                KitReserve.reserved_for_client_id == int(client_id),
            )
        ).all()
    )
    for r in rows:
        key = (r.kit_key or "").strip() or "__NULL__"
        out[key] = out.get(key, 0) + int(r.pieces_reserved or 0)
    return out


def max_take_by_key_for_client(
    db: Session,
    *,
    kit: Kit,
    client_id: int | None,
    stock_map: dict[str, int],
) -> dict[str, int]:
    """Сколько максимум можно списать по ключу: свободно на складе + резерв этого клиента."""
    cid = int(client_id or 0)
    res_c = sum_reserved_by_key_for_client(db, kit_id=int(kit.id), client_id=cid) if cid > 0 else {}
    out: dict[str, int] = {}
    for k, q in stock_map.items():
        extra = int(res_c.get(k, 0))
        out[k] = int(q) + extra
    # резерв без ключа — добавляем ко всем ключам пропорционально составу (консервативно: как равномерный бонус к каждому ключу из stock_map)
    null_extra = int(res_c.get("__NULL__", 0))
    if null_extra > 0 and stock_map:
        dist = distribute_integer_by_weights({k: max(1, stock_map[k]) for k in stock_map}, null_extra)
        for k in stock_map:
            out[k] = int(out.get(k, 0)) + int(dist.get(k, 0))
    return out


def release_client_kit_reserves_into_free_pool(db: Session, *, kit: Kit, client_id: int | None) -> None:
    """Как при визите: снять все резервы клиента по комплекту и вернуть количество в свободный остаток."""
    cid = int(client_id or 0)
    if cid <= 0:
        return
    rows = list(
        db.scalars(
            select(KitReserve)
            .where(KitReserve.kit_id == int(kit.id), KitReserve.reserved_for_client_id == cid)
            .order_by(KitReserve.id.asc())
        ).all()
    )
    if not rows:
        return
    if kit_inventory_is_keyed(db, int(kit.id)):
        deltas: dict[str, int] = {}
        comp = parse_composition_totals(kit)
        for r in rows:
            q = int(r.pieces_reserved or 0)
            if q <= 0:
                continue
            kk = (r.kit_key or "").strip()
            if kk:
                deltas[kk] = deltas.get(kk, 0) + q
            else:
                for ck, dq in distribute_scalar_to_keys(comp if comp else {k: 1 for k in blank_stock_qty_map(db, int(kit.id))}, q).items():
                    deltas[ck] = deltas.get(ck, 0) + int(dq)
            db.delete(r)
        increment_blank_stock_keys(db, int(kit.id), deltas)
        sync_kit_pieces_available_from_blank_lines(db, kit)
    else:
        total = sum(int(r.pieces_reserved or 0) for r in rows)
        for r in rows:
            db.delete(r)
        kit.pieces_available = int(kit.pieces_available or 0) + int(total)


def return_reserve_row_to_stock(db: Session, kit: Kit, r: KitReserve) -> None:
    """Вернуть количество из строки резерва на склад (строка резерва затем удаляется вызывающим кодом)."""
    qty = int(r.pieces_reserved or 0)
    if qty <= 0:
        return
    if kit_inventory_is_keyed(db, int(kit.id)):
        kk = (r.kit_key or "").strip()
        if kk:
            increment_blank_stock_keys(db, int(kit.id), {kk: qty})
        else:
            comp = parse_composition_totals(kit)
            sm = blank_stock_qty_map(db, int(kit.id))
            if comp:
                deltas = distribute_scalar_to_keys(comp, qty)
            elif sm:
                deltas = distribute_integer_by_weights({k: 1 for k in sm}, qty)
            else:
                deltas = {}
            increment_blank_stock_keys(db, int(kit.id), deltas)
        sync_kit_pieces_available_from_blank_lines(db, kit)
    else:
        kit.pieces_available = int(kit.pieces_available or 0) + qty


def _consume_qty_distributed_from_stock_map(stock_map: dict[str, int], qty: int) -> dict[str, int]:
    """Сколько снять с каждого ключа, не больше текущего свободного остатка."""
    take = min(max(0, int(qty)), int(sum(max(0, int(v)) for v in stock_map.values())))
    if take <= 0:
        return {}
    weights = {k: max(0, int(v)) for k, v in stock_map.items() if int(v) > 0}
    if not weights:
        return {}
    dist = distribute_scalar_to_keys(weights, take)
    out: dict[str, int] = {}
    for k, n in dist.items():
        clamped = min(max(0, int(n)), int(stock_map.get(k, 0)))
        if clamped > 0:
            out[k] = clamped
    return out


def consume_blank_stock_for_reserve(
    db: Session,
    kit: Kit,
    *,
    kit_key: str | None,
    qty: int,
    sync_after: bool = True,
) -> None:
    """Снять со свободного остатка при создании резерва."""
    q = int(qty)
    if q <= 0:
        return
    if kit_inventory_is_keyed(db, int(kit.id)):
        kk = (kit_key or "").strip()
        if kk:
            decrement_blank_stock_keys(db, int(kit.id), {kk: q})
        else:
            sm = blank_stock_qty_map(db, int(kit.id))
            dist = _consume_qty_distributed_from_stock_map(sm, q)
            if dist:
                decrement_blank_stock_keys(db, int(kit.id), dist)
        if sync_after:
            sync_kit_pieces_available_from_blank_lines(db, kit)
    else:
        avail = int(kit.pieces_available or 0)
        if q > avail:
            raise ValueError("Недостаточно свободного остатка для резерва.")
        kit.pieces_available = avail - q


def reserve_kit_stock_for_client(
    db: Session,
    kit: Kit,
    *,
    client_id: int,
    reserved_by_user_id: int,
    qty: int,
    kit_key: str | None = None,
) -> None:
    """Снять свободный остаток в резерв клиента (как ручной резерв в админке)."""
    q = int(qty)
    if q <= 0:
        return
    consume_blank_stock_for_reserve(db, kit, kit_key=kit_key, qty=q, sync_after=True)
    db.add(
        KitReserve(
            kit_id=int(kit.id),
            pieces_reserved=q,
            reserved_at=utcnow_naive(),
            reserved_by_user_id=int(reserved_by_user_id),
            reserved_for_client_id=int(client_id),
            reserved_for_user_id=None,
            kit_key=((kit_key or "").strip()[:80] or None),
        )
    )


def repair_kit_blank_stock_reserve_desync(db: Session, kit: Kit) -> bool:
    """Починить рассинхрон свободного остатка и kit_blank_stock после резерва «на заказ».

    Старый путь резервировал комплект, уменьшая только pieces_available, и не снимал
    строки склада. Снятие резерва тогда удваивало остаток.
    """
    if kit.id is None or not kit_inventory_is_keyed(db, int(kit.id)):
        return False
    changed = False
    unkeyed = list(
        db.scalars(
            select(KitReserve).where(
                KitReserve.kit_id == int(kit.id),
                or_(KitReserve.kit_key.is_(None), KitReserve.kit_key == ""),
            )
        ).all()
    )
    unkeyed_qty = sum(int(r.pieces_reserved or 0) for r in unkeyed)
    stock_map = blank_stock_qty_map(db, int(kit.id))
    stock_sum = int(sum(stock_map.values()))
    if unkeyed_qty > 0 and stock_sum > 0:
        dist = _consume_qty_distributed_from_stock_map(stock_map, unkeyed_qty)
        if dist:
            decrement_blank_stock_keys(db, int(kit.id), dist)
            changed = True
            stock_map = blank_stock_qty_map(db, int(kit.id))
            stock_sum = int(sum(stock_map.values()))
    reserved_total = int(
        db.scalar(
            select(func.coalesce(func.sum(KitReserve.pieces_reserved), 0)).where(
                KitReserve.kit_id == int(kit.id)
            )
        )
        or 0
    )
    comp = parse_composition_totals(kit)
    comp_sum = int(sum(int(v) for v in comp.values())) if comp else 0
    total = int(kit.pieces_total or 0)
    expected_free = comp_sum or total
    if (
        reserved_total <= 0
        and expected_free > 0
        and stock_sum == 2 * expected_free
        and (comp_sum <= 0 or comp_sum == total or total <= 0)
    ):
        _, meta, _ = load_catalog_kit_maps(db)
        allowed = set(composition_keys_intersection_catalog(comp, meta)) if comp else set()
        if not allowed and comp:
            allowed = set(comp.keys())
        if not allowed:
            allowed = set(stock_map.keys())
        qty = {k: int(v) for k, v in (comp or stock_map).items() if int(v) > 0}
        if allowed and qty:
            replace_blank_stock_for_kit(db, kit, quantities=qty, allowed_keys=allowed)
            return True
    before = int(kit.pieces_available or 0)
    sync_kit_pieces_available_from_blank_lines(db, kit)
    if int(kit.pieces_available or 0) != before:
        changed = True
    return changed


def repair_all_kits_blank_stock_reserve_desync(db: Session) -> int:
    """Починить все комплекты с kit_blank_stock. Возвращает число изменённых."""
    kit_ids = list(db.scalars(select(KitBlankStock.kit_id).distinct()).all())
    n = 0
    for kid in kit_ids:
        kit = db.get(Kit, int(kid))
        if kit is None:
            continue
        if repair_kit_blank_stock_reserve_desync(db, kit):
            n += 1
    return n


def repair_all_kits_pieces_available_from_blank_stock(db: Session) -> int:
    """Выровнять pieces_available / is_in_stock по сумме kit_blank_stock. Возвращает число изменённых."""
    kit_ids = list(db.scalars(select(KitBlankStock.kit_id).distinct()).all())
    n = 0
    for kid in kit_ids:
        kit = db.get(Kit, int(kid))
        if kit is None:
            continue
        before_avail = int(kit.pieces_available or 0)
        before_stock = bool(kit.is_in_stock)
        sync_kit_pieces_available_from_blank_lines(db, kit)
        if int(kit.pieces_available or 0) != before_avail or bool(kit.is_in_stock) != before_stock:
            n += 1
    return n


def keyed_client_price_selected(
    breakdown: dict[str, int],
    *,
    price_map: dict[str, float],
    meta_by_key: dict[str, dict[str, Any]],
) -> float:
    s = 0.0
    for k, n in breakdown.items():
        if int(n) <= 0:
            continue
        if kit_key_excluded_from_client_price(meta_by_key.get(k) or {}, k):
            continue
        p = price_map.get(k)
        if p is None:
            raise ValueError(f"Нет цены в каталоге для ключа «{k}» (заготовки поштучно).")
        s += float(p) * float(int(n))
    return float(s)


def keyed_cost_selected(breakdown: dict[str, int], *, comp: dict[str, int], kit_cost_total: float) -> float:
    """Себестоимость выбранных заготовок: каждая штука = cost_total / sum(comp)."""
    if not breakdown:
        return 0.0
    s = sum(max(0, int(v)) for v in comp.values()) if comp else 0
    if s <= 0:
        s = max(1, sum(int(v) for v in breakdown.values()))
    per = max(0.0, float(kit_cost_total)) / float(s)
    ntot = sum(int(v) for v in breakdown.values() if int(v) > 0)
    return per * float(ntot)


def apply_discount_capped(price: float, *, discount_percent: int, cost_floor: float) -> tuple[float, float]:
    """Возвращает (discount_amount, net_after_discount)."""
    pct = max(0, min(100, int(discount_percent or 0)))
    raw_disc = float(price) * (pct / 100.0)
    max_m = max(0.0, float(price) - max(0.0, float(cost_floor)))
    disc = min(raw_disc, max_m, float(price))
    net = max(0.0, float(price) - disc)
    return disc, net


def build_usage_breakdown_keyed(
    *,
    use_entire: bool,
    blanks_used: int,
    usage_by_key: dict[str, int] | None,
    max_by_key: dict[str, int],
) -> dict[str, int]:
    if use_entire:
        return {k: int(v) for k, v in max_by_key.items() if int(v) > 0}
    if usage_by_key:
        out = {str(k): int(v) for k, v in usage_by_key.items() if int(v) > 0}
        if sum(out.values()) > 0:
            return out
    # fallback: treat blanks_used as total distributed by max weights
    if blanks_used <= 0:
        raise ValueError("Укажите количество заготовок или «весь комплект».")
    weights = {k: max(0, int(v)) for k, v in max_by_key.items()}
    dist = distribute_integer_by_weights(weights if sum(weights.values()) > 0 else {k: 1 for k in max_by_key}, int(blanks_used))
    if sum(dist.values()) != int(blanks_used):
        raise ValueError("Не удалось распределить количество по видам заготовок.")
    for k, v in dist.items():
        if int(v) > int(max_by_key.get(k, 0)):
            raise ValueError(f"Слишком много по ключу «{k}» для доступного остатка.")
    return {k: int(v) for k, v in dist.items() if int(v) > 0}


def ensure_blank_stock_from_composition(
    db: Session,
    kit: Kit,
    *,
    quantities: dict[str, int] | None = None,
) -> bool:
    """Завести kit_blank_stock из состава, если строк ещё нет (создание комплекта мастером/админом)."""
    if kit.id is None:
        return False
    if kit_inventory_is_keyed(db, int(kit.id)):
        return False
    comp = parse_composition_totals(kit)
    if not comp:
        return False
    if int(kit.pieces_available or 0) <= 0 and int(kit.pieces_total or 0) <= 0:
        return False
    blank_qty = {str(k): int(v) for k, v in (quantities or comp).items() if int(v) > 0}
    if not blank_qty:
        return False
    _, meta, _ = load_catalog_kit_maps(db)
    allowed = set(composition_keys_intersection_catalog(comp, meta)) if comp else set()
    if not allowed and comp:
        allowed = set(comp.keys())
    if not allowed:
        allowed = set(blank_qty.keys())
    replace_blank_stock_for_kit(db, kit, quantities=blank_qty, allowed_keys=allowed)
    return True


def require_composition_stock_rows_or_scalar_ok(db: Session, kit: Kit) -> None:
    """
    Если в составе есть ключи и на складе есть заготовки, но строк kit_blank_stock нет —
    списание «из наличия» блокируем (админ должен завести остатки по видам).
    """
    comp = parse_composition_totals(kit)
    if not comp:
        return
    if int(kit.pieces_available or 0) <= 0:
        return
    if kit_inventory_is_keyed(db, int(kit.id)):
        return
    ensure_blank_stock_from_composition(db, kit)
    db.flush()
    if kit_inventory_is_keyed(db, int(kit.id)):
        return
    raise ValueError(
        "Для этого комплекта задан состав (composition_json), но не заведены остатки по видам заготовок. "
        "Попросите администратора открыть карточку комплекта и заполнить блок «Остаток по видам»."
    )


def read_blank_stock_qty_from_admin_form(form: Any) -> dict[str, int]:
    """Поля вида blank_stock_qty__<KIT_KEY> из POST редактирования комплекта."""
    out: dict[str, int] = {}
    try:
        keys_iter = list(form.keys())
    except Exception:
        return out
    prefix = "blank_stock_qty__"
    for name in keys_iter:
        if not isinstance(name, str) or not name.startswith(prefix):
            continue
        key = name[len(prefix) :].strip()
        if not key:
            continue
        v = form.get(name)
        if v is None or isinstance(v, UploadFile):
            continue
        s = v.decode().strip() if isinstance(v, (bytes, bytearray)) else str(v).strip()
        if not s:
            continue
        try:
            n = int(s.replace(",", ".").split(".")[0])
        except ValueError:
            continue
        out[key[:80]] = max(0, n)
    return out


def replace_blank_stock_for_kit(
    db: Session,
    kit: Kit,
    *,
    quantities: dict[str, int],
    allowed_keys: set[str],
) -> None:
    """Полная замена строк склада по ключам (форма редактирования)."""
    for k in quantities:
        if k not in allowed_keys:
            raise ValueError(f"Ключ «{k}» не из состава комплекта / каталога — строка склада недопустима.")
    db.execute(delete(KitBlankStock).where(KitBlankStock.kit_id == int(kit.id)))
    for k, q in quantities.items():
        n = max(0, int(q))
        if n == 0:
            continue
        db.add(KitBlankStock(kit_id=int(kit.id), kit_key=str(k)[:80], qty=n))
    db.flush()
    sync_kit_pieces_available_from_blank_lines(db, kit)


def load_kit_for_stock_ops(db: Session, kit_id: int) -> Kit | None:
    return db.scalar(
        select(Kit)
        .where(Kit.id == int(kit_id))
        .options(
            selectinload(Kit.reserves),
            selectinload(Kit.blank_stock_lines),
        )
    )


def planned_kit_stock_revert_pieces(
    db: Session,
    kit: Kit,
    pieces: int,
    breakdown: dict[str, int] | None,
) -> tuple[int, dict[str, int] | None]:
    """Сколько штук реально вернуть на склад при откате списания визита.

    Если остаток уже восстановлен (рассинхрон с VisitKitUsage), возвращаем 0 —
    запись списания удаляется без повторного зачисления на склад.
    """
    pieces = int(pieces or 0)
    if pieces <= 0:
        return 0, breakdown

    if kit_inventory_is_keyed(db, int(kit.id)):
        stock_map = blank_stock_qty_map(db, int(kit.id))
        stock_sum = int(sum(int(v) for v in stock_map.values()))
        comp = parse_composition_totals(kit)
        comp_sum = int(sum(int(v) for v in comp.values())) if comp else int(kit.pieces_total or 0)
        if comp_sum <= 0:
            comp_sum = int(kit.pieces_total or 0)
        if comp_sum > 0 and stock_sum >= comp_sum:
            return 0, breakdown
        if comp_sum > 0:
            max_return = comp_sum - stock_sum
            if max_return <= 0:
                return 0, breakdown
            if pieces > max_return:
                pieces = max_return
                if breakdown:
                    bd_sum = sum(int(v) for v in breakdown.values())
                    if bd_sum > 0 and pieces < bd_sum:
                        breakdown = distribute_integer_by_weights(
                            {k: int(v) for k, v in breakdown.items() if int(v) > 0},
                            pieces,
                        )
                else:
                    breakdown = None
        return pieces, breakdown

    avail = int(kit.pieces_available or 0)
    total = int(kit.pieces_total or 0)
    if total >= 0 and avail + pieces > total:
        return 0, breakdown
    return pieces, breakdown


def return_stock_to_kit(
    db: Session,
    *,
    kit_id: int,
    breakdown: dict[str, int] | None,
    pieces_used: int,
) -> None:
    """Вернуть списанное на склад (отмена визита / коррекция)."""
    kit = load_kit_for_stock_ops(db, kit_id)
    if not kit:
        return
    if kit_inventory_is_keyed(db, int(kit_id)):
        bd = breakdown
        if not bd and pieces_used > 0:
            comp = parse_composition_totals(kit)
            sm = blank_stock_qty_map(db, int(kit_id))
            keys = list(sm.keys()) or list(comp.keys())
            if keys:
                w = {k: int(comp.get(k, 1)) for k in keys}
                bd = distribute_integer_by_weights(w if sum(w.values()) > 0 else {k: 1 for k in keys}, int(pieces_used))
            else:
                bd = {}
        if bd:
            increment_blank_stock_keys(db, int(kit_id), {k: int(v) for k, v in bd.items() if int(v) > 0})
        sync_kit_pieces_available_from_blank_lines(db, kit)
        return
    if pieces_used > 0:
        kit.pieces_available = int(kit.pieces_available or 0) + int(pieces_used)
        if kit.pieces_available > 0:
            kit.is_in_stock = True


def parse_usage_breakdown_json(raw: str | None) -> dict[str, int] | None:
    if not raw:
        return None
    try:
        d = json.loads(str(raw))
    except Exception:
        return None
    if not isinstance(d, dict):
        return None
    out: dict[str, int] = {}
    for k, v in d.items():
        try:
            n = int(v)
        except Exception:
            continue
        if n > 0:
            out[str(k)] = n
    return out or None


def infer_kit_blanks_condition_from_totals(db: Session, kit_totals: dict[str, int]) -> KitBlanksCondition:
    """По составу и меткам is_bu в каталоге: только новые / только Б/У / смешанный."""
    _, meta_by_key, _ = load_catalog_kit_maps(db)
    has_new = False
    has_bu = False
    for k, q in kit_totals.items():
        if int(q or 0) <= 0:
            continue
        kk = str(k).strip()
        m = meta_by_key.get(kk) or {}
        if bool(m.get("is_bu")):
            has_bu = True
        else:
            has_new = True
    if has_new and has_bu:
        return KitBlanksCondition.MIXED
    if has_bu:
        return KitBlanksCondition.USED
    return KitBlanksCondition.NEW
