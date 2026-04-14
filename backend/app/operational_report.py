"""Операционный отчёт за период (срез по created_at сущностей + блок по журналу ЗП)."""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.db.models import (
    MaterialPriceCurrent,
    MaterialType,
    PayrollFundEntryKind,
    PayrollFundLedger,
    PayrollPeriod,
    ProductSale,
    ProductSaleKind,
    Service,
    StudioExpense,
    User,
    Visit,
    WorkForInventory,
    WorkScope,
)
from app.payroll_fund import money_q2


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


@dataclass
class EmployeeFundSlice:
    user_id: int
    display_name: str
    from_visits: float
    from_works: float
    total: float


@dataclass
class OperationalReportResult:
    date_from: date
    date_to: date
    period_label: str

    revenue_visits: float
    revenue_sales: float
    revenue_works: float
    revenue_total: float

    visits_count: int
    sales_count: int
    works_in_stock: int
    works_custom_order: int
    works_total: int
    visits_plus_sales: int
    unique_clients: int

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
    total_to_funds: float

    ledger_accrual: float
    ledger_storno: float
    ledger_expense: float
    ledger_payout: float
    ledger_net_accruals: float
    ledger_net_all: float
    reconciliation_delta: float

    employees: list[EmployeeFundSlice] = field(default_factory=list)


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
            .options(selectinload(Visit.masters))
            .where(
                Visit.created_at >= start,
                Visit.created_at < end_excl,
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
                ProductSale.created_at >= start,
                ProductSale.created_at < end_excl,
                ProductSale.is_voided.is_(False),
            )
        ).all()
    )

    works = list(
        db.scalars(
            select(WorkForInventory)
            .options(selectinload(WorkForInventory.staff_rows))
            .where(
                WorkForInventory.created_at >= start,
                WorkForInventory.created_at < end_excl,
                WorkForInventory.is_voided.is_(False),
            )
        )
        .unique()
        .all()
    )

    revenue_visits = money_q2(sum(float(v.amount_from_client or 0) for v in visits))
    revenue_sales = money_q2(sum(float(s.amount_from_client or 0) for s in sales))
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
            StudioExpense.created_at >= start,
            StudioExpense.created_at < end_excl,
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
            # Подкатегория не «канек»/«кудри» — как в расчёте маржи розницы по умолчанию считаем канекалоном.
            k_g_total = money_q2(k_g_total + g)
            k_rub_total = money_q2(k_rub_total + _grams_times_price(g, catalog_k))

    visit_studio = 0.0
    visit_masters = 0.0
    by_uid: dict[int, dict[str, float]] = {}

    def add_emp(uid: int, key: str, amt: float) -> None:
        if uid not in by_uid:
            by_uid[uid] = {"visits": 0.0, "works": 0.0}
        by_uid[uid][key] = money_q2(by_uid[uid][key] + amt)

    for v in visits:
        visit_studio = money_q2(
            visit_studio + float(v.salon_profit or 0) + float(v.studio_fund_amount or 0)
        )
        mp = float(v.masters_pool or 0)
        for vm in v.masters:
            pct = float(vm.percent or 0) / 100.0
            a = money_q2(mp * pct)
            visit_masters = money_q2(visit_masters + a)
            add_emp(int(vm.master_id), "visits", a)
        bonus_mid = v.mix_bonus_master_id
        bonus_amt = money_q2(float(v.mix_bonus_amount or 0))
        if bonus_mid and bonus_amt > 0:
            visit_masters = money_q2(visit_masters + bonus_amt)
            add_emp(int(bonus_mid), "visits", bonus_amt)

    work_studio = 0.0
    work_masters = 0.0
    for w in works:
        work_studio = money_q2(work_studio + float(w.studio_profit_amount or 0))
        for s in w.staff_rows:
            a = money_q2(float(s.master_profit_amount or 0))
            work_masters = money_q2(work_masters + a)
            add_emp(int(s.user_id), "works", a)

    retail_studio = money_q2(sum(float(s.studio_margin_amount or 0) for s in sales))

    total_to_funds = money_q2(
        visit_studio + visit_masters + work_studio + work_masters + retail_studio
    )

    uids = list(by_uid.keys())
    users_by_id: dict[int, User] = {}
    if uids:
        for u in db.scalars(select(User).where(User.id.in_(uids))).all():
            users_by_id[u.id] = u
    employees: list[EmployeeFundSlice] = []
    for uid in sorted(by_uid.keys(), key=lambda x: (-(by_uid[x]["visits"] + by_uid[x]["works"]), x)):
        u = users_by_id.get(uid)
        name = u.display_name if u else f"ID {uid}"
        fv = money_q2(by_uid[uid]["visits"])
        fw = money_q2(by_uid[uid]["works"])
        employees.append(
            EmployeeFundSlice(
                user_id=uid,
                display_name=name,
                from_visits=fv,
                from_works=fw,
                total=money_q2(fv + fw),
            )
        )

    def _ledger_sum(kind: PayrollFundEntryKind) -> float:
        v = db.scalar(
            select(func.coalesce(func.sum(PayrollFundLedger.amount), 0.0)).where(
                PayrollFundLedger.created_at >= start,
                PayrollFundLedger.created_at < end_excl,
                PayrollFundLedger.entry_kind == kind,
            )
        )
        return money_q2(float(v or 0))

    ledger_accrual = _ledger_sum(PayrollFundEntryKind.ACCRUAL)
    ledger_storno = _ledger_sum(PayrollFundEntryKind.STORNO)
    ledger_expense = _ledger_sum(PayrollFundEntryKind.EXPENSE)
    ledger_payout = _ledger_sum(PayrollFundEntryKind.PAYOUT)
    ledger_net_accruals = money_q2(ledger_accrual + ledger_storno)
    reconciliation_delta = money_q2(total_to_funds - ledger_net_accruals)
    ledger_all_raw = db.scalar(
        select(func.coalesce(func.sum(PayrollFundLedger.amount), 0.0)).where(
            PayrollFundLedger.created_at >= start,
            PayrollFundLedger.created_at < end_excl,
        )
    )
    ledger_net_all = money_q2(float(ledger_all_raw or 0))

    return OperationalReportResult(
        date_from=d0,
        date_to=d1,
        period_label=label,
        revenue_visits=revenue_visits,
        revenue_sales=revenue_sales,
        revenue_works=revenue_works,
        revenue_total=revenue_total,
        visits_count=visits_count,
        sales_count=sales_count,
        works_in_stock=works_in_stock,
        works_custom_order=works_custom_order,
        works_total=works_total,
        visits_plus_sales=visits_plus_sales,
        unique_clients=unique_clients,
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
        total_to_funds=total_to_funds,
        ledger_accrual=ledger_accrual,
        ledger_storno=ledger_storno,
        ledger_expense=ledger_expense,
        ledger_payout=ledger_payout,
        ledger_net_accruals=ledger_net_accruals,
        ledger_net_all=ledger_net_all,
        reconciliation_delta=reconciliation_delta,
        employees=employees,
    )


def result_to_template_dict(r: OperationalReportResult) -> dict[str, Any]:
    """Плоский dict для Jinja (удобные ключи)."""
    return {
        "period_label": r.period_label,
        "date_from": r.date_from,
        "date_to": r.date_to,
        "revenue_visits": r.revenue_visits,
        "revenue_sales": r.revenue_sales,
        "revenue_works": r.revenue_works,
        "revenue_total": r.revenue_total,
        "visits_count": r.visits_count,
        "sales_count": r.sales_count,
        "works_in_stock": r.works_in_stock,
        "works_custom_order": r.works_custom_order,
        "works_total": r.works_total,
        "visits_plus_sales": r.visits_plus_sales,
        "unique_clients": r.unique_clients,
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
        "total_to_funds": r.total_to_funds,
        "employees": r.employees,
        "ledger_accrual": r.ledger_accrual,
        "ledger_storno": r.ledger_storno,
        "ledger_expense": r.ledger_expense,
        "ledger_payout": r.ledger_payout,
        "ledger_net_accruals": r.ledger_net_accruals,
        "ledger_net_all": r.ledger_net_all,
        "reconciliation_delta": r.reconciliation_delta,
    }


def list_report_visits(db: Session, d0: date, d1: date) -> list[Visit]:
    start, end_excl = period_bounds(d0, d1)
    return list(
        db.scalars(
            select(Visit)
            .options(selectinload(Visit.client))
            .where(
                Visit.created_at >= start,
                Visit.created_at < end_excl,
                Visit.is_cancelled.is_(False),
            )
            .order_by(Visit.created_at.desc(), Visit.id.desc())
        ).all()
    )


def list_report_sales(db: Session, d0: date, d1: date) -> list[ProductSale]:
    start, end_excl = period_bounds(d0, d1)
    return list(
        db.scalars(
            select(ProductSale)
            .options(selectinload(ProductSale.client))
            .where(
                ProductSale.created_at >= start,
                ProductSale.created_at < end_excl,
                ProductSale.is_voided.is_(False),
            )
            .order_by(ProductSale.created_at.desc(), ProductSale.id.desc())
        ).all()
    )


def list_report_works(db: Session, d0: date, d1: date) -> list[WorkForInventory]:
    start, end_excl = period_bounds(d0, d1)
    return list(
        db.scalars(
            select(WorkForInventory)
            .options(selectinload(WorkForInventory.client))
            .where(
                WorkForInventory.created_at >= start,
                WorkForInventory.created_at < end_excl,
                WorkForInventory.is_voided.is_(False),
            )
            .order_by(WorkForInventory.created_at.desc(), WorkForInventory.id.desc())
        ).all()
    )


def report_to_csv(r: OperationalReportResult) -> str:
    """CSV с разделителем «;» и UTF-8 BOM для Excel."""
    buf = io.StringIO()
    wr = csv.writer(buf, delimiter=";")
    wr.writerow(["Период", r.period_label])
    wr.writerow([])
    wr.writerow(["Показатель", "Значение"])
    wr.writerow(["Выручка всего", f"{r.revenue_total:.2f}"])
    wr.writerow(["Выручка визиты", f"{r.revenue_visits:.2f}"])
    wr.writerow(["Выручка продажи", f"{r.revenue_sales:.2f}"])
    wr.writerow(["Выручка работы", f"{r.revenue_works:.2f}"])
    wr.writerow(["Визитов", str(r.visits_count)])
    wr.writerow(["Продаж", str(r.sales_count)])
    wr.writerow(["Работ всего", str(r.works_total)])
    wr.writerow(["Уникальных клиентов", str(r.unique_clients)])
    wr.writerow(["Расходы студии", f"{r.expenses_total:.2f}"])
    wr.writerow(["Канекалон г", f"{r.kanekalon_grams_total:.2f}"])
    wr.writerow(["Канекалон руб снимок", f"{r.kanekalon_snapshot_rub:.2f}"])
    wr.writerow(["Кудри г", f"{r.kudri_grams_total:.2f}"])
    wr.writerow(["Кудри руб снимок", f"{r.kudri_snapshot_rub:.2f}"])
    wr.writerow(["В фонды операционно", f"{r.total_to_funds:.2f}"])
    wr.writerow(["Журнал начисления", f"{r.ledger_accrual:.2f}"])
    wr.writerow(["Журнал сторно", f"{r.ledger_storno:.2f}"])
    wr.writerow(["Журнал нетто начисл+сторно", f"{r.ledger_net_accruals:.2f}"])
    wr.writerow(["Дельта операц минус нетто начисл", f"{r.reconciliation_delta:.2f}"])
    wr.writerow(["Журнал расходы", f"{r.ledger_expense:.2f}"])
    wr.writerow(["Журнал выплаты", f"{r.ledger_payout:.2f}"])
    wr.writerow(["Журнал нетто все проводки", f"{r.ledger_net_all:.2f}"])
    wr.writerow([])
    wr.writerow(["Сотрудник", "По визитам", "По работам", "Всего"])
    for e in r.employees:
        wr.writerow([e.display_name, f"{e.from_visits:.2f}", f"{e.from_works:.2f}", f"{e.total:.2f}"])
    return buf.getvalue()
