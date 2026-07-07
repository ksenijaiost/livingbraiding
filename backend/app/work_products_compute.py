from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.db.models import MixComplexity, MixSource, WorkKind, WorkScope
from app.mix_rates import mix_complexity_rate_map
from app.work_rate_keys import CUSTOM_ORDER_BONUS_MULTIPLIER

# Подкатегория «Коррекция комплекта» в каталоге «Заказ»
CORR_SVC_TRIM = "Стрижка (1шт)"
CORR_SVC_WASH_WITH = "Стирка (с коррекцией)"
CORR_SVC_WASH_WITHOUT = "Стирка (без коррекции)"
CORR_SVC_HOURLY = "Почасовая коррекция заготовок (1 ч)"
CORR_SVC_CIRCLE = "Одевание на круг"
CORR_SVC_STEAM = "Отпаривание"


def corr_wash_catalog_name(*, trim_qty: int, hourly_hours: float, hourly_avg: bool) -> str:
    """Стирка в прайсе: «с коррекцией», если есть стрижка/часы/ориентир по часам; иначе «без коррекции»."""
    if trim_qty > 0 or hourly_hours > 0 or hourly_avg:
        return CORR_SVC_WASH_WITH
    return CORR_SVC_WASH_WITHOUT


def corr_hourly_pay_units(*, hourly_hours: float, hourly_avg: bool) -> float:
    """Для ЗП/доп. расходов: при ориентире 1–4 ч без ввода часов берём 2.5 ч."""
    h = max(0.0, float(hourly_hours))
    if hourly_avg and h <= 0:
        return 2.5
    return h


def compute_correction_extra_costs(
    db: Session,
    *,
    corr_trim_qty: int,
    corr_hourly_hours: float,
    corr_hourly_avg: bool,
    corr_wash: bool,
    corr_circle: bool,
    corr_steam: bool,
) -> float:
    """Себестоимость коррекции: доп. расходы (fixed_expense) по прайсу «Коррекция комплекта»."""
    from app.work_products import _zakaz_subcategory_services_map  # noqa: WPS433

    corr_map = _zakaz_subcategory_services_map(db, "Коррекция комплекта")

    def _fx_sum(name: str, units: float) -> float:
        row = corr_map.get(name) or {}
        return float(row.get("fixed_expense") or 0.0) * max(0.0, float(units))

    fx_total = 0.0
    if corr_trim_qty > 0:
        fx_total += _fx_sum(CORR_SVC_TRIM, float(corr_trim_qty))
    hh_pay = corr_hourly_pay_units(hourly_hours=corr_hourly_hours, hourly_avg=corr_hourly_avg)
    if hh_pay > 0:
        fx_total += _fx_sum(CORR_SVC_HOURLY, hh_pay)
    if corr_circle:
        fx_total += _fx_sum(CORR_SVC_CIRCLE, 1)
    if corr_wash:
        wash_nm = corr_wash_catalog_name(
            trim_qty=int(corr_trim_qty),
            hourly_hours=float(corr_hourly_hours),
            hourly_avg=bool(corr_hourly_avg),
        )
        fx_total += _fx_sum(wash_nm, 1)
    if corr_steam:
        fx_total += _fx_sum(CORR_SVC_STEAM, 1)
    return fx_total


def resolve_kit_client_price(
    *,
    scope: WorkScope,
    catalog_client_price: float,
    amount_from_client: float | None = None,
) -> float:
    """Цена для расчёта профита комплекта: на заказ — своя сумма с клиента, иначе прайс."""
    if scope == WorkScope.CUSTOM_ORDER and amount_from_client is not None:
        afc = float(amount_from_client)
        if afc > 0:
            return afc
    return float(catalog_client_price)


def kit_studio_profit_amount(
    *,
    scope: WorkScope,
    client_price: float,
    cost_total: float,
    master_total: float,
) -> float:
    """ЗП студии по комплекту: цена − себестоимость − ЗП мастера."""
    if scope == WorkScope.IN_STOCK:
        return 0.0
    return float(client_price) - float(cost_total) - float(master_total)


def split_profit_from_client_amount(
    amount_from_client: float,
    cost_total: float,
    salon_cut_pct: float,
) -> tuple[float, float, float]:
    """Прибыль и деление между студией и мастерами (как в услугах визита)."""
    profit = float(amount_from_client) - float(cost_total)
    salon_profit = profit * float(salon_cut_pct)
    masters_pool = profit - salon_profit
    return profit, masters_pool, salon_profit


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
    # other (catalog)
    other_catalog_product_id: int = 0,
    other_qty: int = 1,
    # correction
    corr_trim_qty: int,
    corr_hourly_hours: float,
    corr_hourly_avg: bool,
    corr_wash: bool,
    corr_circle: bool,
    corr_steam: bool,
    composition_lines: list[Any] | None = None,
    kit_client_price: float | None = None,
    amount_from_client: float | None = None,
) -> WorkFinancials:
    # Local imports to avoid circular deps with work_products.py
    from app.work_products import (  # noqa: WPS433
        _kit_work_pay_for_item,
        _rubber_pricing_from_catalog,
        _other_pricing_from_catalog,
        _studio_share_snapshot,
        _wr_float,
        _zakaz_subcategory_services_map,
    )

    extra_costs_amount = 0.0
    studio_total = 0.0

    staff_master_profit: dict[int, float] = {uid: 0.0 for uid, _ in alloc}

    if kind == WorkKind.KIT:
        if composition_lines:
            from app.kit_composition_lines import work_pay_for_lines

            pay_map = work_pay_for_lines(db, composition_lines)
            for uid in kit_staff_ids:
                staff_master_profit[uid] = float(pay_map.get(uid, 0.0))
        else:
            for item_key, total_qty in kit_totals.items():
                rate = _kit_work_pay_for_item(db, item_key)
                if rate <= 0:
                    continue
                for uid in kit_staff_ids:
                    q = int(kit_by_staff.get(uid, {}).get(item_key, 0))
                    if q > 0:
                        staff_master_profit[uid] += rate * q
        if mix_source == MixSource.SELF_MIXED and grams_total > 0 and mix_complexity is not None:
            mrate = float(mix_complexity_rate_map(db).get(mix_complexity, 0.0))
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
            bonus = max(0.0, _wr_float(db, CUSTOM_ORDER_BONUS_MULTIPLIER, 1.0))
            if bonus <= 0:
                bonus = 1.0
        staff_master_profit[current_user_id] = float(mp) * float(units) * bonus
        studio_total = float(sp) * float(units) * bonus
        extra_costs_amount = float(fx) * float(units)

    elif kind == WorkKind.KIT_CORRECTION:
        corr_map = _zakaz_subcategory_services_map(db, "Коррекция комплекта")
        bonus = 1.0
        if scope == WorkScope.CUSTOM_ORDER:
            bonus = max(0.0, _wr_float(db, CUSTOM_ORDER_BONUS_MULTIPLIER, 1.0))
            if bonus <= 0:
                bonus = 1.0

        def _svc_sum(name: str, units: float) -> tuple[float, float, float]:
            row = corr_map.get(name) or {}
            u = max(0.0, float(units))
            mp = float(row.get("master_pay") or 0.0) * u
            sp = float(row.get("studio_pay") or 0.0) * u
            fx = float(row.get("fixed_expense") or 0.0) * u
            return mp, sp, fx

        mp_total = 0.0
        sp_total = 0.0
        fx_total = 0.0
        if corr_trim_qty > 0:
            mp, sp, fx = _svc_sum(CORR_SVC_TRIM, float(corr_trim_qty))
            mp_total += mp
            sp_total += sp
            fx_total += fx
        hh_pay = corr_hourly_pay_units(hourly_hours=corr_hourly_hours, hourly_avg=corr_hourly_avg)
        if hh_pay > 0:
            mp, sp, fx = _svc_sum(CORR_SVC_HOURLY, hh_pay)
            mp_total += mp
            sp_total += sp
            fx_total += fx
        if corr_circle:
            mp, sp, fx = _svc_sum(CORR_SVC_CIRCLE, 1)
            mp_total += mp
            sp_total += sp
            fx_total += fx
        if corr_wash:
            wash_nm = corr_wash_catalog_name(
                trim_qty=int(corr_trim_qty),
                hourly_hours=float(corr_hourly_hours),
                hourly_avg=bool(corr_hourly_avg),
            )
            mp, sp, fx = _svc_sum(wash_nm, 1)
            mp_total += mp
            sp_total += sp
            fx_total += fx
        if corr_steam:
            mp, sp, fx = _svc_sum(CORR_SVC_STEAM, 1)
            mp_total += mp
            sp_total += sp
            fx_total += fx

        staff_master_profit[current_user_id] = mp_total * bonus
        studio_total = sp_total * bonus
        extra_costs_amount = fx_total

    elif kind == WorkKind.MIX:
        rate = float(
            mix_complexity_rate_map(db).get(mix_complexity or MixComplexity.STANDARD, 0.0)
        )
        staff_master_profit[current_user_id] = max(0.0, float(grams_total) * rate)

    elif kind == WorkKind.OTHER:
        mp, sp, fx, is_per_unit, _ul = _other_pricing_from_catalog(db, other_catalog_product_id)
        units = int(other_qty) if is_per_unit else 1
        bonus = 1.0
        if scope == WorkScope.CUSTOM_ORDER:
            bonus = max(0.0, _wr_float(db, CUSTOM_ORDER_BONUS_MULTIPLIER, 1.0))
            if bonus <= 0:
                bonus = 1.0
        staff_master_profit[current_user_id] = float(mp) * float(units) * bonus
        studio_total = float(sp) * float(units) * bonus
        extra_costs_amount = float(fx) * float(units)

        if (
            mix_source == MixSource.SELF_MIXED
            and grams_total > 0
            and mix_complexity is not None
        ):
            rate = float(mix_complexity_rate_map(db).get(mix_complexity, 0.0))
            staff_master_profit[current_user_id] += max(0.0, float(grams_total) * rate)

    master_total = float(sum(staff_master_profit.values()))
    cost_total_amount = float(mat_cost) + float(extra_costs_amount)
    studio_share = _studio_share_snapshot(db)
    if kind == WorkKind.KIT:
        price = resolve_kit_client_price(
            scope=scope,
            catalog_client_price=float(kit_client_price or 0.0),
            amount_from_client=amount_from_client,
        )
        studio_total = kit_studio_profit_amount(
            scope=scope,
            client_price=price,
            cost_total=cost_total_amount,
            master_total=master_total,
        )
    elif kind not in (WorkKind.RUBBER, WorkKind.KIT_CORRECTION):
        studio_total = 0.0
        if scope != WorkScope.IN_STOCK and 0 < studio_share < 1 and master_total > 0:
            studio_total = master_total * (studio_share / (1.0 - studio_share))
    profit_total = master_total + studio_total

    return WorkFinancials(
        staff_master_profit=staff_master_profit,
        master_total=master_total,
        studio_total=studio_total,
        profit_total=profit_total,
        extra_costs_amount=extra_costs_amount,
        cost_total_amount=cost_total_amount,
        studio_share_snapshot=float(studio_share),
    )

