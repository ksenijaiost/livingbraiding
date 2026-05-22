from __future__ import annotations

import calendar
import re
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.auth import AuthUser, get_current_user, require_role
from app.db.models import (
    Booking,
    BookingMaster,
    BookingStaff,
    BookingStatus,
    PayrollFundLedger,
    PayrollFundSide,
    PayrollFundSourceKind,
    PayrollPeriod,
    ProductSale,
    Service,
    ServiceCategory,
    ServiceSubcategory,
    StudioExpense,
    UserRole,
    Visit,
    VisitMaster,
    WorkForInventory,
    WorkForInventoryStaff,
)
from app.db.session import get_db
from app.display_time import get_display_timezone
from app.time_utils import utcnow_naive
from app.payroll_fund import (
    employee_fund_balance,
    employee_payout_total_net,
    studio_fund_balance,
)
from app.webui import templates, ctx as _ctx


router = APIRouter()


def _money0(x: float | None) -> float:
    if x is None:
        return 0.0
    v = float(x)
    return 0.0 if abs(v) < 0.0005 else v


@router.get("/", response_class=HTMLResponse)
def home(
    request: Request,
    m: str | None = None,
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    payroll_home: dict[str, Any] | None = None
    calendar_ctx: dict[str, Any] | None = None
    sections_ctx: dict[str, Any] | None = None

    display_tz = get_display_timezone(db)
    tz = ZoneInfo(display_tz)

    def _parse_month_ym(ym: str | None) -> tuple[int, int] | None:
        if ym is None:
            return None
        s = str(ym).strip()
        if not re.fullmatch(r"\d{4}-\d{2}", s):
            return None
        try:
            d = date.fromisoformat(f"{s}-01")
        except ValueError:
            return None
        return d.year, d.month

    # Month to render: default = current month in display timezone.
    now_local = utcnow_naive().replace(tzinfo=ZoneInfo("UTC")).astimezone(tz)
    parsed = _parse_month_ym(m)
    year = parsed[0] if parsed else now_local.year
    month = parsed[1] if parsed else now_local.month
    month_local_start = datetime(year, month, 1, 0, 0, 0, tzinfo=tz)
    if month == 12:
        next_month_local_start = datetime(year + 1, 1, 1, 0, 0, 0, tzinfo=tz)
    else:
        next_month_local_start = datetime(year, month + 1, 1, 0, 0, 0, tzinfo=tz)
    month_start_utc = month_local_start.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    month_end_utc = next_month_local_start.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)

    def _utc_naive_to_local_date(dt_utc_naive: datetime) -> date:
        return dt_utc_naive.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz).date()

    if current_user.role in (UserRole.MASTER, UserRole.ADMIN, UserRole.ADMIN_SUPER):
        show_studio = UserRole.ADMIN_SUPER in current_user.roles

        payroll_home = {
            "personal_balance": _money0(employee_fund_balance(db, current_user.id)),
            "paid_net": _money0(employee_payout_total_net(db, current_user.id)),
            "show_studio": show_studio,
            "studio_balance": _money0(studio_fund_balance(db)) if show_studio else None,
            "display_tz": display_tz,
        }

        visits_by_day: dict[date, int] = defaultdict(int)
        bookings_by_day: dict[date, int] = defaultdict(int)
        works_by_day: dict[date, int] = defaultdict(int)
        payroll_sum_by_day: dict[date, float] = defaultdict(float)

        v_stmt = (
            select(Visit.id, Visit.performed_date)
            .where(
                Visit.is_cancelled.is_(False),
                Visit.performed_date >= month_start_utc,
                Visit.performed_date < month_end_utc,
            )
        )
        if current_user.role == UserRole.MASTER:
            v_stmt = v_stmt.where(Visit.id.in_(select(VisitMaster.visit_id).where(VisitMaster.master_id == current_user.id)))
        visit_rows = list(db.execute(v_stmt).all())
        visit_ids = [int(vid) for vid, _ in visit_rows if vid is not None]
        for _, dt0 in visit_rows:
            if isinstance(dt0, datetime):
                visits_by_day[_utc_naive_to_local_date(dt0)] += 1

        w_stmt = (
            select(WorkForInventory.id, WorkForInventory.performed_date, WorkForInventory.created_at)
            .where(
                WorkForInventory.is_voided.is_(False),
                or_(
                    and_(
                        WorkForInventory.performed_date.is_not(None),
                        WorkForInventory.performed_date >= month_start_utc,
                        WorkForInventory.performed_date < month_end_utc,
                    ),
                    and_(
                        WorkForInventory.performed_date.is_(None),
                        WorkForInventory.created_at >= month_start_utc,
                        WorkForInventory.created_at < month_end_utc,
                    ),
                ),
            )
        )
        if current_user.role == UserRole.MASTER:
            w_stmt = w_stmt.where(
                or_(
                    WorkForInventory.created_by_user_id == current_user.id,
                    WorkForInventory.id.in_(select(WorkForInventoryStaff.work_id).where(WorkForInventoryStaff.user_id == current_user.id)),
                )
            )
        work_rows = list(db.execute(w_stmt).all())
        work_ids = [int(wid) for wid, _, _ in work_rows if wid is not None]
        for _, perf_dt, created_dt in work_rows:
            dt0 = perf_dt if isinstance(perf_dt, datetime) else created_dt
            if isinstance(dt0, datetime):
                works_by_day[_utc_naive_to_local_date(dt0)] += 1

        b_stmt = (
            select(Booking.planned_date)
            .where(
                Booking.status.in_(
                    (BookingStatus.PENDING_CONFIRMATION, BookingStatus.ACTIVE)
                ),
                Booking.planned_date >= month_start_utc,
                Booking.planned_date < month_end_utc,
            )
        )
        if current_user.role == UserRole.MASTER:
            b_stmt = b_stmt.where(
                or_(
                    Booking.id.in_(select(BookingMaster.booking_id).where(BookingMaster.master_id == current_user.id)),
                    Booking.id.in_(select(BookingStaff.booking_id).where(BookingStaff.user_id == current_user.id)),
                )
            )
        for (dt0,) in db.execute(b_stmt).all():
            if isinstance(dt0, datetime):
                bookings_by_day[_utc_naive_to_local_date(dt0)] += 1

        if visit_ids:
            v_pay = list(
                db.execute(
                    select(Visit.id, Visit.performed_date, func.coalesce(func.sum(PayrollFundLedger.amount), 0.0))
                    .join(PayrollFundLedger, PayrollFundLedger.source_id == Visit.id)
                    .where(
                        PayrollFundLedger.side == PayrollFundSide.MASTER,
                        PayrollFundLedger.user_id == current_user.id,
                        PayrollFundLedger.source_kind == PayrollFundSourceKind.VISIT,
                        Visit.id.in_(visit_ids),
                    )
                    .group_by(Visit.id, Visit.performed_date)
                ).all()
            )
            for _, dt0, amt in v_pay:
                if isinstance(dt0, datetime):
                    payroll_sum_by_day[_utc_naive_to_local_date(dt0)] += float(amt or 0.0)

        if work_ids:
            w_pay = list(
                db.execute(
                    select(
                        WorkForInventory.id,
                        func.coalesce(WorkForInventory.performed_date, WorkForInventory.created_at),
                        func.coalesce(func.sum(PayrollFundLedger.amount), 0.0),
                    )
                    .join(PayrollFundLedger, PayrollFundLedger.source_id == WorkForInventory.id)
                    .where(
                        PayrollFundLedger.side == PayrollFundSide.MASTER,
                        PayrollFundLedger.user_id == current_user.id,
                        PayrollFundLedger.source_kind == PayrollFundSourceKind.WORK,
                        WorkForInventory.id.in_(work_ids),
                    )
                    .group_by(WorkForInventory.id, func.coalesce(WorkForInventory.performed_date, WorkForInventory.created_at))
                ).all()
            )
            for _, dt0, amt in w_pay:
                if isinstance(dt0, datetime):
                    payroll_sum_by_day[_utc_naive_to_local_date(dt0)] += float(amt or 0.0)

        s_pay = list(
            db.execute(
                select(ProductSale.id, ProductSale.performed_date, func.coalesce(func.sum(PayrollFundLedger.amount), 0.0))
                .join(PayrollFundLedger, PayrollFundLedger.source_id == ProductSale.id)
                .where(
                    PayrollFundLedger.side == PayrollFundSide.MASTER,
                    PayrollFundLedger.user_id == current_user.id,
                    PayrollFundLedger.source_kind == PayrollFundSourceKind.PRODUCT_SALE,
                    ProductSale.performed_date >= month_start_utc,
                    ProductSale.performed_date < month_end_utc,
                    ProductSale.is_voided.is_(False),
                )
                .group_by(ProductSale.id, ProductSale.performed_date)
            ).all()
        )
        for _, dt0, amt in s_pay:
            if isinstance(dt0, datetime):
                payroll_sum_by_day[_utc_naive_to_local_date(dt0)] += float(amt or 0.0)

        other_stmt = (
            select(PayrollFundLedger.created_at, PayrollFundLedger.amount)
            .where(
                PayrollFundLedger.side == PayrollFundSide.MASTER,
                PayrollFundLedger.user_id == current_user.id,
                PayrollFundLedger.created_at >= month_start_utc,
                PayrollFundLedger.created_at < month_end_utc,
                PayrollFundLedger.source_kind.notin_(
                    (
                        PayrollFundSourceKind.VISIT,
                        PayrollFundSourceKind.WORK,
                        PayrollFundSourceKind.PRODUCT_SALE,
                    )
                ),
            )
        )
        for dt0, amt in db.execute(other_stmt).all():
            if isinstance(dt0, datetime):
                payroll_sum_by_day[_utc_naive_to_local_date(dt0)] += float(amt or 0.0)

        period_ctx: dict[str, Any] | None = None
        if show_studio:
            today = now_local.date()
            p = db.scalar(
                select(PayrollPeriod)
                .where(PayrollPeriod.date_from <= datetime.combine(today, time.max), PayrollPeriod.date_to >= datetime.combine(today, time.min))
                .order_by(case((PayrollPeriod.closed_at.is_(None), 0), else_=1), PayrollPeriod.date_from.desc(), PayrollPeriod.id.desc())
                .limit(1)
            )
            if p is None:
                p = db.scalar(select(PayrollPeriod).order_by(PayrollPeriod.date_from.desc(), PayrollPeriod.id.desc()).limit(1))
            if p is not None:
                period_end = p.date_to
                if p.closed_at is None:
                    from app.payroll_utils import payroll_period_day_end

                    period_end = payroll_period_day_end(today)
                expenses_sum = (
                    db.scalar(
                        select(func.coalesce(func.sum(StudioExpense.amount), 0.0)).where(
                            StudioExpense.is_voided.is_(False),
                            StudioExpense.date >= p.date_from,
                            StudioExpense.date <= period_end,
                        )
                    )
                    or 0.0
                )
                period_ctx = {"id": p.id, "date_from": p.date_from, "date_to": period_end, "expenses_sum": _money0(expenses_sum)}
        payroll_home["period"] = period_ctx

        cal = calendar.Calendar(firstweekday=0)  # Mon
        weeks = []
        for week in cal.monthdatescalendar(year, month):
            w = []
            for d0 in week:
                w.append(
                    {
                        "date": d0,
                        "iso": d0.isoformat(),
                        "in_month": (d0.month == month),
                        "visits": int(visits_by_day.get(d0, 0)),
                        "bookings": int(bookings_by_day.get(d0, 0)),
                        "works": int(works_by_day.get(d0, 0)),
                        "payroll_sum": float(payroll_sum_by_day.get(d0, 0.0)),
                        "is_today": (d0 == now_local.date()),
                    }
                )
            weeks.append(w)

        prev_month = (month_local_start - timedelta(days=1)).date().replace(day=1)
        next_month = next_month_local_start.date().replace(day=1)
        months_ru = ("январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь")
        calendar_ctx = {
            "display_tz": display_tz,
            "year": year,
            "month": month,
            "label": f"{months_ru[month - 1].capitalize()} {year}",
            "ym": f"{year:04d}-{month:02d}",
            "prev_ym": f"{prev_month.year:04d}-{prev_month.month:02d}",
            "next_ym": f"{next_month.year:04d}-{next_month.month:02d}",
            "weeks": weeks,
        }

        sections_ctx = {"is_master": current_user.role == UserRole.MASTER, "is_admin_super": current_user.role == UserRole.ADMIN_SUPER}

    return templates.TemplateResponse(
        "home.html",
        _ctx(request, current_user=current_user, payroll_home=payroll_home, calendar_ctx=calendar_ctx, sections_ctx=sections_ctx),
    )


@router.get("/service-catalog", response_class=HTMLResponse)
def service_catalog_view(
    request: Request,
    category_id: int | None = None,
    subcategory_id: int | None = None,
    current_user: AuthUser = Depends(require_role(UserRole.MASTER, UserRole.ADMIN, UserRole.ADMIN_SUPER)),
    db: Session = Depends(get_db),
):
    if category_id is not None and category_id <= 0:
        category_id = None
    if subcategory_id is not None and subcategory_id <= 0:
        subcategory_id = None

    _EXCLUDED_CATS = ("Заказ", "Продажа материала")
    categories = list(
        db.scalars(
            select(ServiceCategory)
            .where(
                ServiceCategory.is_active.is_(True),
                ServiceCategory.name.not_in(_EXCLUDED_CATS),
            )
            .order_by(ServiceCategory.name.asc())
        ).all()
    )

    selected_category: ServiceCategory | None = None
    subcategories: list[ServiceSubcategory] = []
    selected_subcategory: ServiceSubcategory | None = None
    services: list[Service] = []
    mismatch = False

    sub_from_q: ServiceSubcategory | None = None
    if subcategory_id is not None and subcategory_id > 0:
        sub_from_q = db.scalar(
            select(ServiceSubcategory)
            .options(selectinload(ServiceSubcategory.category))
            .where(ServiceSubcategory.id == subcategory_id, ServiceSubcategory.is_active.is_(True))
        )

    if (
        sub_from_q
        and sub_from_q.category
        and sub_from_q.category.is_active
        and (sub_from_q.category.name or "").strip() not in _EXCLUDED_CATS
    ):
        if category_id is not None and category_id > 0 and category_id != sub_from_q.category_id:
            mismatch = True
            selected_category = db.scalar(
                select(ServiceCategory).where(
                    ServiceCategory.id == category_id,
                    ServiceCategory.is_active.is_(True),
                    ServiceCategory.name.not_in(_EXCLUDED_CATS),
                )
            )
            if selected_category:
                subcategories = list(
                    db.scalars(
                        select(ServiceSubcategory)
                        .where(ServiceSubcategory.category_id == category_id, ServiceSubcategory.is_active.is_(True))
                        .order_by(ServiceSubcategory.name.asc())
                    ).all()
                )
        else:
            selected_subcategory = sub_from_q
            selected_category = sub_from_q.category
            subcategories = list(
                db.scalars(
                    select(ServiceSubcategory)
                    .where(ServiceSubcategory.category_id == selected_category.id, ServiceSubcategory.is_active.is_(True))
                    .order_by(ServiceSubcategory.name.asc())
                ).all()
            )
            services = list(
                db.scalars(
                    select(Service)
                    .where(Service.subcategory_id == sub_from_q.id)
                    .order_by(Service.is_active.desc(), Service.name.asc())
                ).all()
            )
    elif category_id is not None and category_id > 0:
        selected_category = db.scalar(
            select(ServiceCategory).where(
                ServiceCategory.id == category_id,
                ServiceCategory.is_active.is_(True),
                ServiceCategory.name.not_in(_EXCLUDED_CATS),
            )
        )
        if selected_category:
            subcategories = list(
                db.scalars(
                    select(ServiceSubcategory)
                    .where(ServiceSubcategory.category_id == category_id, ServiceSubcategory.is_active.is_(True))
                    .order_by(ServiceSubcategory.name.asc())
                ).all()
            )
            if (subcategory_id is None or subcategory_id <= 0) and len(subcategories) == 1:
                selected_subcategory = subcategories[0]
                services = list(
                    db.scalars(
                        select(Service)
                        .where(Service.subcategory_id == selected_subcategory.id)
                        .order_by(Service.is_active.desc(), Service.name.asc())
                    ).all()
                )

    return templates.TemplateResponse(
        "service_catalog_view.html",
        _ctx(
            request,
            current_user=current_user,
            title="Прайс · Услуги",
            categories=categories,
            selected_category=selected_category,
            subcategories=subcategories,
            selected_subcategory=selected_subcategory,
            services=services,
            mismatch=mismatch,
        ),
    )


@router.get("/price", response_class=HTMLResponse)
def price_index(
    request: Request,
    current_user: AuthUser = Depends(require_role(UserRole.MASTER, UserRole.ADMIN, UserRole.ADMIN_SUPER)),
):
    return templates.TemplateResponse("price_index.html", _ctx(request, current_user=current_user))

