from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import AuthUser, require_role
from app.db.models import User, UserRole
from app.db.session import get_db
from app.master_statistics import build_master_statistics
from app.operational_report import list_closed_payroll_periods, resolve_report_dates
from app.user_roles import select_users_with_any_role
from app.webui import templates, ctx as _ctx


router = APIRouter()


@router.get("/admin/statistics", response_class=HTMLResponse)
def admin_master_statistics_page(
    request: Request,
    master_id: str | None = Query(None),
    report_mode: str | None = Query(None),
    display_mode: str | None = Query(None),
    period_id: str | None = Query(None),
    df: str | None = Query(None),
    dt: str | None = Query(None),
    current_user: AuthUser = Depends(require_role(UserRole.ADMIN_SUPER)),
    db: Session = Depends(get_db),
):
    selected_display_mode = "all" if str(display_mode or "").strip().lower() == "all" else "categories"
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
            selected_period_id=selected_period_id,
            form_df=d0.isoformat(),
            form_dt=d1.isoformat(),
            stats=stats,
        ),
    )
