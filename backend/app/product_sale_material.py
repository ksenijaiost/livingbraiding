"""Розница материала: снимки цен, смешка, себестоимость, флаг проверки."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.db.models import (
    MaterialPriceCurrent,
    MaterialType,
    MixComplexity,
    MixSource,
    ProductSale,
    ProductSaleKind,
    Service,
    UserRole,
)
from app.mix_rates import mix_complexity_rate_for
from app.payroll_fund import money_q2


def material_retail_has_pricing_path(svc: Service) -> bool:
    return bool(svc.retail_material_kanekalon or svc.retail_material_kudri or svc.retail_material_mix)


def material_cost_review_pending_for_sale(sale: ProductSale, svc: Service | None) -> bool:
    if sale.kind != ProductSaleKind.MATERIAL or svc is None:
        return False
    if material_retail_has_pricing_path(svc):
        return False
    return sale.material_manual_cost is None


def retail_mix_grams_total(
    svc: Service,
    *,
    g_k: float | None,
    g_ku: float | None,
    g_standalone: float | None,
    g_legacy: float | None,
) -> float:
    if svc.retail_material_kanekalon or svc.retail_material_kudri:
        a = max(0.0, float(g_k or 0.0)) if svc.retail_material_kanekalon else 0.0
        b = max(0.0, float(g_ku or 0.0)) if svc.retail_material_kudri else 0.0
        return money_q2(a + b)
    return money_q2(max(0.0, float(g_standalone or g_legacy or 0.0)))


def apply_price_snapshots(db: Session, sale: ProductSale, svc: Service | None) -> None:
    if sale.kind != ProductSaleKind.MATERIAL:
        return
    pk = db.get(MaterialPriceCurrent, MaterialType.KANEKALON)
    ku = db.get(MaterialPriceCurrent, MaterialType.KUDRI)
    if svc and svc.retail_material_kanekalon:
        sale.material_kanekalon_price_per_gram_at_time = float(pk.price_per_gram) if pk else None
    else:
        sale.material_kanekalon_price_per_gram_at_time = None
    if svc and svc.retail_material_kudri:
        sale.material_kudri_price_per_gram_at_time = float(ku.price_per_gram) if ku else None
    else:
        sale.material_kudri_price_per_gram_at_time = None


def material_sale_goods_cost(sale: ProductSale) -> float:
    """Себестоимость материала для розницы (граммы×цены + ручная + смешка)."""
    if sale.kind != ProductSaleKind.MATERIAL:
        return 0.0
    svc = sale.material_service
    cost = 0.0
    if svc is None:
        grams = float(sale.material_grams or 0.0)
        price_g = float(sale.material_kanekalon_price_per_gram_at_time or 0.0)
        return money_q2(grams * price_g)

    if svc.retail_material_kanekalon:
        g = float(sale.material_kanekalon_grams or 0.0)
        p = sale.material_kanekalon_price_per_gram_at_time
        if p is not None:
            cost += g * float(p)
    if svc.retail_material_kudri:
        g = float(sale.material_kudri_grams or 0.0)
        p = sale.material_kudri_price_per_gram_at_time
        if p is not None:
            cost += g * float(p)

    if not material_retail_has_pricing_path(svc):
        mc = sale.material_manual_cost
        if mc is not None:
            cost += float(mc)
    elif not (svc.retail_material_kanekalon or svc.retail_material_kudri) and svc.retail_material_mix:
        mc = sale.material_manual_cost
        if mc is not None:
            cost += float(mc)

    cost += float(sale.material_mix_cost_amount or 0.0)
    return money_q2(cost)


def finalize_material_sale_fields(
    db: Session,
    sale: ProductSale,
    *,
    seller_user_id: int,
    active_role: UserRole,
) -> None:
    """Заполняет поля смешки, снимки цен, флаги проверки и маржу студии (без commit)."""
    if sale.kind != ProductSaleKind.MATERIAL:
        sale.material_cost_review_pending = False
        sale.material_mix_cost_amount = 0.0
        sale.material_mix_bonus_user_id = None
        sale.material_mix_bonus_amount = 0.0
        return

    svc = sale.material_service
    if svc is None:
        # Легаси: одно поле грамм → канекалон по текущей цене.
        sale.material_kanekalon_grams = None
        sale.material_kudri_grams = None
        sale.material_manual_cost = None
        sale.material_mix_source = None
        sale.material_mix_complexity = None
        sale.material_mix_standalone_grams = None
        sale.material_mix_cost_amount = 0.0
        sale.material_mix_bonus_user_id = None
        sale.material_mix_bonus_amount = 0.0
        pk = db.get(MaterialPriceCurrent, MaterialType.KANEKALON)
        sale.material_kanekalon_price_per_gram_at_time = float(pk.price_per_gram) if pk else None
        sale.material_kudri_price_per_gram_at_time = None
        sale.material_cost_review_pending = False
        amt = float(sale.amount_from_client or 0)
        cost = material_sale_goods_cost(sale)
        sale.studio_margin_amount = money_q2(max(0.0, amt - cost))
        return

    apply_price_snapshots(db, sale, svc)

    # Смешка: источник для админа только из наличия.
    mix_src = sale.material_mix_source
    if svc.retail_material_mix:
        if active_role in (UserRole.ADMIN, UserRole.ADMIN_SENIOR, UserRole.ADMIN_SUPER):
            mix_src = MixSource.FROM_STOCK
            sale.material_mix_source = mix_src
        if mix_src and mix_src != MixSource.NO_MIX:
            comp = sale.material_mix_complexity
            if comp is None:
                raise ValueError("Укажите сложность смешки.")
            grams_t = retail_mix_grams_total(
                svc,
                g_k=sale.material_kanekalon_grams,
                g_ku=sale.material_kudri_grams,
                g_standalone=sale.material_mix_standalone_grams,
                g_legacy=sale.material_grams,
            )
            if grams_t <= 0:
                raise ValueError("Для смешки укажите граммы (по полям канекалона/кудрей или отдельное поле).")
            coef = mix_complexity_rate_for(db, comp)
            mix_cost = money_q2(grams_t * coef)
            sale.material_mix_cost_amount = mix_cost
            if mix_src == MixSource.SELF_MIXED:
                sale.material_mix_bonus_amount = mix_cost
                sale.material_mix_bonus_user_id = seller_user_id
            else:
                sale.material_mix_bonus_amount = 0.0
                sale.material_mix_bonus_user_id = None
        else:
            sale.material_mix_cost_amount = 0.0
            sale.material_mix_bonus_amount = 0.0
            sale.material_mix_bonus_user_id = None
    else:
        sale.material_mix_source = None
        sale.material_mix_complexity = None
        sale.material_mix_standalone_grams = None
        sale.material_mix_cost_amount = 0.0
        sale.material_mix_bonus_amount = 0.0
        sale.material_mix_bonus_user_id = None

    sale.material_cost_review_pending = material_cost_review_pending_for_sale(sale, svc)

    amt = float(sale.amount_from_client or 0)
    cost = material_sale_goods_cost(sale)

    if sale.material_cost_review_pending:
        sale.studio_margin_amount = 0.0
    else:
        # Legacy-снимок без процента; при sale_percent пересчитает compute_product_sale_studio_margin.
        sale.studio_margin_amount = money_q2(max(0.0, amt - cost))
