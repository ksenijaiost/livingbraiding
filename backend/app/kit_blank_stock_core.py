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


_USED_STOCK_SUFFIX = "__USED__"


def _stock_key_for_condition(base_key: str, condition: str) -> str:
    k = str(base_key or "").strip()
    if not k:
        return ""
    return k if str(condition or "NEW").upper() != "USED" else f"{k}{_USED_STOCK_SUFFIX}"


def _split_stock_key_condition(stock_key: str) -> tuple[str, str]:
    raw = str(stock_key or "").strip()
    if not raw:
        return "", "NEW"
    if raw.endswith(_USED_STOCK_SUFFIX):
        return raw[: -len(_USED_STOCK_SUFFIX)], "USED"
    if raw.endswith("_USED"):
        return raw[: -len("_USED")], "USED"
    return raw, "NEW"


def inventory_qty_by_key_from_kit(kit: Kit) -> dict[str, int]:
    """Количество заготовок по ключу склада из состава (NEW и USED — отдельно)."""
    from app.kit_composition_lines import filter_nonempty, lines_from_json

    raw = getattr(kit, "composition_json", None)
    lines = filter_nonempty(lines_from_json(str(raw))) if raw else []
    if lines:
        out: dict[str, int] = {}
        for ln in lines:
            if ln.total_qty() <= 0 or str(ln.key or "") in KIT_INVENTORY_PIECE_EXCLUDE_KEYS:
                continue
            stock_key = _stock_key_for_condition(ln.key, getattr(ln.condition, "value", ln.condition))
            if not stock_key:
                continue
            out[stock_key] = out.get(stock_key, 0) + int(ln.total_qty())
        return {k: int(v) for k, v in out.items() if int(v) > 0}
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
        base_key, cond = _split_stock_key_condition(k)
        is_used = cond == "USED"
        fallback_label = label_by_key.get(base_key, base_key)
        rows.append(
            {
                "key": base_key,
                "raw_key": k,
                "qty": int(sm.get(k, 0)),
                "label": fallback_label,
                "price": price_map.get(base_key),
                "condition_label": "б/у" if is_used else "нов",
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
        per_key = reserve_row_per_key_map(r)
        if per_key is not None:
            for k, v in per_key.items():
                out[k] = out.get(k, 0) + int(v)
        else:
            out["__NULL__"] = out.get("__NULL__", 0) + int(r.pieces_reserved or 0)
    return out


def reserve_row_per_key_map(r: KitReserve) -> dict[str, int] | None:
    """Явная разбивка по ключам (breakdown_json или один kit_key); None — legacy scalar без ключа."""
    raw = getattr(r, "reserve_breakdown_json", None)
    if raw:
        try:
            d = json.loads(str(raw))
            if isinstance(d, dict) and d:
                return {str(k): int(v) for k, v in d.items() if int(v) > 0}
        except Exception:
            pass
    kk = (r.kit_key or "").strip()
    if kk:
        return {kk: int(r.pieces_reserved or 0)}
    return None


def reserve_row_stock_deltas(db: Session, kit: Kit, r: KitReserve) -> dict[str, int]:
    """Сколько вернуть на склад по каждому ключу при снятии резерва."""
    per_key = reserve_row_per_key_map(r)
    if per_key is not None:
        return dict(per_key)
    q = int(r.pieces_reserved or 0)
    if q <= 0:
        return {}
    sm = blank_stock_qty_map(db, int(kit.id))
    comp_use = _composition_for_stock_keys(kit, sm)
    if comp_use:
        return distribute_scalar_to_keys(comp_use, q)
    if sm:
        return distribute_integer_by_weights({k: max(1, sm[k]) for k in sm}, q)
    return {}


def reserve_breakdown_json_from_map(breakdown: dict[str, int]) -> str | None:
    bd = {str(k): int(v) for k, v in breakdown.items() if int(v) > 0}
    if len(bd) <= 1:
        return None
    return json.dumps(bd, ensure_ascii=False)


def kit_reserve_fields_from_breakdown(breakdown: dict[str, int]) -> tuple[str | None, str | None, int]:
    """(kit_key, reserve_breakdown_json, pieces_reserved) для одной строки резерва."""
    bd = {str(k): int(v) for k, v in breakdown.items() if int(v) > 0}
    total = int(sum(bd.values()))
    if len(bd) == 1:
        kk = next(iter(bd.keys()))
        return str(kk)[:80], None, total
    return None, reserve_breakdown_json_from_map(bd), total


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
    # ключи только в резерве (строки склада уже 0/удалены) — тоже доступны клиенту
    for k, v in res_c.items():
        if k == "__NULL__":
            continue
        if k not in out:
            out[k] = int(v)
    # резерв без ключа — раскладываем по составу комплекта (как при снятии резерва)
    null_extra = int(res_c.get("__NULL__", 0))
    if null_extra > 0:
        comp_use = _composition_for_stock_keys(kit, stock_map if stock_map else out)
        if comp_use:
            dist = distribute_scalar_to_keys(comp_use, null_extra)
            for k, add in dist.items():
                if int(add) > 0:
                    out[k] = int(out.get(k, 0)) + int(add)
        elif stock_map:
            dist = distribute_integer_by_weights({k: max(1, stock_map[k]) for k in stock_map}, null_extra)
            for k, add in dist.items():
                if int(add) > 0:
                    out[k] = int(out.get(k, 0)) + int(add)
    return {k: int(v) for k, v in out.items() if int(v) > 0}


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
        for r in rows:
            q = int(r.pieces_reserved or 0)
            if q <= 0:
                continue
            for ck, dq in reserve_row_stock_deltas(db, kit, r).items():
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
        deltas = reserve_row_stock_deltas(db, kit, r)
        if deltas:
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


def kit_reserve_free_rows(db: Session, kit: Kit) -> tuple[bool, list[dict[str, Any]]]:
    """Свободный остаток по ключам для модалки резерва: (keyed, rows)."""
    if kit.id is None:
        return False, []
    keyed = kit_inventory_is_keyed(db, int(kit.id))
    if not keyed:
        return False, []
    stock_map = blank_stock_qty_map(db, int(kit.id))
    _price, _meta, label_by_key = load_catalog_kit_maps(db)
    rows: list[dict[str, Any]] = []
    for kk in sorted(stock_map.keys()):
        q = int(stock_map.get(kk, 0))
        if q <= 0:
            continue
        rows.append({"key": kk, "label": label_by_key.get(kk) or kk, "qty_free": q})
    return True, rows


def split_unkeyed_kit_reserves_by_composition(db: Session, kit: Kit) -> int:
    """Разложить legacy-резервы без kit_key на строки по ключам (по составу комплекта)."""
    if kit.id is None:
        return 0
    stock_map = blank_stock_qty_map(db, int(kit.id))
    comp_use = _composition_for_stock_keys(kit, stock_map)
    if not comp_use:
        return 0
    changed = 0
    unkeyed = list(
        db.scalars(
            select(KitReserve).where(
                KitReserve.kit_id == int(kit.id),
                or_(KitReserve.kit_key.is_(None), KitReserve.kit_key == ""),
            )
        ).all()
    )
    for r in unkeyed:
        q = int(r.pieces_reserved or 0)
        if q <= 0:
            db.delete(r)
            changed += 1
            continue
        dist = distribute_scalar_to_keys(comp_use, q)
        parts = [(str(kk), int(qn)) for kk, qn in dist.items() if int(qn) > 0]
        if not parts:
            continue
        kit_key, breakdown_json, total = kit_reserve_fields_from_breakdown(dict(parts))
        r.kit_key = kit_key
        r.reserve_breakdown_json = breakdown_json
        r.pieces_reserved = total
        changed += 1
    return changed


def merge_keyed_kit_reserve_rows_by_batch(db: Session, kit: Kit) -> int:
    """Слить несколько строк резерва одной «порции» (разные kit_key) в одну с breakdown_json."""
    from collections import defaultdict

    rows = list(db.scalars(select(KitReserve).where(KitReserve.kit_id == int(kit.id))).all())
    groups: dict[tuple, list[KitReserve]] = defaultdict(list)
    for r in rows:
        if getattr(r, "reserve_breakdown_json", None):
            continue
        if not (r.kit_key or "").strip():
            continue
        batch_key = (
            int(r.reserved_for_client_id or 0),
            int(r.reserved_for_user_id or 0),
            int(r.reserved_by_user_id or 0),
            r.reserved_at,
            int(r.booking_id or 0),
        )
        groups[batch_key].append(r)
    changed = 0
    for batch in groups.values():
        if len(batch) < 2:
            continue
        breakdown: dict[str, int] = {}
        for r in batch:
            kk = (r.kit_key or "").strip()
            if kk:
                breakdown[kk] = breakdown.get(kk, 0) + int(r.pieces_reserved or 0)
        if len(breakdown) < 2:
            continue
        keep = batch[0]
        keep.kit_key, keep.reserve_breakdown_json, keep.pieces_reserved = kit_reserve_fields_from_breakdown(
            breakdown
        )
        for r in batch[1:]:
            db.delete(r)
        changed += 1
    return changed


def repair_all_merged_keyed_kit_reserves(db: Session) -> int:
    """Слить разнесённые по ключам строки резерва в одну на комплект. Возвращает число комплектов с изменениями."""
    kit_ids = list(db.scalars(select(KitReserve.kit_id).distinct()).all())
    n = 0
    for kid in kit_ids:
        kit = db.get(Kit, int(kid))
        if kit is None:
            continue
        if merge_keyed_kit_reserve_rows_by_batch(db, kit) > 0:
            n += 1
    return n


def repair_all_unkeyed_kit_reserves(db: Session) -> int:
    """Разложить все legacy-резервы без kit_key. Возвращает число комплектов с изменениями."""
    kit_ids = list(
        db.scalars(
            select(KitReserve.kit_id)
            .where(or_(KitReserve.kit_key.is_(None), KitReserve.kit_key == ""))
            .distinct()
        ).all()
    )
    n = 0
    for kid in kit_ids:
        kit = db.get(Kit, int(kid))
        if kit is None:
            continue
        if split_unkeyed_kit_reserves_by_composition(db, kit) > 0:
            n += 1
    return n


def _composition_for_stock_keys(kit: Kit, stock_map: dict[str, int]) -> dict[str, int]:
    """Состав комплекта, ограниченный ключами склада (без обрезков вне остатка)."""
    comp = parse_composition_totals(kit)
    if not comp:
        return {}
    keys = set(stock_map.keys()) if stock_map else set(inventory_qty_by_key_from_kit(kit).keys())
    keys = {k for k in keys if k not in KIT_INVENTORY_PIECE_EXCLUDE_KEYS}
    if keys:
        filtered = {k: int(comp.get(k, 0)) for k in keys if int(comp.get(k, 0)) > 0}
        if filtered:
            return filtered
    return {
        k: int(v)
        for k, v in comp.items()
        if int(v) > 0 and k not in KIT_INVENTORY_PIECE_EXCLUDE_KEYS
    }


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
    if split_unkeyed_kit_reserves_by_composition(db, kit) > 0:
        changed = True
    if merge_keyed_kit_reserve_rows_by_batch(db, kit) > 0:
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


def catalog_unit_weight_for_kit_key(
    db: Session,
    kit: Kit,
    kit_key: str,
    *,
    price_map: dict[str, float],
    meta_by_key: dict[str, dict[str, Any]],
) -> float | None:
    """Вес ключа для разложения цены склада (= прайсовая цена единицы, с учётом б/у в v2)."""
    from app.kit_composition_lines import (
        composition_has_v2_lines,
        lines_from_json,
        unit_client_price_for_key,
    )

    kk = str(kit_key or "").strip()
    if not kk or kit_key_excluded_from_client_price(meta_by_key.get(kk) or {}, kk):
        return None
    base_k, _cond = _split_stock_key_condition(kk)
    lookup = base_k or kk
    if composition_has_v2_lines(getattr(kit, "composition_json", None)):
        lines = lines_from_json(str(kit.composition_json or ""))
        unit = unit_client_price_for_key(
            db, lines, lookup, price_map=price_map, meta_by_key=meta_by_key
        )
        if unit is not None:
            return float(unit)
    p = price_map.get(kk)
    if p is None and lookup != kk:
        p = price_map.get(lookup)
    return float(p) if p is not None else None


def keyed_stock_unit_prices_from_catalog_weights(
    db: Session,
    kit: Kit,
    *,
    stock_price_total: float,
    composition_qty: dict[str, int],
    price_map: dict[str, float],
    meta_by_key: dict[str, dict[str, Any]],
    extra_keys: list[str] | None = None,
) -> dict[str, float]:
    """Цена 1 шт. каждого вида из «цены на складе», веса — прайс (соотношение x:y).

    stock_total = Σ composition_qty[k] * unit[k],
    unit[k] = stock_total * catalog_weight[k] / Σ(composition_qty[i] * catalog_weight[i]).
    """
    gross = max(0.0, float(stock_price_total or 0.0))
    if gross <= 0:
        return {}
    keys = set(str(k) for k, q in (composition_qty or {}).items() if int(q) > 0)
    for k in extra_keys or []:
        if str(k or "").strip():
            keys.add(str(k).strip())
    weights: dict[str, float] = {}
    for k in keys:
        w = catalog_unit_weight_for_kit_key(
            db, kit, k, price_map=price_map, meta_by_key=meta_by_key
        )
        if w is not None and float(w) > 0:
            weights[k] = float(w)
    denom = 0.0
    for k, q in (composition_qty or {}).items():
        qi = int(q)
        if qi <= 0:
            continue
        w = weights.get(str(k))
        if w is None:
            continue
        denom += float(qi) * float(w)
    if denom <= 0:
        return {}
    return {k: gross * float(w) / denom for k, w in weights.items()}


def keyed_stock_price_selected(
    breakdown: dict[str, int],
    *,
    unit_stock_by_key: dict[str, float],
) -> float | None:
    """Сумма по списанию из unit-цен склада. None — нет разложения, нужен fallback на прайс."""
    if not unit_stock_by_key:
        return None
    s = 0.0
    for k, n in breakdown.items():
        ni = int(n)
        if ni <= 0:
            continue
        u = unit_stock_by_key.get(str(k))
        if u is None:
            return None
        s += float(u) * float(ni)
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
    inv = inventory_qty_by_key_from_kit(kit)
    if not inv:
        return False
    if int(kit.pieces_available or 0) <= 0 and int(kit.pieces_total or 0) <= 0:
        return False
    blank_qty = {str(k): int(v) for k, v in (quantities or inv).items() if int(v) > 0}
    if not blank_qty:
        return False
    allowed = set(inv.keys())
    if not allowed:
        allowed = set(blank_qty.keys())
    replace_blank_stock_for_kit(db, kit, quantities=blank_qty, allowed_keys=allowed)
    return True


def require_composition_stock_rows_or_scalar_ok(db: Session, kit: Kit) -> None:
    """
    Если в составе есть ключи и на складе есть заготовки, но строк kit_blank_stock нет —
    списание «из наличия» блокируем (админ должен завести остатки по видам).
    """
    inv = inventory_qty_by_key_from_kit(kit)
    if not inv:
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
