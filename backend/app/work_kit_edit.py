"""Редактирование работы с комплектом: состав заготовок, мастера, связанный Kit."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.db.models import (
    Kit,
    KitAuthorStaff,
    KitReserve,
    MixComplexity,
    User,
    UserRole,
    WorkForInventory,
    WorkForInventoryStaff,
    WorkKind,
    WorkScope,
)
from app.forms_parse import parse_float
from app.kit_blank_stock_core import (
    composition_keys_intersection_catalog,
    load_catalog_kit_maps,
    replace_blank_stock_for_kit,
)
from app.kit_composition_lines import (
    client_price_for_lines,
    filter_nonempty,
    infer_blanks_condition,
    inventory_piece_count,
    inventory_totals_by_key,
    kit_by_staff_from_lines,
    lines_dicts_for_details,
    lines_from_form,
    lines_from_json,
    lines_to_json,
    lines_to_legacy_totals,
)
from app.user_roles import user_has_role
from starlette.datastructures import UploadFile


def _g_str(form: Any, name: str, default: str = "") -> str:
    v = form.get(name)
    if v is None or isinstance(v, UploadFile):
        return default
    if isinstance(v, (bytes, bytearray)):
        return v.decode().strip()
    return str(v).strip()


def _g_float(form: Any, name: str, default: float = 0.0) -> float:
    s = _g_str(form, name, "")
    if not s:
        return default
    try:
        return parse_float(s, default=default, field_name=name)
    except ValueError:
        return default


def _g_bool(form: Any, name: str) -> bool:
    v = form.get(name)
    if v is None or isinstance(v, UploadFile):
        return False
    s = v.decode() if isinstance(v, (bytes, bytearray)) else v
    from app.forms_parse import parse_bool

    return parse_bool(s)


def read_kit_master_on_ids(form: Any) -> list[int]:
    raw: list[Any] = []
    if hasattr(form, "getlist"):
        raw = list(form.getlist("kit_master_on"))
    else:
        v = form.get("kit_master_on")
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


def details_lines_to_initial_lines(lines: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for ln in lines or []:
        if not isinstance(ln, dict):
            continue
        by_staff: dict[int, int] = {}
        for k, v in (ln.get("by_staff") or {}).items():
            try:
                qi = int(v)
            except (TypeError, ValueError):
                qi = 0
            if qi > 0:
                by_staff[int(k)] = qi
        out.append(
            {
                "key": ln.get("key") or "",
                "condition": ln.get("condition") or "NEW",
                "used_price_pct": ln.get("used_price_pct") if ln.get("used_price_pct") is not None else 100,
                "by_staff": by_staff,
            }
        )
    return out


def kit_master_ids_from_work(work: WorkForInventory, kit_detail: dict[str, Any]) -> list[int]:
    seen: set[int] = set()
    out: list[int] = []
    for s in work.staff_rows or []:
        uid = int(s.user_id)
        if uid not in seen:
            seen.add(uid)
            out.append(uid)
    if out:
        return out
    for k in (kit_detail.get("by_staff") or {}):
        try:
            uid = int(k)
        except (TypeError, ValueError):
            continue
        if uid not in seen:
            seen.add(uid)
            out.append(uid)
    for ln in kit_detail.get("lines") or []:
        if not isinstance(ln, dict):
            continue
        for k in (ln.get("by_staff") or {}):
            try:
                uid = int(k)
            except (TypeError, ValueError):
                continue
            if uid not in seen:
                seen.add(uid)
                out.append(uid)
    return out


def read_staff_profits_from_form(form: Any, active_uids: list[int]) -> dict[int, float]:
    out: dict[int, float] = {}
    for uid in active_uids:
        out[uid] = max(0.0, _g_float(form, f"staff_profit_{uid}", 0.0))
    return out


@dataclass
class KitEditResult:
    kit_staff_ids: list[int]
    staff_profits: dict[int, float]
    master_total: float
    kit_detail: dict[str, Any]


def _sync_kit_reserve_pieces(db: Session, kit: Kit, work: WorkForInventory, new_pieces: int) -> None:
    reserves = list(db.scalars(select(KitReserve).where(KitReserve.kit_id == int(kit.id))).all())
    kit.pieces_total = int(new_pieces)
    if work.scope == WorkScope.CUSTOM_ORDER and reserves:
        work_reserve = None
        for r in reserves:
            if work.client_id and int(r.reserved_for_client_id or 0) == int(work.client_id):
                work_reserve = r
                break
        if work_reserve is not None and len(reserves) == 1:
            work_reserve.pieces_reserved = int(new_pieces)
        total_reserved = int(
            db.scalar(
                select(func.coalesce(func.sum(KitReserve.pieces_reserved), 0)).where(
                    KitReserve.kit_id == int(kit.id)
                )
            )
            or 0
        )
        kit.pieces_available = max(0, int(new_pieces) - total_reserved)
    elif not reserves:
        kit.pieces_available = int(new_pieces)


def sync_work_kit_reserves_for_scope(
    db: Session,
    work: WorkForInventory,
    *,
    prev_scope: WorkScope | None,
    prev_client_id: int | None,
    actor_user_id: int,
) -> None:
    """При смене режима/клиента: снять старый резерв и при «на заказ» зарезервировать комплект."""
    if work.kind != WorkKind.KIT or not work.created_kit_id:
        return
    kit = db.get(Kit, int(work.created_kit_id))
    if not kit:
        return

    from app.kit_blank_stock_core import release_client_kit_reserves_into_free_pool
    from app.time_utils import utcnow_naive

    old_cid = int(prev_client_id or 0)
    new_cid = int(work.client_id or 0)
    left_custom = prev_scope == WorkScope.CUSTOM_ORDER and work.scope != WorkScope.CUSTOM_ORDER
    client_changed = (
        prev_scope == WorkScope.CUSTOM_ORDER
        and work.scope == WorkScope.CUSTOM_ORDER
        and old_cid > 0
        and old_cid != new_cid
    )
    if (left_custom or client_changed) and old_cid > 0:
        release_client_kit_reserves_into_free_pool(db, kit=kit, client_id=old_cid)

    if work.scope != WorkScope.CUSTOM_ORDER or new_cid <= 0:
        return

    existing = list(
        db.scalars(
            select(KitReserve).where(
                KitReserve.kit_id == int(kit.id),
                KitReserve.reserved_for_client_id == new_cid,
            )
        ).all()
    )
    if existing:
        return

    pieces_reserved = int(kit.pieces_total or 0)
    if pieces_reserved <= 0:
        return
    db.add(
        KitReserve(
            kit_id=int(kit.id),
            pieces_reserved=pieces_reserved,
            reserved_at=utcnow_naive(),
            reserved_by_user_id=int(actor_user_id),
            reserved_for_client_id=new_cid,
            reserved_for_user_id=None,
        )
    )
    kit.pieces_available = max(0, int(kit.pieces_available or 0) - pieces_reserved)


def apply_kit_work_edit(
    db: Session,
    work: WorkForInventory,
    form: Any,
    *,
    extra_costs_amount: float,
    cost_total_amount: float,
    alloc_equal_shares_for_masters,
    kit_stock_price_snapshot_text,
    kit_cost_snapshot_text,
) -> KitEditResult:
    """Обновить details_json.kit и связанный Kit по данным формы редактирования."""
    if work.kind != WorkKind.KIT:
        raise ValueError("Редактирование состава доступно только для работ «Комплект».")
    if not work.created_kit_id:
        raise ValueError("У работы нет связанного комплекта на складе — состав изменить нельзя.")

    kit = db.get(Kit, int(work.created_kit_id))
    if not kit:
        raise ValueError("Связанный комплект не найден.")

    kit_blank_type_se = _g_bool(form, "kit_type_se")
    kit_blank_type_de = _g_bool(form, "kit_type_de")
    if not kit_blank_type_se and not kit_blank_type_de:
        raise ValueError("Для комплекта выберите тип заготовок: SE и/или DE.")

    kit_use_multi_masters = _g_bool(form, "kit_use_multi_masters")
    if kit_use_multi_masters:
        kit_staff_ids = read_kit_master_on_ids(form)
        if not kit_staff_ids:
            raise ValueError(
                "Для таблицы комплекта отметьте мастеров или снимите «Несколько мастеров (комплект)»."
            )
    else:
        single_raw = (_g_str(form, "kit_single_master_id", "") or "").strip()
        if not single_raw:
            raise ValueError("Укажите мастера для таблицы заготовок.")
        try:
            uid = int(single_raw)
        except ValueError as e:
            raise ValueError("Некорректный мастер в таблице заготовок.") from e
        kit_staff_ids = [uid]
        mu = db.get(User, uid)
        if not mu or not mu.is_active or not user_has_role(db, uid, UserRole.MASTER):
            raise ValueError("В комплекте участвуют только активные мастера.")

    composition_lines = filter_nonempty(lines_from_form(form))
    if not composition_lines:
        raise ValueError("Для комплекта укажите хотя бы одну строку состава (вид и количество).")

    kit_totals, kit_by_staff = kit_by_staff_from_lines(composition_lines)
    kit_pieces_inventory = inventory_piece_count(composition_lines)

    details = {}
    if work.details_json:
        try:
            parsed = json.loads(work.details_json)
            if isinstance(parsed, dict):
                details = parsed
        except Exception:
            details = {}
    kit_prev = details.get("kit") if isinstance(details.get("kit"), dict) else {}

    kit_detail: dict[str, Any] = {
        "blank_type_se": kit_blank_type_se,
        "blank_type_de": kit_blank_type_de,
        "totals": kit_totals,
        "by_staff": {str(k): v for k, v in kit_by_staff.items()},
        "lines": lines_dicts_for_details(composition_lines),
        "bu_correction": bool(kit_prev.get("bu_correction")),
    }
    if kit_prev.get("bu_correction_details"):
        kit_detail["bu_correction_details"] = kit_prev["bu_correction_details"]

    catalog_client_price, missing_price = client_price_for_lines(
        db, composition_lines, extra_costs_amount=float(extra_costs_amount)
    )
    if missing_price:
        miss = ", ".join(missing_price)
        raise ValueError(f"Не найдены цены в прайсе «Заказ → Заготовки поштучно» для: {miss}.")
    kit_detail["catalog_client_price"] = float(catalog_client_price)
    details["kit"] = kit_detail
    work.details_json = json.dumps(details, ensure_ascii=False)

    staff_profits = read_staff_profits_from_form(form, kit_staff_ids)
    master_total = float(sum(staff_profits.values()))

    mix_complexity: MixComplexity | None = None
    raw_mc = details.get("mix_complexity")
    if raw_mc:
        try:
            mix_complexity = MixComplexity(str(raw_mc))
        except ValueError:
            mix_complexity = None

    comp_json = lines_to_json(composition_lines)
    stock_price_total, _ = client_price_for_lines(
        db, composition_lines, extra_costs_amount=float(extra_costs_amount)
    )
    kit.composition_json = comp_json
    kit.blank_type_se = kit_blank_type_se
    kit.blank_type_de = kit_blank_type_de
    kit.blanks_condition = infer_blanks_condition(composition_lines)
    kit.stock_price_total = float(stock_price_total)
    kit.stock_price_snapshot_text = kit_stock_price_snapshot_text(
        db, kit_totals=kit_totals, extra_costs_amount=float(extra_costs_amount)
    )
    full_cost = float(cost_total_amount) + master_total
    kit.cost_total = full_cost
    kit.cost_snapshot_text = kit_cost_snapshot_text(
        db,
        kit_totals=kit_totals,
        mat_cost=float(work.materials_cost_total or 0.0),
        kanek=float(work.kanekalon_grams or 0.0),
        kudri=float(work.kudri_grams or 0.0),
        k_snap=float(work.kanekalon_price_per_gram_at_time or 0.0),
        ku_snap=float(work.kudri_price_per_gram_at_time or 0.0),
        mix_source=work.mix_source,
        mix_complexity=mix_complexity,
        grams_total=float((work.kanekalon_grams or 0.0) + (work.kudri_grams or 0.0)),
        extra_costs_amount=float(extra_costs_amount),
    )

    blank_qty = inventory_totals_by_key(composition_lines)
    comp_legacy = lines_to_legacy_totals(composition_lines)
    _, meta_map, _ = load_catalog_kit_maps(db)
    allowed = set(composition_keys_intersection_catalog(comp_legacy, meta_map)) if comp_legacy else set()
    if not allowed and comp_legacy:
        allowed = set(comp_legacy.keys())
    if not allowed:
        allowed = set(blank_qty.keys())
    if blank_qty:
        replace_blank_stock_for_kit(db, kit, quantities=blank_qty, allowed_keys=allowed)

    _sync_kit_reserve_pieces(db, kit, work, kit_pieces_inventory)

    db.execute(delete(KitAuthorStaff).where(KitAuthorStaff.kit_id == int(kit.id)))
    seen_uid: set[int] = set()
    so = 0
    for uid in kit_staff_ids:
        if uid <= 0 or uid in seen_uid:
            continue
        seen_uid.add(uid)
        mu = db.get(User, uid)
        if mu and mu.is_active and user_has_role(db, uid, UserRole.MASTER):
            db.add(KitAuthorStaff(kit_id=int(kit.id), user_id=uid, sort_order=so))
            so += 1

    return KitEditResult(
        kit_staff_ids=kit_staff_ids,
        staff_profits=staff_profits,
        master_total=master_total,
        kit_detail=kit_detail,
    )


def replace_work_staff_rows(
    db: Session,
    work: WorkForInventory,
    kit_staff_ids: list[int],
    staff_profits: dict[int, float],
    alloc_equal_shares_for_masters,
) -> list[WorkForInventoryStaff]:
    """Пересоздать строки мастеров работы по списку из таблицы заготовок."""
    alloc = alloc_equal_shares_for_masters(db, kit_staff_ids)
    db.execute(delete(WorkForInventoryStaff).where(WorkForInventoryStaff.work_id == int(work.id)))
    rows: list[WorkForInventoryStaff] = []
    for uid, share in alloc:
        row = WorkForInventoryStaff(
            work_id=int(work.id),
            user_id=int(uid),
            share=float(share),
            master_profit_amount=float(staff_profits.get(uid, 0.0)),
            details_json=None,
        )
        db.add(row)
        rows.append(row)
    db.flush()
    return rows


def work_kit_edit_template_extras(
    db: Session,
    work: WorkForInventory,
    *,
    kit_table_state_json_builder,
    list_masters,
) -> dict[str, Any]:
    """Дополнительный контекст шаблона редактирования для работы-комплекта."""
    masters = list_masters(db)
    details: dict[str, Any] = {}
    if work.details_json:
        try:
            parsed = json.loads(work.details_json)
            if isinstance(parsed, dict):
                details = parsed
        except Exception:
            details = {}
    kit_detail = details.get("kit") if isinstance(details.get("kit"), dict) else {}
    kit_master_on_ids = kit_master_ids_from_work(work, kit_detail)
    staff_profit_by_uid = {
        int(s.user_id): float(s.master_profit_amount or 0.0) for s in (work.staff_rows or [])
    }
    kit = db.get(Kit, int(work.created_kit_id)) if work.created_kit_id else None
    initial_lines = details_lines_to_initial_lines(kit_detail.get("lines"))
    if not initial_lines and kit and kit.composition_json:
        initial_lines = details_lines_to_initial_lines(
            lines_dicts_for_details(lines_from_json(kit.composition_json))
        )
    single_master_id = kit_master_on_ids[0] if len(kit_master_on_ids) == 1 else 0
    return {
        "is_kit_work": True,
        "masters": masters,
        "kit_detail": kit_detail,
        "kit": kit,
        "kit_master_on_ids": kit_master_on_ids,
        "kit_use_multi_masters": len(kit_master_on_ids) != 1,
        "kit_single_master_id": single_master_id,
        "kit_type_se": bool(kit_detail.get("blank_type_se")),
        "kit_type_de": bool(kit_detail.get("blank_type_de")),
        "staff_profit_by_uid": staff_profit_by_uid,
        "profit_master_uids": kit_master_on_ids,
        "kit_table_state_json": kit_table_state_json_builder(
            masters=masters,
            initial_lines=initial_lines,
        ),
    }
