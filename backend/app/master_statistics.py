"""Статистика по мастеру за период (суперадмин /admin/statistics)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.client_payment import format_client_payment_kinds
from app.db.models import (
    Booking,
    Consultation,
    Kit,
    PayrollFundEntryKind,
    PayrollFundLedger,
    PayrollFundSide,
    PayrollFundSourceKind,
    ProductSale,
    ProductSaleKind,
    User,
    Visit,
    VisitKitUsage,
    VisitMastersScope,
    VisitService,
    WorkForInventory,
    WorkForInventoryStaff,
    WorkScope,
)
from app.operational_report import period_bounds
from app.payroll_fund import money_q2, sum_ledger_amounts_by_source


@dataclass
class MasterStatsVisitRow:
    visit_id: int
    visit_date: datetime
    amount_from_client: float
    payment_display: str
    discount_display: str
    cost_total: float
    masters_pay_total: float
    master_payroll: float
    studio_payroll: float


@dataclass
class MasterStatsWorkRow:
    work_id: int
    work_date: datetime
    amount_from_client: float
    payment_display: str
    discount_display: str
    cost_total: float
    master_payroll: float
    studio_payroll: float


@dataclass
class MasterStatsSaleRow:
    sale_id: int
    sale_date: datetime
    amount_from_client: float
    payment_display: str
    discount_display: str
    cost_total: float
    master_payroll: float
    studio_payroll: float


@dataclass
class MasterStatsConsultationRow:
    consultation_id: int
    consultation_date: datetime
    booking_id: int | None
    master_payroll: float


@dataclass
class MasterStatisticsResult:
    master_id: int
    master_name: str
    date_from: date
    date_to: date
    period_label: str
    payroll_accrued: float
    payouts_total: float
    fund_balance_start: float
    fund_balance_end: float
    visits: list[MasterStatsVisitRow]
    works: list[MasterStatsWorkRow]
    sales: list[MasterStatsSaleRow]
    consultations: list[MasterStatsConsultationRow]


def format_discounts(percents: list[int]) -> str:
    """Скидки: нули скрываем, если все нули — «0»."""
    if not percents:
        return "0"
    nonzero = sorted({int(p) for p in percents if int(p) != 0})
    if not nonzero:
        return "0"
    return ", ".join(str(p) for p in nonzero)


def employee_fund_balance_before(db: Session, user_id: int, before: datetime) -> float:
    v = db.scalar(
        select(func.coalesce(func.sum(PayrollFundLedger.amount), 0.0)).where(
            PayrollFundLedger.side == PayrollFundSide.MASTER,
            PayrollFundLedger.user_id == user_id,
            PayrollFundLedger.created_at < before,
        )
    )
    return money_q2(float(v or 0))


def employee_payroll_net_in_period(
    db: Session,
    user_id: int,
    start: datetime,
    end_excl: datetime,
) -> float:
    v = db.scalar(
        select(func.coalesce(func.sum(PayrollFundLedger.amount), 0.0)).where(
            PayrollFundLedger.side == PayrollFundSide.MASTER,
            PayrollFundLedger.user_id == user_id,
            PayrollFundLedger.created_at >= start,
            PayrollFundLedger.created_at < end_excl,
            PayrollFundLedger.entry_kind.in_(
                (PayrollFundEntryKind.ACCRUAL, PayrollFundEntryKind.STORNO)
            ),
        )
    )
    return money_q2(float(v or 0))


def employee_payouts_in_period(
    db: Session,
    user_id: int,
    start: datetime,
    end_excl: datetime,
) -> float:
    v = db.scalar(
        select(func.coalesce(func.sum(PayrollFundLedger.amount), 0.0)).where(
            PayrollFundLedger.entry_kind == PayrollFundEntryKind.PAYOUT,
            PayrollFundLedger.user_id == user_id,
            PayrollFundLedger.created_at >= start,
            PayrollFundLedger.created_at < end_excl,
        )
    )
    return money_q2(-float(v or 0))


def _master_on_visit_service(visit: Visit, vs: VisitService, master_id: int) -> bool:
    if visit.masters_scope == VisitMastersScope.PER_SERVICE:
        return any(int(m.master_id) == master_id for m in vs.masters)
    return any(int(m.master_id) == master_id for m in visit.masters)


def _master_services_on_visit(visit: Visit, master_id: int) -> list[VisitService]:
    return [
        vs
        for vs in visit.services
        if not vs.is_cancelled and _master_on_visit_service(visit, vs, master_id)
    ]


def _master_visit_service_pay(visit: Visit, vs: VisitService, master_id: int) -> float:
    pool = float(vs.masters_pool or 0)
    amt = 0.0
    if visit.masters_scope == VisitMastersScope.PER_SERVICE:
        for m in vs.masters:
            if int(m.master_id) == master_id:
                amt = money_q2(amt + pool * float(m.percent or 0) / 100.0)
    else:
        for m in visit.masters:
            if int(m.master_id) == master_id:
                amt = money_q2(amt + pool * float(m.percent or 0) / 100.0)
    if vs.mix_bonus_master_id and int(vs.mix_bonus_master_id) == master_id:
        amt = money_q2(amt + float(vs.mix_bonus_amount or 0))
    return amt


def _visit_studio_pay(vs: VisitService) -> float:
    return money_q2(float(vs.salon_profit or 0) + float(vs.studio_fund_amount or 0))


def _visit_masters_pay_total(visit: Visit) -> float:
    """Суммарная ЗП всех мастеров по визиту: пул + бонусы за смешку."""
    total = 0.0
    for vs in visit.services or []:
        if vs.is_cancelled:
            continue
        total = money_q2(total + float(vs.masters_pool or 0) + float(vs.mix_bonus_amount or 0))
    return total


def _kit_studio_margin_from_visit_usage(db: Session, kit: Kit, usage: VisitKitUsage) -> float:
    total_pieces = max(int(kit.pieces_total or 0), 1)
    k = float(usage.pieces_used or 0) / float(total_pieces)
    cost_portion = float(kit.cost_total or 0) * k
    return money_q2(max(0.0, float(usage.cost_amount or 0) - cost_portion))


def _work_studio_payroll_sold(db: Session, work: WorkForInventory) -> float:
    if work.scope == WorkScope.CUSTOM_ORDER:
        return money_q2(float(work.studio_profit_amount or 0))
    kid = work.created_kit_id
    if not kid:
        return 0.0
    total = 0.0
    kit = db.get(Kit, int(kid))
    for sale in db.scalars(
        select(ProductSale).where(
            ProductSale.kit_id == int(kid),
            ProductSale.is_voided.is_(False),
        )
    ).all():
        total = money_q2(total + float(sale.studio_margin_amount or 0))
    if kit:
        for usage in db.scalars(select(VisitKitUsage).where(VisitKitUsage.kit_id == int(kid))).all():
            total = money_q2(total + _kit_studio_margin_from_visit_usage(db, kit, usage))
    return money_q2(total)


def _work_event_bounds(d0: date, d1: date) -> tuple[datetime, datetime]:
    start = datetime.combine(d0, time.min)
    end_excl = datetime.combine(d1 + timedelta(days=1), time.min)
    return start, end_excl


def _work_in_period(w: WorkForInventory, start: datetime, end_excl: datetime) -> bool:
    ev = w.performed_date or w.created_at
    return ev >= start and ev < end_excl


def _sale_discount_display(db: Session, sale: ProductSale) -> str:
    if sale.kind == ProductSaleKind.KIT and sale.kit_id:
        kit = db.get(Kit, int(sale.kit_id))
        if kit:
            return format_discounts([int(kit.discount_percent or 0)])
    return "0"


def _sale_cost_total(sale: ProductSale) -> float:
    amt = float(sale.amount_from_client or 0)
    margin = float(sale.studio_margin_amount or 0)
    return money_q2(max(0.0, amt - margin))


def _consultation_master_payroll(db: Session, consultation_id: int, master_id: int) -> float:
    by_src = sum_ledger_amounts_by_source(
        db,
        side=PayrollFundSide.MASTER,
        source_kind=PayrollFundSourceKind.CONSULTATION,
        source_ids=[consultation_id],
        user_id=master_id,
    )
    return money_q2(by_src.get(consultation_id, 0.0))


def build_master_statistics(db: Session, master_id: int, d0: date, d1: date) -> MasterStatisticsResult | None:
    master = db.get(User, master_id)
    if master is None:
        return None

    start, end_excl = period_bounds(d0, d1)
    visit_start = datetime.combine(d0, time.min)
    visit_end = datetime.combine(d1 + timedelta(days=1), time.min)
    label = f"{d0.isoformat()} — {d1.isoformat()}"

    payroll_accrued = employee_payroll_net_in_period(db, master_id, start, end_excl)
    payouts_total = employee_payouts_in_period(db, master_id, start, end_excl)
    fund_balance_start = employee_fund_balance_before(db, master_id, start)
    fund_balance_end = employee_fund_balance_before(db, master_id, end_excl)

    visits_out: list[MasterStatsVisitRow] = []
    visits = list(
        db.scalars(
            select(Visit)
            .options(
                selectinload(Visit.services).selectinload(VisitService.masters),
                selectinload(Visit.masters),
            )
            .where(
                Visit.is_cancelled.is_(False),
                Visit.performed_date >= visit_start,
                Visit.performed_date < visit_end,
            )
            .order_by(Visit.performed_date.desc(), Visit.id.desc())
        )
        .unique()
        .all()
    )
    for visit in visits:
        svc_lines = _master_services_on_visit(visit, master_id)
        if not svc_lines:
            continue
        visits_out.append(
            MasterStatsVisitRow(
                visit_id=int(visit.id),
                visit_date=visit.performed_date,
                amount_from_client=money_q2(sum(float(vs.amount_from_client or 0) for vs in svc_lines)),
                payment_display=format_client_payment_kinds(
                    vs.client_payment_kind
                    for vs in svc_lines
                    if float(vs.amount_from_client or 0) > 0
                ),
                discount_display=format_discounts([int(vs.client_discount_percent or 0) for vs in svc_lines]),
                cost_total=money_q2(sum(float(vs.cost_total or 0) for vs in svc_lines)),
                masters_pay_total=_visit_masters_pay_total(visit),
                master_payroll=money_q2(
                    sum(_master_visit_service_pay(visit, vs, master_id) for vs in svc_lines)
                ),
                studio_payroll=money_q2(sum(_visit_studio_pay(vs) for vs in svc_lines)),
            )
        )

    works_out: list[MasterStatsWorkRow] = []
    work_start, work_end = _work_event_bounds(d0, d1)
    works = list(
        db.scalars(
            select(WorkForInventory)
            .options(selectinload(WorkForInventory.staff_rows))
            .where(WorkForInventory.is_voided.is_(False))
            .order_by(WorkForInventory.created_at.desc(), WorkForInventory.id.desc())
        )
        .unique()
        .all()
    )
    for work in works:
        if not _work_in_period(work, work_start, work_end):
            continue
        staff = next((s for s in work.staff_rows if int(s.user_id) == master_id), None)
        if staff is None:
            continue
        discount = "0"
        if work.created_kit_id:
            kit = db.get(Kit, int(work.created_kit_id))
            if kit:
                discount = format_discounts([int(kit.discount_percent or 0)])
        works_out.append(
            MasterStatsWorkRow(
                work_id=int(work.id),
                work_date=work.performed_date or work.created_at,
                amount_from_client=money_q2(float(work.amount_from_client or 0)),
                payment_display=(
                    format_client_payment_kinds([work.client_payment_kind])
                    if work.amount_from_client is not None and float(work.amount_from_client or 0) > 0
                    else "—"
                ),
                discount_display=discount,
                cost_total=money_q2(float(work.cost_total_amount or 0)),
                master_payroll=money_q2(float(staff.master_profit_amount or 0)),
                studio_payroll=_work_studio_payroll_sold(db, work),
            )
        )

    sales_out: list[MasterStatsSaleRow] = []
    sales = list(
        db.scalars(
            select(ProductSale)
            .where(
                ProductSale.is_voided.is_(False),
                ProductSale.performed_date >= visit_start,
                ProductSale.performed_date < visit_end,
                or_(
                    ProductSale.created_by_user_id == master_id,
                    ProductSale.material_mix_bonus_user_id == master_id,
                ),
            )
            .order_by(ProductSale.performed_date.desc(), ProductSale.id.desc())
        ).all()
    )
    for sale in sales:
        master_pay = 0.0
        if sale.material_mix_bonus_user_id and int(sale.material_mix_bonus_user_id) == master_id:
            master_pay = money_q2(float(sale.material_mix_bonus_amount or 0))
        sales_out.append(
            MasterStatsSaleRow(
                sale_id=int(sale.id),
                sale_date=sale.performed_date,
                amount_from_client=money_q2(float(sale.amount_from_client or 0)),
                payment_display=(
                    format_client_payment_kinds([sale.client_payment_kind])
                    if float(sale.amount_from_client or 0) > 0
                    else "—"
                ),
                discount_display=_sale_discount_display(db, sale),
                cost_total=_sale_cost_total(sale),
                master_payroll=master_pay,
                studio_payroll=money_q2(float(sale.studio_margin_amount or 0)),
            )
        )

    consultations_out: list[MasterStatsConsultationRow] = []
    consultations = list(
        db.scalars(
            select(Consultation)
            .where(
                Consultation.created_by_user_id == master_id,
                Consultation.consultation_date >= visit_start,
                Consultation.consultation_date < visit_end,
            )
            .order_by(Consultation.consultation_date.desc(), Consultation.id.desc())
        ).all()
    )
    for cons in consultations:
        booking_id = db.scalar(
            select(Booking.id).where(Booking.consultation_id == cons.id).limit(1)
        )
        consultations_out.append(
            MasterStatsConsultationRow(
                consultation_id=int(cons.id),
                consultation_date=cons.consultation_date,
                booking_id=int(booking_id) if booking_id is not None else None,
                master_payroll=_consultation_master_payroll(db, int(cons.id), master_id),
            )
        )

    return MasterStatisticsResult(
        master_id=master_id,
        master_name=master.display_name,
        date_from=d0,
        date_to=d1,
        period_label=label,
        payroll_accrued=payroll_accrued,
        payouts_total=payouts_total,
        fund_balance_start=fund_balance_start,
        fund_balance_end=fund_balance_end,
        visits=visits_out,
        works=works_out,
        sales=sales_out,
        consultations=consultations_out,
    )
