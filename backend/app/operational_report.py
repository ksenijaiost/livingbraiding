"""Операционный отчёт за период (срез по дате события + блок журнала ЗП по effective_at)."""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, aliased, selectinload

from app.db.models import (
    Consultation,
    HourlyWorkEntry,
    MaterialPriceCurrent,
    MaterialType,
    PayrollFundEntryKind,
    PayrollFundLedger,
    PayrollFundSourceKind,
    PayrollPeriod,
    ProductSale,
    ProductSaleKind,
    Service,
    StudioExpense,
    User,
    Visit,
    VisitService,
    WorkForInventory,
    WorkScope,
)
from app.payroll_fund import PAYROLL_FUND_SOURCE_KIND_RU, money_q2
from app.ui_visit_display import visit_masters_fund_by_master, visit_masters_fund_total


@dataclass(frozen=True)
class ReportFundCompareRow:
    """Строка сверки: фонды отчёт/журнал + суммы с клиента (шапка vs карточка)."""

    entity: Any
    entity_id: int
    event_at: datetime | None
    open_url: str
    note: str | None
    amount_ops: float
    amount_ledger: float
    amount_mismatch: bool
    client_amount_header: float | None = None
    client_amount_card: float | None = None
    client_amount_mismatch: bool = False

    @property
    def any_mismatch(self) -> bool:
        return self.amount_mismatch or self.client_amount_mismatch


def _fund_mismatch(ops: float, ledger: float) -> bool:
    return money_q2(ops) != money_q2(ledger)


def _client_amounts_mismatch(a: float | None, b: float | None) -> bool:
    if a is None and b is None:
        return False
    if a is None or b is None:
        return True
    return money_q2(a) != money_q2(b)


def _visit_services_amount_from_client(visit: Visit) -> float:
    """Как «Итого с клиента» в карточке: сумма активных строк услуг."""
    return money_q2(
        sum(
            float(s.amount_from_client or 0)
            for s in (visit.services or [])
            if not s.is_cancelled
        )
    )


def _sort_fund_rows(rows: list[ReportFundCompareRow]) -> list[ReportFundCompareRow]:
    """Сначала любые расхождения, затем по дате события (новые сверху)."""

    def key(r: ReportFundCompareRow) -> tuple:
        ts = r.event_at.timestamp() if r.event_at is not None else 0.0
        return (not r.any_mismatch, -ts, -r.entity_id)

    return sorted(rows, key=key)


# Сторно расхода (EXPENSE) не должно попадать в «начисления+сторно» сверки.
_LedgerOrig = aliased(PayrollFundLedger)
_ACCRUAL_OR_ACCRUAL_STORNO = or_(
    PayrollFundLedger.entry_kind == PayrollFundEntryKind.ACCRUAL,
    and_(
        PayrollFundLedger.entry_kind == PayrollFundEntryKind.STORNO,
        _LedgerOrig.entry_kind == PayrollFundEntryKind.ACCRUAL,
    ),
)


def _accrual_net_by_source_ids(
    db: Session,
    *,
    source_kind: PayrollFundSourceKind,
    source_ids: list[int],
    start: datetime,
    end_excl: datetime,
) -> dict[int, float]:
    """Нетто ACCRUAL + сторно начислений по source_id за период (обе стороны фондов)."""
    if not source_ids:
        return {}
    stmt = (
        select(PayrollFundLedger.source_id, func.coalesce(func.sum(PayrollFundLedger.amount), 0.0))
        .select_from(PayrollFundLedger)
        .outerjoin(_LedgerOrig, PayrollFundLedger.storno_of_id == _LedgerOrig.id)
        .where(
            PayrollFundLedger.effective_at >= start,
            PayrollFundLedger.effective_at < end_excl,
            _ACCRUAL_OR_ACCRUAL_STORNO,
            PayrollFundLedger.source_kind == source_kind,
            PayrollFundLedger.source_id.in_(source_ids),
        )
        .group_by(PayrollFundLedger.source_id)
    )
    out: dict[int, float] = {}
    for sid, amt in db.execute(stmt).all():
        if sid is None:
            continue
        out[int(sid)] = money_q2(float(amt or 0))
    return out


def _visit_accrual_net_by_visit_id(
    db: Session,
    *,
    visit_ids: list[int],
    start: datetime,
    end_excl: datetime,
) -> dict[int, float]:
    """Нетто начислений по визиту: legacy VISIT + VISIT_SERVICE за период."""
    if not visit_ids:
        return {}
    out = _accrual_net_by_source_ids(
        db,
        source_kind=PayrollFundSourceKind.VISIT,
        source_ids=visit_ids,
        start=start,
        end_excl=end_excl,
    )
    stmt = (
        select(VisitService.visit_id, func.coalesce(func.sum(PayrollFundLedger.amount), 0.0))
        .select_from(PayrollFundLedger)
        .join(VisitService, VisitService.id == PayrollFundLedger.source_id)
        .outerjoin(_LedgerOrig, PayrollFundLedger.storno_of_id == _LedgerOrig.id)
        .where(
            PayrollFundLedger.effective_at >= start,
            PayrollFundLedger.effective_at < end_excl,
            _ACCRUAL_OR_ACCRUAL_STORNO,
            PayrollFundLedger.source_kind == PayrollFundSourceKind.VISIT_SERVICE,
            VisitService.visit_id.in_(visit_ids),
        )
        .group_by(VisitService.visit_id)
    )
    for vid, amt in db.execute(stmt).all():
        if vid is None:
            continue
        k = int(vid)
        out[k] = money_q2(out.get(k, 0.0) + float(amt or 0))
    return out


def _visit_ids_with_accrual_in_period(db: Session, start: datetime, end_excl: datetime) -> set[int]:
    legacy = db.scalars(
        select(PayrollFundLedger.source_id)
        .select_from(PayrollFundLedger)
        .outerjoin(_LedgerOrig, PayrollFundLedger.storno_of_id == _LedgerOrig.id)
        .where(
            PayrollFundLedger.effective_at >= start,
            PayrollFundLedger.effective_at < end_excl,
            _ACCRUAL_OR_ACCRUAL_STORNO,
            PayrollFundLedger.source_kind == PayrollFundSourceKind.VISIT,
            PayrollFundLedger.source_id.is_not(None),
        )
        .distinct()
    ).all()
    from_services = db.scalars(
        select(VisitService.visit_id)
        .select_from(PayrollFundLedger)
        .join(VisitService, VisitService.id == PayrollFundLedger.source_id)
        .outerjoin(_LedgerOrig, PayrollFundLedger.storno_of_id == _LedgerOrig.id)
        .where(
            PayrollFundLedger.effective_at >= start,
            PayrollFundLedger.effective_at < end_excl,
            _ACCRUAL_OR_ACCRUAL_STORNO,
            PayrollFundLedger.source_kind == PayrollFundSourceKind.VISIT_SERVICE,
        )
        .distinct()
    ).all()
    out: set[int] = set()
    for sid in legacy:
        if sid is not None:
            out.add(int(sid))
    for vid in from_services:
        if vid is not None:
            out.add(int(vid))
    return out


def _source_ids_with_accrual_in_period(
    db: Session,
    *,
    source_kind: PayrollFundSourceKind,
    start: datetime,
    end_excl: datetime,
) -> set[int]:
    rows = db.scalars(
        select(PayrollFundLedger.source_id)
        .select_from(PayrollFundLedger)
        .outerjoin(_LedgerOrig, PayrollFundLedger.storno_of_id == _LedgerOrig.id)
        .where(
            PayrollFundLedger.effective_at >= start,
            PayrollFundLedger.effective_at < end_excl,
            _ACCRUAL_OR_ACCRUAL_STORNO,
            PayrollFundLedger.source_kind == source_kind,
            PayrollFundLedger.source_id.is_not(None),
        )
        .distinct()
    ).all()
    return {int(sid) for sid in rows if sid is not None}


def _visit_ops_funds(visit: Visit) -> float:
    """Фонды визита: студия + ЗП мастеров (как в карточке / VISIT_SERVICE), вкл. PER_SERVICE."""
    studio = money_q2(float(visit.salon_profit or 0) + float(visit.studio_fund_amount or 0))
    return money_q2(studio + visit_masters_fund_total(visit))


def _work_ops_funds(work: WorkForInventory) -> float:
    studio = 0.0
    if work.scope != WorkScope.IN_STOCK:
        studio = money_q2(float(work.studio_profit_amount or 0))
    masters = money_q2(sum(float(s.master_profit_amount or 0) for s in (work.staff_rows or [])))
    return money_q2(studio + masters)


def _sale_ops_funds(sale: ProductSale) -> float:
    return money_q2(float(sale.studio_margin_amount or 0))


def _hourly_ops_funds(entry: HourlyWorkEntry) -> float:
    a = money_q2(float(entry.amount or 0))
    return a if a > 0 else 0.0


def _visit_note(visit: Visit, *, in_ops_slice: bool) -> str | None:
    parts: list[str] = []
    if visit.is_cancelled:
        parts.append("отменён")
    if not in_ops_slice:
        parts.append("только журнал / вне среза отчёта")
    return "; ".join(parts) if parts else None


def _void_note(is_voided: bool, *, in_ops_slice: bool, void_label: str = "аннулирован") -> str | None:
    parts: list[str] = []
    if is_voided:
        parts.append(void_label)
    if not in_ops_slice:
        parts.append("только журнал / вне среза отчёта")
    return "; ".join(parts) if parts else None


@dataclass(frozen=True)
class OrphanVisitLedgerSummary:
    """Проводки VISIT / VISIT_SERVICE за период без живой карточки услуги/визита."""

    net_amount: float
    source_count: int
    details: tuple[tuple[str, int, float], ...]  # (VISIT|VISIT_SERVICE, source_id, net)


def _orphan_source_ids_for_kind(
    db: Session,
    *,
    source_kind: PayrollFundSourceKind,
    start: datetime,
    end_excl: datetime,
    existing_ids_subq,
) -> list[int]:
    """source_id с начисл./сторно начисл. в периоде, которых нет в existing_ids_subq."""
    rows = db.scalars(
        select(PayrollFundLedger.source_id)
        .select_from(PayrollFundLedger)
        .outerjoin(_LedgerOrig, PayrollFundLedger.storno_of_id == _LedgerOrig.id)
        .where(
            PayrollFundLedger.effective_at >= start,
            PayrollFundLedger.effective_at < end_excl,
            _ACCRUAL_OR_ACCRUAL_STORNO,
            PayrollFundLedger.source_kind == source_kind,
            PayrollFundLedger.source_id.is_not(None),
            ~PayrollFundLedger.source_id.in_(existing_ids_subq),
        )
        .distinct()
    ).all()
    return sorted({int(sid) for sid in rows if sid is not None})


def summarize_orphan_visit_ledger(db: Session, d0: date, d1: date) -> OrphanVisitLedgerSummary:
    """Нетто «Визит ?» / удалённых услуг за период — то, что не попадает в строки списка сверки."""
    start, end_excl = period_bounds(d0, d1)
    visit_ids_subq = select(Visit.id)
    service_ids_subq = select(VisitService.id)

    orphan_visit_ids = _orphan_source_ids_for_kind(
        db,
        source_kind=PayrollFundSourceKind.VISIT,
        start=start,
        end_excl=end_excl,
        existing_ids_subq=visit_ids_subq,
    )
    orphan_service_ids = _orphan_source_ids_for_kind(
        db,
        source_kind=PayrollFundSourceKind.VISIT_SERVICE,
        start=start,
        end_excl=end_excl,
        existing_ids_subq=service_ids_subq,
    )

    details: list[tuple[str, int, float]] = []
    visit_nets = _accrual_net_by_source_ids(
        db,
        source_kind=PayrollFundSourceKind.VISIT,
        source_ids=orphan_visit_ids,
        start=start,
        end_excl=end_excl,
    )
    for sid in orphan_visit_ids:
        net = money_q2(visit_nets.get(sid, 0.0))
        if net != 0.0:
            details.append(("VISIT", sid, net))

    svc_nets = _accrual_net_by_source_ids(
        db,
        source_kind=PayrollFundSourceKind.VISIT_SERVICE,
        source_ids=orphan_service_ids,
        start=start,
        end_excl=end_excl,
    )
    for sid in orphan_service_ids:
        net = money_q2(svc_nets.get(sid, 0.0))
        if net != 0.0:
            details.append(("VISIT_SERVICE", sid, net))

    net_total = money_q2(sum(n for _, _, n in details))
    return OrphanVisitLedgerSummary(
        net_amount=net_total,
        source_count=len(details),
        details=tuple(details),
    )


def storno_orphan_visit_ledger(
    db: Session,
    d0: date,
    d1: date,
    *,
    created_by_user_id: int | None,
) -> OrphanVisitLedgerSummary:
    """Сторно непривязанных VISIT / VISIT_SERVICE, у которых есть нетто в периоде."""
    from app.payroll_fund import storno_source_accruals

    summary = summarize_orphan_visit_ledger(db, d0, d1)
    for kind, sid, _net in summary.details:
        sk = (
            PayrollFundSourceKind.VISIT
            if kind == "VISIT"
            else PayrollFundSourceKind.VISIT_SERVICE
        )
        storno_source_accruals(db, sk, sid, created_by_user_id)
    return summarize_orphan_visit_ledger(db, d0, d1)



def resolve_report_dates(
    db: Session,
    *,
    report_mode: str | None,
    period_id_raw: str | None,
    df_raw: str | None,
    dt_raw: str | None,
    month_start: date,
    today: date,
) -> tuple[date, date, int | None, str]:
    """Границы отчёта и режим (как на странице /admin/reports)."""
    mode = (report_mode or "custom_dates").strip()
    if mode not in ("payroll_period", "custom_dates"):
        mode = "custom_dates"

    selected_period_id: int | None = None
    pid: int | None = None
    if period_id_raw and str(period_id_raw).strip().isdigit():
        pid = int(str(period_id_raw).strip())

    def _from_form_dates() -> tuple[date, date]:
        try:
            d_a = date.fromisoformat(df_raw) if df_raw else month_start
            d_b = date.fromisoformat(dt_raw) if dt_raw else today
        except ValueError:
            d_a, d_b = month_start, today
        return d_a, d_b

    if mode == "payroll_period":
        if pid is not None and pid > 0:
            p = db.get(PayrollPeriod, pid)
            if p is not None and p.closed_at is not None:
                selected_period_id = p.id
                d0 = p.date_from.date()
                d1 = p.date_to.date()
            else:
                selected_period_id = None
                d0, d1 = _from_form_dates()
        else:
            selected_period_id = None
            d0, d1 = _from_form_dates()
    else:
        selected_period_id = None
        d0, d1 = _from_form_dates()

    if d1 < d0:
        d0, d1 = d1, d0
    return d0, d1, selected_period_id, mode


def period_bounds(d0: date, d1: date) -> tuple[datetime, datetime]:
    """Начало первого дня и конец последнего дня (верхняя граница exclusive для SQL)."""
    if d1 < d0:
        d0, d1 = d1, d0
    start = datetime.combine(d0, time.min)
    end_excl = datetime.combine(d1 + timedelta(days=1), time.min)
    return start, end_excl


def _work_event_at():
    """Дата события работы: performed_date, иначе created_at."""
    return func.coalesce(WorkForInventory.performed_date, WorkForInventory.created_at)

@dataclass
class EmployeeFundSlice:
    user_id: int
    display_name: str
    from_visits: float
    from_works: float
    from_consultations: float
    from_hourly: float
    from_manual: float
    total: float


@dataclass
class ReconcileBucket:
    key: str
    label: str
    ops_amount: float
    ledger_amount: float
    delta: float
    journal_source: str  # значение фильтра source в /admin/payroll-fund


@dataclass
class LedgerSourceLine:
    source_kind: str
    source_label: str
    source_id: int | None
    amount: float
    user_name: str
    comment: str
    link: str
    effective_at: datetime | None = None


@dataclass
class OperationalReportResult:
    date_from: date
    date_to: date
    period_label: str

    revenue_visits: float
    revenue_sales: float
    revenue_sales_verified: float
    revenue_sales_pending_review: float
    revenue_works: float
    revenue_total: float

    visits_count: int
    sales_count: int
    works_in_stock: int
    works_custom_order: int
    works_total: int
    visits_plus_sales: int
    unique_clients: int
    consultations_count: int
    hourly_work_count: int

    expenses_total: float

    kanekalon_grams_total: float
    kanekalon_snapshot_rub: float
    kudri_grams_total: float
    kudri_snapshot_rub: float

    visit_studio_to_fund: float
    visit_masters_to_fund: float
    work_studio_to_fund: float
    work_masters_to_fund: float
    retail_studio_to_fund: float
    consultation_masters_to_fund: float
    hourly_masters_to_fund: float
    manual_to_fund: float
    total_to_funds: float
    total_to_funds_without_manual: float

    ledger_accrual: float
    ledger_storno: float
    ledger_expense: float
    ledger_payout: float
    ledger_net_accruals: float
    ledger_net_all: float
    reconciliation_delta: float

    employees: list[EmployeeFundSlice] = field(default_factory=list)
    reconcile_buckets: list[ReconcileBucket] = field(default_factory=list)
    reconcile_extra_lines: list[LedgerSourceLine] = field(default_factory=list)


def _grams_times_price(grams: float, price_per_gram: float | None) -> float:
    if price_per_gram is None:
        return 0.0
    return money_q2(float(grams) * float(price_per_gram))


def _material_retail_kind_from_sale(sale: ProductSale) -> MaterialType | None:
    """Канекалон/кудри по подкатегории услуги «Продажа материала»."""
    if sale.kind != ProductSaleKind.MATERIAL:
        return None
    svc = sale.material_service
    if svc is None or svc.subcategory is None:
        return None
    name = (svc.subcategory.name or "").lower()
    if "кудри" in name:
        return MaterialType.KUDRI
    if "канек" in name:
        return MaterialType.KANEKALON
    return None


def list_closed_payroll_periods(db: Session) -> list[PayrollPeriod]:
    return list(
        db.scalars(
            select(PayrollPeriod)
            .where(PayrollPeriod.closed_at.isnot(None))
            .order_by(PayrollPeriod.date_from.desc(), PayrollPeriod.id.desc())
        ).all()
    )


def build_operational_report(db: Session, d0: date, d1: date) -> OperationalReportResult:
    start, end_excl = period_bounds(d0, d1)
    label = f"{d0.isoformat()} — {d1.isoformat()}"

    visits = list(
        db.scalars(
            select(Visit)
            .options(
                selectinload(Visit.masters),
                selectinload(Visit.services).selectinload(VisitService.masters),
            )
            .where(
                Visit.performed_date >= start,
                Visit.performed_date < end_excl,
                Visit.is_cancelled.is_(False),
            )
        )
        .unique()
        .all()
    )

    sales = list(
        db.scalars(
            select(ProductSale)
            .options(
                selectinload(ProductSale.material_service).selectinload(Service.subcategory),
            )
            .where(
                ProductSale.performed_date >= start,
                ProductSale.performed_date < end_excl,
                ProductSale.is_voided.is_(False),
            )
        ).all()
    )

    work_at = _work_event_at()
    works = list(
        db.scalars(
            select(WorkForInventory)
            .options(selectinload(WorkForInventory.staff_rows))
            .where(
                work_at >= start,
                work_at < end_excl,
                WorkForInventory.is_voided.is_(False),
            )
        )
        .unique()
        .all()
    )

    revenue_visits = money_q2(sum(float(v.amount_from_client or 0) for v in visits))
    revenue_sales = money_q2(sum(float(s.amount_from_client or 0) for s in sales))
    pending_sales = money_q2(
        sum(
            float(s.amount_from_client or 0)
            for s in sales
            if s.kind == ProductSaleKind.MATERIAL and s.material_cost_review_pending
        )
    )
    revenue_sales_verified = money_q2(revenue_sales - pending_sales)
    revenue_sales_pending_review = pending_sales
    rev_w = 0.0
    for w in works:
        if w.amount_from_client is not None:
            rev_w += float(w.amount_from_client)
    revenue_works = money_q2(rev_w)
    revenue_total = money_q2(revenue_visits + revenue_sales + revenue_works)

    visits_count = len(visits)
    sales_count = len(sales)
    works_in_stock = sum(1 for w in works if w.scope == WorkScope.IN_STOCK)
    works_custom_order = sum(1 for w in works if w.scope == WorkScope.CUSTOM_ORDER)
    works_total = len(works)
    visits_plus_sales = visits_count + sales_count

    client_ids: set[int] = set()
    for v in visits:
        client_ids.add(v.client_id)
    for s in sales:
        client_ids.add(s.client_id)
    for w in works:
        if w.client_id is not None:
            client_ids.add(w.client_id)
    unique_clients = len(client_ids)

    exp_val = db.scalar(
        select(func.coalesce(func.sum(StudioExpense.amount), 0.0)).where(
            StudioExpense.date >= start,
            StudioExpense.date < end_excl,
            StudioExpense.is_voided.is_(False),
        )
    )
    expenses_total = money_q2(float(exp_val or 0))

    pk_k = db.get(MaterialPriceCurrent, MaterialType.KANEKALON)
    pk_u = db.get(MaterialPriceCurrent, MaterialType.KUDRI)
    catalog_k = float(pk_k.price_per_gram) if pk_k else None
    catalog_u = float(pk_u.price_per_gram) if pk_u else None

    k_g_total = 0.0
    k_rub_total = 0.0
    u_g_total = 0.0
    u_rub_total = 0.0

    for v in visits:
        kg = float(v.kanekalon_grams or 0)
        ug = float(v.kudri_grams or 0)
        k_g_total = money_q2(k_g_total + kg)
        u_g_total = money_q2(u_g_total + ug)
        k_rub_total = money_q2(k_rub_total + _grams_times_price(kg, v.kanekalon_price_per_gram_at_time))
        u_rub_total = money_q2(u_rub_total + _grams_times_price(ug, v.kudri_price_per_gram_at_time))

    for w in works:
        kg = float(w.kanekalon_grams or 0)
        ug = float(w.kudri_grams or 0)
        k_g_total = money_q2(k_g_total + kg)
        u_g_total = money_q2(u_g_total + ug)
        k_rub_total = money_q2(k_rub_total + _grams_times_price(kg, w.kanekalon_price_per_gram_at_time))
        u_rub_total = money_q2(u_rub_total + _grams_times_price(ug, w.kudri_price_per_gram_at_time))

    for s in sales:
        if s.kind != ProductSaleKind.MATERIAL:
            continue
        svc = s.material_service
        if svc and (svc.retail_material_kanekalon or svc.retail_material_kudri):
            if svc.retail_material_kanekalon:
                gk = float(s.material_kanekalon_grams or 0)
                if gk > 0:
                    k_g_total = money_q2(k_g_total + gk)
                    k_rub_total = money_q2(
                        k_rub_total + _grams_times_price(gk, s.material_kanekalon_price_per_gram_at_time)
                    )
            if svc.retail_material_kudri:
                gku = float(s.material_kudri_grams or 0)
                if gku > 0:
                    u_g_total = money_q2(u_g_total + gku)
                    u_rub_total = money_q2(
                        u_rub_total + _grams_times_price(gku, s.material_kudri_price_per_gram_at_time)
                    )
            continue
        g = float(s.material_grams or 0)
        if g <= 0:
            continue
        mt = _material_retail_kind_from_sale(s)
        if mt == MaterialType.KANEKALON:
            k_g_total = money_q2(k_g_total + g)
            k_rub_total = money_q2(k_rub_total + _grams_times_price(g, catalog_k))
        elif mt == MaterialType.KUDRI:
            u_g_total = money_q2(u_g_total + g)
            u_rub_total = money_q2(u_rub_total + _grams_times_price(g, catalog_u))
        else:
            k_g_total = money_q2(k_g_total + g)
            k_rub_total = money_q2(k_rub_total + _grams_times_price(g, catalog_k))

    visit_studio = 0.0
    visit_masters = 0.0
    by_uid: dict[int, dict[str, float]] = {}

    def add_emp(uid: int, key: str, amt: float) -> None:
        if uid not in by_uid:
            by_uid[uid] = {
                "visits": 0.0,
                "works": 0.0,
                "consultations": 0.0,
                "hourly": 0.0,
                "manual": 0.0,
            }
        by_uid[uid][key] = money_q2(by_uid[uid][key] + amt)

    for v in visits:
        visit_studio = money_q2(
            visit_studio + float(v.salon_profit or 0) + float(v.studio_fund_amount or 0)
        )
        for mid, a in visit_masters_fund_by_master(v).items():
            visit_masters = money_q2(visit_masters + a)
            add_emp(int(mid), "visits", a)
        # 1.19: бонус за смешку больше не входит в фонды визита.

    work_studio = 0.0
    work_masters = 0.0
    for w in works:
        # Для работ «в наличие» студийная доля в снимках — не выручка/маржа.
        # Деньги появляются только при продаже; здесь учитываем только начисления мастерам.
        if w.scope != WorkScope.IN_STOCK:
            work_studio = money_q2(work_studio + float(w.studio_profit_amount or 0))
        for s in w.staff_rows:
            a = money_q2(float(s.master_profit_amount or 0))
            work_masters = money_q2(work_masters + a)
            add_emp(int(s.user_id), "works", a)

    retail_studio = money_q2(sum(float(s.studio_margin_amount or 0) for s in sales))

    hourly_rows = list(
        db.scalars(
            select(HourlyWorkEntry).where(
                HourlyWorkEntry.performed_date >= start,
                HourlyWorkEntry.performed_date < end_excl,
            )
        ).all()
    )
    hourly_masters = 0.0
    for h in hourly_rows:
        a = money_q2(float(h.amount or 0))
        if a <= 0:
            continue
        hourly_masters = money_q2(hourly_masters + a)
        add_emp(int(h.master_user_id), "hourly", a)

    def _ledger_kind_net(kinds: tuple[PayrollFundSourceKind, ...]) -> float:
        """Нетто начислений по источникам: ACCRUAL + сторно начислений (не расходов)."""
        v = db.scalar(
            select(func.coalesce(func.sum(PayrollFundLedger.amount), 0.0))
            .select_from(PayrollFundLedger)
            .outerjoin(_LedgerOrig, PayrollFundLedger.storno_of_id == _LedgerOrig.id)
            .where(
                PayrollFundLedger.effective_at >= start,
                PayrollFundLedger.effective_at < end_excl,
                _ACCRUAL_OR_ACCRUAL_STORNO,
                PayrollFundLedger.source_kind.in_(kinds),
            )
        )
        return money_q2(float(v or 0))

    consultation_masters = _ledger_kind_net((PayrollFundSourceKind.CONSULTATION,))
    manual_net = _ledger_kind_net((PayrollFundSourceKind.MANUAL,))

    # Разнести консультации/ручные по сотрудникам из журнала (для таблицы долей).
    cons_manual_rows = list(
        db.scalars(
            select(PayrollFundLedger)
            .outerjoin(_LedgerOrig, PayrollFundLedger.storno_of_id == _LedgerOrig.id)
            .where(
                PayrollFundLedger.effective_at >= start,
                PayrollFundLedger.effective_at < end_excl,
                _ACCRUAL_OR_ACCRUAL_STORNO,
                PayrollFundLedger.source_kind.in_(
                    (PayrollFundSourceKind.CONSULTATION, PayrollFundSourceKind.MANUAL)
                ),
            )
        ).all()
    )
    for row in cons_manual_rows:
        if row.user_id is None:
            continue
        amt = money_q2(float(row.amount or 0))
        if amt == 0:
            continue
        key = "consultations" if row.source_kind == PayrollFundSourceKind.CONSULTATION else "manual"
        add_emp(int(row.user_id), key, amt)

    consultations_count = int(
        db.scalar(
            select(func.count(func.distinct(PayrollFundLedger.source_id))).where(
                PayrollFundLedger.effective_at >= start,
                PayrollFundLedger.effective_at < end_excl,
                PayrollFundLedger.source_kind == PayrollFundSourceKind.CONSULTATION,
                PayrollFundLedger.entry_kind == PayrollFundEntryKind.ACCRUAL,
                PayrollFundLedger.source_id.isnot(None),
            )
        )
        or 0
    )
    hourly_work_count = len(hourly_rows)

    total_without_manual = money_q2(
        visit_studio
        + visit_masters
        + work_studio
        + work_masters
        + retail_studio
        + consultation_masters
        + hourly_masters
    )
    total_to_funds = money_q2(total_without_manual + manual_net)

    uids = list(by_uid.keys())
    users_by_id: dict[int, User] = {}
    if uids:
        for u in db.scalars(select(User).where(User.id.in_(uids))).all():
            users_by_id[u.id] = u
    employees: list[EmployeeFundSlice] = []
    for uid in sorted(
        by_uid.keys(),
        key=lambda x: (
            -(
                by_uid[x]["visits"]
                + by_uid[x]["works"]
                + by_uid[x]["consultations"]
                + by_uid[x]["hourly"]
                + by_uid[x]["manual"]
            ),
            x,
        ),
    ):
        u = users_by_id.get(uid)
        name = u.display_name if u else f"ID {uid}"
        fv = money_q2(by_uid[uid]["visits"])
        fw = money_q2(by_uid[uid]["works"])
        fc = money_q2(by_uid[uid]["consultations"])
        fh = money_q2(by_uid[uid]["hourly"])
        fm = money_q2(by_uid[uid]["manual"])
        employees.append(
            EmployeeFundSlice(
                user_id=uid,
                display_name=name,
                from_visits=fv,
                from_works=fw,
                from_consultations=fc,
                from_hourly=fh,
                from_manual=fm,
                total=money_q2(fv + fw + fc + fh + fm),
            )
        )

    def _ledger_sum(kind: PayrollFundEntryKind) -> float:
        v = db.scalar(
            select(func.coalesce(func.sum(PayrollFundLedger.amount), 0.0)).where(
                PayrollFundLedger.effective_at >= start,
                PayrollFundLedger.effective_at < end_excl,
                PayrollFundLedger.entry_kind == kind,
            )
        )
        return money_q2(float(v or 0))

    ledger_accrual = _ledger_sum(PayrollFundEntryKind.ACCRUAL)
    # Сторно только по начислениям — сторно расходов не смешиваем со сверкой фондов.
    ledger_storno_raw = db.scalar(
        select(func.coalesce(func.sum(PayrollFundLedger.amount), 0.0))
        .select_from(PayrollFundLedger)
        .join(_LedgerOrig, PayrollFundLedger.storno_of_id == _LedgerOrig.id)
        .where(
            PayrollFundLedger.effective_at >= start,
            PayrollFundLedger.effective_at < end_excl,
            PayrollFundLedger.entry_kind == PayrollFundEntryKind.STORNO,
            _LedgerOrig.entry_kind == PayrollFundEntryKind.ACCRUAL,
        )
    )
    ledger_storno = money_q2(float(ledger_storno_raw or 0))
    ledger_expense = _ledger_sum(PayrollFundEntryKind.EXPENSE)
    ledger_payout = _ledger_sum(PayrollFundEntryKind.PAYOUT)
    ledger_net_accruals = money_q2(ledger_accrual + ledger_storno)
    reconciliation_delta = money_q2(total_to_funds - ledger_net_accruals)
    ledger_all_raw = db.scalar(
        select(func.coalesce(func.sum(PayrollFundLedger.amount), 0.0)).where(
            PayrollFundLedger.effective_at >= start,
            PayrollFundLedger.effective_at < end_excl,
        )
    )
    ledger_net_all = money_q2(float(ledger_all_raw or 0))

    ledger_visits = _ledger_kind_net(
        (PayrollFundSourceKind.VISIT, PayrollFundSourceKind.VISIT_SERVICE)
    )
    ledger_works = _ledger_kind_net((PayrollFundSourceKind.WORK,))
    ledger_sales = _ledger_kind_net((PayrollFundSourceKind.PRODUCT_SALE,))
    ledger_hourly = _ledger_kind_net((PayrollFundSourceKind.HOURLY_WORK,))

    ops_visits = money_q2(visit_studio + visit_masters)
    ops_works = money_q2(work_studio + work_masters)

    def _bucket(
        key: str,
        label: str,
        ops: float,
        led: float,
        journal_source: str,
    ) -> ReconcileBucket:
        return ReconcileBucket(
            key=key,
            label=label,
            ops_amount=ops,
            ledger_amount=led,
            delta=money_q2(ops - led),
            journal_source=journal_source,
        )

    reconcile_buckets = [
        _bucket("visits", "Визиты (услуги визита)", ops_visits, ledger_visits, "VISIT"),
        _bucket("works", "Работы", ops_works, ledger_works, "WORK"),
        _bucket("sales", "Розница / продажи", retail_studio, ledger_sales, "PRODUCT_SALE"),
        _bucket(
            "consultations",
            "Консультации",
            consultation_masters,
            consultation_masters,
            "CONSULTATION",
        ),
        _bucket("hourly", "Почасовая работа", hourly_masters, ledger_hourly, "HOURLY_WORK"),
        _bucket("manual", "Ручные проводки", manual_net, manual_net, "MANUAL"),
    ]

    # Строки журнала по консультациям / почасовой / ручным — для drill-down.
    extra_kinds = (
        PayrollFundSourceKind.CONSULTATION,
        PayrollFundSourceKind.HOURLY_WORK,
        PayrollFundSourceKind.MANUAL,
    )
    extra_ledger = list(
        db.scalars(
            select(PayrollFundLedger)
            .options(selectinload(PayrollFundLedger.user))
            .outerjoin(_LedgerOrig, PayrollFundLedger.storno_of_id == _LedgerOrig.id)
            .where(
                PayrollFundLedger.effective_at >= start,
                PayrollFundLedger.effective_at < end_excl,
                _ACCRUAL_OR_ACCRUAL_STORNO,
                PayrollFundLedger.source_kind.in_(extra_kinds),
            )
            .order_by(PayrollFundLedger.effective_at.desc(), PayrollFundLedger.id.desc())
            .limit(80)
        ).all()
    )
    reconcile_extra_lines: list[LedgerSourceLine] = []
    for row in extra_ledger:
        sk = row.source_kind
        sid = row.source_id
        label = PAYROLL_FUND_SOURCE_KIND_RU.get(sk, sk.value if sk else "")
        link = ""
        if sk == PayrollFundSourceKind.CONSULTATION and sid:
            link = f"/consultations/{sid}"
        elif sk == PayrollFundSourceKind.HOURLY_WORK and sid:
            link = f"/hourly-work/{sid}"
        elif sk == PayrollFundSourceKind.MANUAL:
            link = ""
        uname = ""
        if row.user is not None:
            uname = (row.user.display_name or row.user.username or "").strip()
        reconcile_extra_lines.append(
            LedgerSourceLine(
                source_kind=sk.value if sk else "",
                source_label=label,
                source_id=int(sid) if sid is not None else None,
                amount=money_q2(float(row.amount or 0)),
                user_name=uname or "—",
                comment=(row.comment or "").strip(),
                link=link,
                effective_at=row.effective_at,
            )
        )

    return OperationalReportResult(
        date_from=d0,
        date_to=d1,
        period_label=label,
        revenue_visits=revenue_visits,
        revenue_sales=revenue_sales,
        revenue_sales_verified=revenue_sales_verified,
        revenue_sales_pending_review=revenue_sales_pending_review,
        revenue_works=revenue_works,
        revenue_total=revenue_total,
        visits_count=visits_count,
        sales_count=sales_count,
        works_in_stock=works_in_stock,
        works_custom_order=works_custom_order,
        works_total=works_total,
        visits_plus_sales=visits_plus_sales,
        unique_clients=unique_clients,
        consultations_count=consultations_count,
        hourly_work_count=hourly_work_count,
        expenses_total=expenses_total,
        kanekalon_grams_total=k_g_total,
        kanekalon_snapshot_rub=k_rub_total,
        kudri_grams_total=u_g_total,
        kudri_snapshot_rub=u_rub_total,
        visit_studio_to_fund=visit_studio,
        visit_masters_to_fund=visit_masters,
        work_studio_to_fund=work_studio,
        work_masters_to_fund=work_masters,
        retail_studio_to_fund=retail_studio,
        consultation_masters_to_fund=consultation_masters,
        hourly_masters_to_fund=hourly_masters,
        manual_to_fund=manual_net,
        total_to_funds=total_to_funds,
        total_to_funds_without_manual=total_without_manual,
        ledger_accrual=ledger_accrual,
        ledger_storno=ledger_storno,
        ledger_expense=ledger_expense,
        ledger_payout=ledger_payout,
        ledger_net_accruals=ledger_net_accruals,
        ledger_net_all=ledger_net_all,
        reconciliation_delta=reconciliation_delta,
        employees=employees,
        reconcile_buckets=reconcile_buckets,
        reconcile_extra_lines=reconcile_extra_lines,
    )


def result_to_template_dict(r: OperationalReportResult) -> dict[str, Any]:
    """Плоский dict для Jinja (удобные ключи)."""
    return {
        "period_label": r.period_label,
        "date_from": r.date_from,
        "date_to": r.date_to,
        "revenue_visits": r.revenue_visits,
        "revenue_sales": r.revenue_sales,
        "revenue_sales_verified": r.revenue_sales_verified,
        "revenue_sales_pending_review": r.revenue_sales_pending_review,
        "revenue_works": r.revenue_works,
        "revenue_total": r.revenue_total,
        "visits_count": r.visits_count,
        "sales_count": r.sales_count,
        "works_in_stock": r.works_in_stock,
        "works_custom_order": r.works_custom_order,
        "works_total": r.works_total,
        "visits_plus_sales": r.visits_plus_sales,
        "unique_clients": r.unique_clients,
        "consultations_count": r.consultations_count,
        "hourly_work_count": r.hourly_work_count,
        "expenses_total": r.expenses_total,
        "kanekalon_grams_total": r.kanekalon_grams_total,
        "kanekalon_snapshot_rub": r.kanekalon_snapshot_rub,
        "kudri_grams_total": r.kudri_grams_total,
        "kudri_snapshot_rub": r.kudri_snapshot_rub,
        "visit_studio_to_fund": r.visit_studio_to_fund,
        "visit_masters_to_fund": r.visit_masters_to_fund,
        "work_studio_to_fund": r.work_studio_to_fund,
        "work_masters_to_fund": r.work_masters_to_fund,
        "retail_studio_to_fund": r.retail_studio_to_fund,
        "consultation_masters_to_fund": r.consultation_masters_to_fund,
        "hourly_masters_to_fund": r.hourly_masters_to_fund,
        "manual_to_fund": r.manual_to_fund,
        "total_to_funds": r.total_to_funds,
        "total_to_funds_without_manual": r.total_to_funds_without_manual,
        "employees": r.employees,
        "reconcile_buckets": r.reconcile_buckets,
        "reconcile_extra_lines": r.reconcile_extra_lines,
        "ledger_accrual": r.ledger_accrual,
        "ledger_storno": r.ledger_storno,
        "ledger_expense": r.ledger_expense,
        "ledger_payout": r.ledger_payout,
        "ledger_net_accruals": r.ledger_net_accruals,
        "ledger_net_all": r.ledger_net_all,
        "reconciliation_delta": r.reconciliation_delta,
    }


def list_report_visits(db: Session, d0: date, d1: date) -> list[ReportFundCompareRow]:
    """Сверка фондов по визитам: срез отчёта + сиротские проводки периода."""
    start, end_excl = period_bounds(d0, d1)
    ops_visits = list(
        db.scalars(
            select(Visit)
            .options(
                selectinload(Visit.masters),
                selectinload(Visit.services).selectinload(VisitService.masters),
            )
            .where(
                Visit.performed_date >= start,
                Visit.performed_date < end_excl,
                Visit.is_cancelled.is_(False),
            )
        ).all()
    )
    ops_by_id = {int(v.id): v for v in ops_visits}
    ledger_visit_ids = _visit_ids_with_accrual_in_period(db, start, end_excl)
    all_ids = sorted(set(ops_by_id) | ledger_visit_ids)

    missing_ids = [i for i in all_ids if i not in ops_by_id]
    if missing_ids:
        for v in db.scalars(
            select(Visit)
            .options(
                selectinload(Visit.masters),
                selectinload(Visit.services).selectinload(VisitService.masters),
            )
            .where(Visit.id.in_(missing_ids))
        ).all():
            ops_by_id[int(v.id)] = v

    ledger_by_id = _visit_accrual_net_by_visit_id(
        db, visit_ids=all_ids, start=start, end_excl=end_excl
    )

    rows: list[ReportFundCompareRow] = []
    for vid in all_ids:
        v = ops_by_id.get(vid)
        in_ops = (
            v is not None
            and not v.is_cancelled
            and v.performed_date is not None
            and start <= v.performed_date < end_excl
        )
        ops_amt = _visit_ops_funds(v) if in_ops and v is not None else 0.0
        led_amt = money_q2(ledger_by_id.get(vid, 0.0))
        if v is None:
            note = "карточка не найдена; только журнал"
            event_at = None
            open_url = f"/visits/{vid}"
            client_header = None
            client_card = None
        else:
            note = _visit_note(v, in_ops_slice=in_ops)
            event_at = v.performed_date
            open_url = f"/visits/{v.id}"
            client_header = money_q2(float(v.amount_from_client or 0))
            client_card = _visit_services_amount_from_client(v)
        rows.append(
            ReportFundCompareRow(
                entity=v,
                entity_id=vid,
                event_at=event_at,
                open_url=open_url,
                note=note,
                amount_ops=ops_amt,
                amount_ledger=led_amt,
                amount_mismatch=_fund_mismatch(ops_amt, led_amt),
                client_amount_header=client_header,
                client_amount_card=client_card,
                client_amount_mismatch=_client_amounts_mismatch(client_header, client_card),
            )
        )
    return _sort_fund_rows(rows)


def list_report_sales(db: Session, d0: date, d1: date) -> list[ReportFundCompareRow]:
    start, end_excl = period_bounds(d0, d1)
    ops_sales = list(
        db.scalars(
            select(ProductSale).where(
                ProductSale.performed_date >= start,
                ProductSale.performed_date < end_excl,
                ProductSale.is_voided.is_(False),
            )
        ).all()
    )
    ops_by_id = {int(s.id): s for s in ops_sales}
    ledger_ids = _source_ids_with_accrual_in_period(
        db, source_kind=PayrollFundSourceKind.PRODUCT_SALE, start=start, end_excl=end_excl
    )
    all_ids = sorted(set(ops_by_id) | ledger_ids)
    missing = [i for i in all_ids if i not in ops_by_id]
    if missing:
        for s in db.scalars(select(ProductSale).where(ProductSale.id.in_(missing))).all():
            ops_by_id[int(s.id)] = s

    ledger_by_id = _accrual_net_by_source_ids(
        db,
        source_kind=PayrollFundSourceKind.PRODUCT_SALE,
        source_ids=all_ids,
        start=start,
        end_excl=end_excl,
    )

    rows: list[ReportFundCompareRow] = []
    for sid in all_ids:
        s = ops_by_id.get(sid)
        in_ops = (
            s is not None
            and not s.is_voided
            and s.performed_date is not None
            and start <= s.performed_date < end_excl
        )
        ops_amt = _sale_ops_funds(s) if in_ops and s is not None else 0.0
        led_amt = money_q2(ledger_by_id.get(sid, 0.0))
        if s is None:
            note = "карточка не найдена; только журнал"
            event_at = None
            open_url = f"/sales/products/{sid}"
            client_amt = None
        else:
            note = _void_note(bool(s.is_voided), in_ops_slice=in_ops)
            event_at = s.performed_date
            open_url = f"/sales/products/{s.id}"
            client_amt = money_q2(float(s.amount_from_client or 0))
        rows.append(
            ReportFundCompareRow(
                entity=s,
                entity_id=sid,
                event_at=event_at,
                open_url=open_url,
                note=note,
                amount_ops=ops_amt,
                amount_ledger=led_amt,
                amount_mismatch=_fund_mismatch(ops_amt, led_amt),
                client_amount_header=client_amt,
                client_amount_card=client_amt,
                client_amount_mismatch=False,
            )
        )
    return _sort_fund_rows(rows)


def list_report_works(db: Session, d0: date, d1: date) -> list[ReportFundCompareRow]:
    start, end_excl = period_bounds(d0, d1)
    work_at = _work_event_at()
    ops_works = list(
        db.scalars(
            select(WorkForInventory)
            .options(selectinload(WorkForInventory.staff_rows))
            .where(
                work_at >= start,
                work_at < end_excl,
                WorkForInventory.is_voided.is_(False),
            )
        )
        .unique()
        .all()
    )
    ops_by_id = {int(w.id): w for w in ops_works}
    ledger_ids = _source_ids_with_accrual_in_period(
        db, source_kind=PayrollFundSourceKind.WORK, start=start, end_excl=end_excl
    )
    all_ids = sorted(set(ops_by_id) | ledger_ids)
    missing = [i for i in all_ids if i not in ops_by_id]
    if missing:
        for w in db.scalars(
            select(WorkForInventory)
            .options(selectinload(WorkForInventory.staff_rows))
            .where(WorkForInventory.id.in_(missing))
        ).unique().all():
            ops_by_id[int(w.id)] = w

    ledger_by_id = _accrual_net_by_source_ids(
        db,
        source_kind=PayrollFundSourceKind.WORK,
        source_ids=all_ids,
        start=start,
        end_excl=end_excl,
    )

    rows: list[ReportFundCompareRow] = []
    for wid in all_ids:
        w = ops_by_id.get(wid)
        event_at = (w.performed_date or w.created_at) if w is not None else None
        in_ops = (
            w is not None
            and not w.is_voided
            and event_at is not None
            and start <= event_at < end_excl
        )
        ops_amt = _work_ops_funds(w) if in_ops and w is not None else 0.0
        led_amt = money_q2(ledger_by_id.get(wid, 0.0))
        if w is None:
            note = "карточка не найдена; только журнал"
            open_url = f"/sales/work/{wid}"
            client_amt = None
        else:
            note = _void_note(bool(w.is_voided), in_ops_slice=in_ops)
            open_url = f"/sales/work/{w.id}"
            client_amt = (
                money_q2(float(w.amount_from_client)) if w.amount_from_client is not None else None
            )
        rows.append(
            ReportFundCompareRow(
                entity=w,
                entity_id=wid,
                event_at=event_at,
                open_url=open_url,
                note=note,
                amount_ops=ops_amt,
                amount_ledger=led_amt,
                amount_mismatch=_fund_mismatch(ops_amt, led_amt),
                client_amount_header=client_amt,
                client_amount_card=client_amt,
                client_amount_mismatch=False,
            )
        )
    return _sort_fund_rows(rows)


def list_report_consultations(db: Session, d0: date, d1: date) -> list[ReportFundCompareRow]:
    """Консультации: в отчёте фонды = журнал; строки нужны для сирот и контроля периода."""
    start, end_excl = period_bounds(d0, d1)
    ops_rows = list(
        db.scalars(
            select(Consultation).where(
                Consultation.consultation_date >= start,
                Consultation.consultation_date < end_excl,
            )
        ).all()
    )
    ops_by_id = {int(c.id): c for c in ops_rows}
    ledger_ids = _source_ids_with_accrual_in_period(
        db, source_kind=PayrollFundSourceKind.CONSULTATION, start=start, end_excl=end_excl
    )
    all_ids = sorted(set(ops_by_id) | ledger_ids)
    missing = [i for i in all_ids if i not in ops_by_id]
    if missing:
        for c in db.scalars(select(Consultation).where(Consultation.id.in_(missing))).all():
            ops_by_id[int(c.id)] = c

    ledger_by_id = _accrual_net_by_source_ids(
        db,
        source_kind=PayrollFundSourceKind.CONSULTATION,
        source_ids=all_ids,
        start=start,
        end_excl=end_excl,
    )

    rows: list[ReportFundCompareRow] = []
    for cid in all_ids:
        c = ops_by_id.get(cid)
        led_amt = money_q2(ledger_by_id.get(cid, 0.0))
        in_date_slice = (
            c is not None
            and c.consultation_date is not None
            and start <= c.consultation_date < end_excl
        )
        if c is None:
            note = "карточка не найдена; только журнал"
            event_at = None
            open_url = f"/consultations/{cid}"
            # Без карточки в сводку попадает только журнал — помечаем как сироту.
            ops_amt = 0.0
        else:
            # В сводке консультации = журнал; по дате показываем ту же сумму.
            ops_amt = led_amt if in_date_slice or led_amt != 0.0 else 0.0
            note = None if (in_date_slice or led_amt == 0.0) else "дата консультации вне периода; проводка в периоде"
            event_at = c.consultation_date
            open_url = f"/consultations/{c.id}"
        rows.append(
            ReportFundCompareRow(
                entity=c,
                entity_id=cid,
                event_at=event_at,
                open_url=open_url,
                note=note,
                amount_ops=ops_amt,
                amount_ledger=led_amt,
                amount_mismatch=_fund_mismatch(ops_amt, led_amt),
            )
        )
    return _sort_fund_rows(rows)


def list_report_hourly(db: Session, d0: date, d1: date) -> list[ReportFundCompareRow]:
    start, end_excl = period_bounds(d0, d1)
    ops_rows = list(
        db.scalars(
            select(HourlyWorkEntry)
            .options(selectinload(HourlyWorkEntry.master_user))
            .where(
                HourlyWorkEntry.performed_date >= start,
                HourlyWorkEntry.performed_date < end_excl,
            )
        ).all()
    )
    ops_by_id = {int(h.id): h for h in ops_rows}
    ledger_ids = _source_ids_with_accrual_in_period(
        db, source_kind=PayrollFundSourceKind.HOURLY_WORK, start=start, end_excl=end_excl
    )
    all_ids = sorted(set(ops_by_id) | ledger_ids)
    missing = [i for i in all_ids if i not in ops_by_id]
    if missing:
        for h in db.scalars(
            select(HourlyWorkEntry)
            .options(selectinload(HourlyWorkEntry.master_user))
            .where(HourlyWorkEntry.id.in_(missing))
        ).all():
            ops_by_id[int(h.id)] = h

    ledger_by_id = _accrual_net_by_source_ids(
        db,
        source_kind=PayrollFundSourceKind.HOURLY_WORK,
        source_ids=all_ids,
        start=start,
        end_excl=end_excl,
    )

    rows: list[ReportFundCompareRow] = []
    for hid in all_ids:
        h = ops_by_id.get(hid)
        in_ops = (
            h is not None
            and h.performed_date is not None
            and start <= h.performed_date < end_excl
        )
        ops_amt = _hourly_ops_funds(h) if in_ops and h is not None else 0.0
        led_amt = money_q2(ledger_by_id.get(hid, 0.0))
        if h is None:
            note = "карточка не найдена; только журнал"
            event_at = None
            open_url = f"/hourly-work/{hid}"
        else:
            note = None if in_ops else "только журнал / вне среза отчёта"
            event_at = h.performed_date
            open_url = f"/hourly-work/{h.id}"
        rows.append(
            ReportFundCompareRow(
                entity=h,
                entity_id=hid,
                event_at=event_at,
                open_url=open_url,
                note=note,
                amount_ops=ops_amt,
                amount_ledger=led_amt,
                amount_mismatch=_fund_mismatch(ops_amt, led_amt),
            )
        )
    return _sort_fund_rows(rows)


def report_to_csv(r: OperationalReportResult) -> str:
    """CSV с разделителем «;» и UTF-8 BOM для Excel."""
    buf = io.StringIO()
    wr = csv.writer(buf, delimiter=";")
    wr.writerow(["Период", r.period_label])
    wr.writerow([])
    wr.writerow(["Показатель", "Значение"])
    wr.writerow(["Выручка всего", f"{r.revenue_total:.2f}"])
    wr.writerow(["Выручка визиты", f"{r.revenue_visits:.2f}"])
    wr.writerow(["Выручка продажи всего", f"{r.revenue_sales:.2f}"])
    wr.writerow(["Выручка продажи без проверки себестоимости", f"{r.revenue_sales_verified:.2f}"])
    wr.writerow(["Выручка продажи непроверенные", f"{r.revenue_sales_pending_review:.2f}"])
    wr.writerow(["Выручка работы", f"{r.revenue_works:.2f}"])
    wr.writerow(["Визитов", str(r.visits_count)])
    wr.writerow(["Продаж", str(r.sales_count)])
    wr.writerow(["Работ всего", str(r.works_total)])
    wr.writerow(["Уникальных клиентов", str(r.unique_clients)])
    wr.writerow(["Консультаций (с начислением)", str(r.consultations_count)])
    wr.writerow(["Почасовых работ", str(r.hourly_work_count)])
    wr.writerow(["Расходы студии", f"{r.expenses_total:.2f}"])
    wr.writerow(["Канекалон г", f"{r.kanekalon_grams_total:.2f}"])
    wr.writerow(["Канекалон руб снимок", f"{r.kanekalon_snapshot_rub:.2f}"])
    wr.writerow(["Кудри г", f"{r.kudri_grams_total:.2f}"])
    wr.writerow(["Кудри руб снимок", f"{r.kudri_snapshot_rub:.2f}"])
    wr.writerow(["В фонды визиты студия", f"{r.visit_studio_to_fund:.2f}"])
    wr.writerow(["В фонды визиты сотрудники", f"{r.visit_masters_to_fund:.2f}"])
    wr.writerow(["В фонды работы студия", f"{r.work_studio_to_fund:.2f}"])
    wr.writerow(["В фонды работы сотрудники", f"{r.work_masters_to_fund:.2f}"])
    wr.writerow(["В фонды розница", f"{r.retail_studio_to_fund:.2f}"])
    wr.writerow(["В фонды консультации", f"{r.consultation_masters_to_fund:.2f}"])
    wr.writerow(["В фонды почасовая", f"{r.hourly_masters_to_fund:.2f}"])
    wr.writerow(["В фонды ручные", f"{r.manual_to_fund:.2f}"])
    wr.writerow(["В фонды операционно", f"{r.total_to_funds:.2f}"])
    wr.writerow(["Журнал начисления", f"{r.ledger_accrual:.2f}"])
    wr.writerow(["Журнал сторно", f"{r.ledger_storno:.2f}"])
    wr.writerow(["Журнал нетто начисл+сторно", f"{r.ledger_net_accruals:.2f}"])
    wr.writerow(["Дельта операц минус нетто начисл", f"{r.reconciliation_delta:.2f}"])
    wr.writerow(["Журнал расходы", f"{r.ledger_expense:.2f}"])
    wr.writerow(["Журнал выплаты", f"{r.ledger_payout:.2f}"])
    wr.writerow(["Журнал нетто все проводки", f"{r.ledger_net_all:.2f}"])
    wr.writerow([])
    wr.writerow(["Источник сверки", "В отчёте", "В журнале", "Дельта"])
    for b in r.reconcile_buckets:
        wr.writerow([b.label, f"{b.ops_amount:.2f}", f"{b.ledger_amount:.2f}", f"{b.delta:.2f}"])
    wr.writerow([])
    wr.writerow(["Сотрудник", "По визитам", "По работам", "Консультации", "Почасовая", "Ручные", "Всего"])
    for e in r.employees:
        wr.writerow(
            [
                e.display_name,
                f"{e.from_visits:.2f}",
                f"{e.from_works:.2f}",
                f"{e.from_consultations:.2f}",
                f"{e.from_hourly:.2f}",
                f"{e.from_manual:.2f}",
                f"{e.total:.2f}",
            ]
        )
    return buf.getvalue()
