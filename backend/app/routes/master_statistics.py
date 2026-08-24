from __future__ import annotations

from datetime import date
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import AuthUser, require_role
from app.db.models import User, UserRole
from app.db.session import get_db
from app.master_statistics import (
    build_master_statistics,
    parse_master_statistics_date_sort,
    sort_master_stats_daily_rows,
)
from app.operational_report import list_closed_payroll_periods, resolve_report_dates
from app.user_roles import select_users_with_any_role
from app.webui import templates, ctx as _ctx


router = APIRouter()


def _statistics_page_url(
    *,
    master_id: int | None,
    report_mode: str,
    display_mode: str,
    date_sort: str,
    period_id: int | None,
    df: str,
    dt: str,
) -> str:
    params: dict[str, str | int] = {
        "master_id": int(master_id),
        "report_mode": report_mode,
        "display_mode": display_mode,
        "date_sort": date_sort,
    }
    if report_mode == "payroll_period" and period_id is not None:
        params["period_id"] = int(period_id)
    else:
        params["df"] = df
        params["dt"] = dt
    return "/admin/statistics?" + urlencode(params)


@router.get("/admin/statistics", response_class=HTMLResponse)
def admin_master_statistics_page(
    request: Request,
    master_id: str | None = Query(None),
    report_mode: str | None = Query(None),
    display_mode: str | None = Query(None),
    date_sort: str | None = Query(None),
    period_id: str | None = Query(None),
    df: str | None = Query(None),
    dt: str | None = Query(None),
    current_user: AuthUser = Depends(require_role(UserRole.ADMIN_SUPER)),
    db: Session = Depends(get_db),
):
    selected_display_mode = "all" if str(display_mode or "").strip().lower() == "all" else "categories"
    selected_date_sort = parse_master_statistics_date_sort(date_sort)
    closed_periods = list_closed_payroll_periods(db)
    today = date.today()
    month_start = today.replace(day=1)
    d0, d1, selected_period_id, mode = resolve_report_dates(
        db,
        report_mode=report_mode,
        period_id_raw=period_id,
        df_raw=df,
        dt_raw=dt,
        month_start=month_start,
        today=today,
    )

    masters = list(
        db.scalars(
            select_users_with_any_role(UserRole.MASTER)
            .where(User.is_active.is_(True))
            .order_by(User.display_name.asc())
        ).all()
    )

    selected_master_id: int | None = None
    if master_id and str(master_id).strip().isdigit():
        pid = int(str(master_id).strip())
        if any(int(m.id) == pid for m in masters):
            selected_master_id = pid

    stats = None
    if selected_master_id is not None:
        stats = build_master_statistics(db, selected_master_id, d0, d1)
        if stats is not None:
            stats.daily_rows = sort_master_stats_daily_rows(stats.daily_rows, order=selected_date_sort)

    sort_url_asc = None
    sort_url_desc = None
    if selected_master_id is not None:
        sort_url_asc = _statistics_page_url(
            master_id=selected_master_id,
            report_mode=mode,
            display_mode=selected_display_mode,
            date_sort="asc",
            period_id=selected_period_id,
            df=d0.isoformat(),
            dt=d1.isoformat(),
        )
        sort_url_desc = _statistics_page_url(
            master_id=selected_master_id,
            report_mode=mode,
            display_mode=selected_display_mode,
            date_sort="desc",
            period_id=selected_period_id,
            df=d0.isoformat(),
            dt=d1.isoformat(),
        )

    return templates.TemplateResponse(
        "admin_master_statistics.html",
        _ctx(
            request,
            current_user=current_user,
            title="Статистика",
            masters=masters,
            selected_master_id=selected_master_id,
            closed_periods=closed_periods,
            report_mode=mode,
            display_mode=selected_display_mode,
            date_sort=selected_date_sort,
            sort_url_asc=sort_url_asc,
            sort_url_desc=sort_url_desc,
            selected_period_id=selected_period_id,
            form_df=d0.isoformat(),
            form_dt=d1.isoformat(),
            stats=stats,
        ),
    )
