from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.db.models import MixComplexity, MixSource, WorkKind, WorkScope


@dataclass(frozen=True)
class WorkFinancials:
    staff_master_profit: dict[int, float]
    master_total: float
    studio_total: float
    profit_total: float
    extra_costs_amount: float
    cost_total_amount: float
    studio_share_snapshot: float


def compute_work_financials(
    db: Session,
    *,
    kind: WorkKind,
    scope: WorkScope,
    alloc: list[tuple[int, float]],
    current_user_id: int,
    # materials
    mat_cost: float,
    # kit
    kit_totals: dict[str, int],
    kit_staff_ids: list[int],
    kit_by_staff: dict[int, dict[str, int]],
    # mix
    mix_source: MixSource | None,
    mix_complexity: MixComplexity | None,
    grams_total: float,
    # rubber
    rubber_type: str,
    rubber_qty: int,
    # correction
    corr_trim_qty: int,
    corr_wash: bool,
    corr_circle: bool,
    corr_steam: bool,
    corr_dread_qty: int,
    corr_curl_qty: int,
    corr_curl_dread_complexity: str | None,
) -> WorkFinancials:
    # Local imports to avoid circular deps with work_products.py
    from app.work_products import (  # noqa: WPS433
        _kit_work_pay_for_item,
        _rubber_pricing_from_catalog,
        _studio_share_snapshot,
        _wr_float,
        _zakaz_subcategory_services_map,
    )

    extra_costs_amount = 0.0
    studio_total = 0.0

    staff_master_profit: dict[int, float] = {uid: 0.0 for uid, _ in alloc}

    if kind == WorkKind.KIT:
        for item_key, total_qty in kit_totals.items():
            rate = _kit_work_pay_for_item(db, item_key)
            if rate <= 0:
                continue
            for uid in kit_staff_ids:
                q = int(kit_by_staff.get(uid, {}).get(item_key, 0))
                if q > 0:
                    staff_master_profit[uid] += rate * q
        if mix_source == MixSource.SELF_MIXED and grams_total > 0 and mix_complexity is not None:
            rate_map = {
                MixComplexity.SIMPLE: _wr_float(db, "mix_simple", 1.0),
                MixComplexity.MEDIUM: _wr_float(db, "mix_medium", 1.5),
                MixComplexity.HARD: _wr_float(db, "mix_hard", 2.0),
            }
            mrate = float(rate_map.get(mix_complexity, 0.0))
            mix_pay = max(0.0, float(grams_total) * mrate)
            if mix_pay > 0:
                if current_user_id in staff_master_profit:
                    staff_master_profit[current_user_id] += mix_pay
                elif kit_staff_ids:
                    share = mix_pay / float(len(kit_staff_ids))
                    for uid in kit_staff_ids:
                        staff_master_profit[uid] += share

    elif kind == WorkKind.RUBBER:
        mp, sp, fx, is_per_unit, _ul = _rubber_pricing_from_catalog(db, rubber_type)
        units = int(rubber_qty) if is_per_unit else 1
        bonus = 1.0
        if scope == WorkScope.CUSTOM_ORDER:
            bonus = max(0.0, _wr_float(db, "custom_order_bonus_multiplier", 1.0))
            if bonus <= 0:
                bonus = 1.0
        staff_master_profit[current_user_id] = float(mp) * float(units) * bonus
        studio_total = float(sp) * float(units) * bonus
        extra_costs_amount = float(fx) * float(units)

    elif kind == WorkKind.KIT_CORRECTION:
        corr_map = _zakaz_subcategory_services_map(db, "Коррекция комплекта")
        bonus = 1.0
        if scope == WorkScope.CUSTOM_ORDER:
            bonus = max(0.0, _wr_float(db, "custom_order_bonus_multiplier", 1.0))
            if bonus <= 0:
                bonus = 1.0

        def _svc_sum(name: str, units: int, *, complexity_mul: float = 1.0) -> tuple[float, float, float]:
            row = corr_map.get(name) or {}
            mp = float(row.get("master_pay") or 0.0) * float(units) * float(complexity_mul)
            sp = float(row.get("studio_pay") or 0.0) * float(units) * float(complexity_mul)
            fx = float(row.get("fixed_expense") or 0.0) * float(units)
            return mp, sp, fx

        mp_total = 0.0
        sp_total = 0.0
        fx_total = 0.0
        if corr_trim_qty > 0:
            mp, sp, fx = _svc_sum("Стрижка (1шт)", corr_trim_qty)
            mp_total += mp
            sp_total += sp
            fx_total += fx
        if corr_circle:
            mp, sp, fx = _svc_sum("Одевание на круг", 1)
            mp_total += mp
            sp_total += sp
            fx_total += fx
        if corr_wash:
            mp, sp, fx = _svc_sum("Стирка", 1)
            mp_total += mp
            sp_total += sp
            fx_total += fx
        if corr_steam:
            mp, sp, fx = _svc_sum("Отпаривание", 1)
            mp_total += mp
            sp_total += sp
            fx_total += fx
        cm_cd = 1.5 if corr_curl_dread_complexity == "HARD" else 1.0
        if corr_dread_qty > 0:
            mp, sp, fx = _svc_sum("Коррекция дреда (1шт)", corr_dread_qty, complexity_mul=cm_cd)
            mp_total += mp
            sp_total += sp
            fx_total += fx
        if corr_curl_qty > 0:
            mp, sp, fx = _svc_sum("Коррекция кудрей (1шт)", corr_curl_qty, complexity_mul=cm_cd)
            mp_total += mp
            sp_total += sp
            fx_total += fx

        staff_master_profit[current_user_id] = mp_total * bonus
        studio_total = sp_total * bonus
        extra_costs_amount = fx_total

    elif kind == WorkKind.MIX:
        rate_map = {
            MixComplexity.SIMPLE: _wr_float(db, "mix_simple", 1.0),
            MixComplexity.MEDIUM: _wr_float(db, "mix_medium", 1.5),
            MixComplexity.HARD: _wr_float(db, "mix_hard", 2.0),
        }
        rate = float(rate_map.get(mix_complexity or MixComplexity.SIMPLE, 0.0))
        staff_master_profit[current_user_id] = max(0.0, float(grams_total) * rate)

    master_total = float(sum(staff_master_profit.values()))
    studio_share = _studio_share_snapshot(db)
    if kind not in (WorkKind.RUBBER, WorkKind.KIT_CORRECTION):
        studio_total = 0.0
        if 0 < studio_share < 1 and master_total > 0:
            studio_total = master_total * (studio_share / (1.0 - studio_share))
    profit_total = master_total + studio_total
    cost_total_amount = float(mat_cost) + float(extra_costs_amount)

    return WorkFinancials(
        staff_master_profit=staff_master_profit,
        master_total=master_total,
        studio_total=studio_total,
        profit_total=profit_total,
        extra_costs_amount=extra_costs_amount,
        cost_total_amount=cost_total_amount,
        studio_share_snapshot=float(studio_share),
    )

