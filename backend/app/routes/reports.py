from __future__ import annotations

from datetime import date
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.auth import AuthUser, require_role
from app.db.models import ProductSaleKind, UserRole
from app.db.session import get_db
from app.forms_parse import parse_date_iso
from app.operational_report import (
    build_operational_report,
    list_closed_payroll_periods,
    list_report_consultations,
    list_report_hourly,
    list_report_sales,
    list_report_visits,
    list_report_works,
    report_to_csv,
    resolve_report_dates,
    result_to_template_dict,
)
from app.webui import templates, ctx as _ctx


router = APIRouter()


def _admin_report_export_params(mode: str, d0: date, d1: date, selected_period_id: int | None) -> dict[str, str]:
    p: dict[str, str] = {"report_mode": mode, "df": d0.isoformat(), "dt": d1.isoformat()}
    if selected_period_id is not None:
        p["period_id"] = str(selected_period_id)
    return p


def _report_detail_dates(df: str | None, dt: str | None, month_start: date, today: date) -> tuple[date, date]:
    try:
        d0 = parse_date_iso(df, field_name="df") if df else month_start
        d1 = parse_date_iso(dt, field_name="dt") if dt else today
    except ValueError:
        d0, d1 = month_start, today
    if d1 < d0:
        d0, d1 = d1, d0
    return d0, d1


def _reports_nav_query(report_mode: str | None, period_id: str | None, d0: date, d1: date) -> str:
    p: dict[str, str] = {"df": d0.isoformat(), "dt": d1.isoformat()}
    if report_mode and report_mode.strip():
        p["report_mode"] = report_mode.strip()
    if period_id is not None and str(period_id).strip() != "":
        p["period_id"] = str(period_id).strip()
    return urlencode(p)


@router.get("/admin/reports", response_class=HTMLResponse)
def admin_operational_report_page(
    request: Request,
    report_mode: str | None = Query(None),
    period_id: str | None = Query(None),
    df: str | None = Query(None),
    dt: str | None = Query(None),
    current_user: AuthUser = Depends(require_role(UserRole.ADMIN_SUPER)),
    db: Session = Depends(get_db),
):
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

    report = build_operational_report(db, d0, d1)
    report_dict = result_to_template_dict(report)
    exp_base = _admin_report_export_params(mode, d0, d1, selected_period_id)
    export_csv_url = "/admin/reports/export?" + urlencode({**exp_base, "format": "csv"})
    export_print_url = "/admin/reports/export?" + urlencode({**exp_base, "format": "print"})
    reports_filter_q = urlencode(exp_base)
    return templates.TemplateResponse(
        "admin_operational_report.html",
        _ctx(
            request,
            current_user=current_user,
            title="Отчёт",
            closed_periods=closed_periods,
            report_mode=mode,
            selected_period_id=selected_period_id,
            form_df=d0.isoformat(),
            form_dt=d1.isoformat(),
            export_csv_url=export_csv_url,
            export_print_url=export_print_url,
            reports_filter_q=reports_filter_q,
            **report_dict,
        ),
    )


@router.get("/admin/reports/export")
def admin_operational_report_export(
    request: Request,
    export_format: str = Query("csv", alias="format"),
    report_mode: str | None = Query(None),
    period_id: str | None = Query(None),
    df: str | None = Query(None),
    dt: str | None = Query(None),
    current_user: AuthUser = Depends(require_role(UserRole.ADMIN_SUPER)),
    db: Session = Depends(get_db),
):
    today = date.today()
    month_start = today.replace(day=1)
    d0, d1, _, _mode = resolve_report_dates(
        db,
        report_mode=report_mode,
        period_id_raw=period_id,
        df_raw=df,
        dt_raw=dt,
        month_start=month_start,
        today=today,
    )
    report = build_operational_report(db, d0, d1)
    fmt = (export_format or "csv").strip().lower()
    if fmt == "csv":
        body = report_to_csv(report).encode("utf-8-sig")
        fn = f"report_{d0.isoformat()}_{d1.isoformat()}.csv"
        return Response(
            content=body,
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{fn}"'},
        )
    if fmt == "print":
        return templates.TemplateResponse(
            "admin_report_print.html",
            _ctx(request, current_user=current_user, title="Отчёт — печать", **result_to_template_dict(report)),
        )
    return RedirectResponse(url="/admin/reports", status_code=303)


@router.get("/admin/reports/visits", response_class=HTMLResponse)
def admin_report_visits_list(
    request: Request,
    report_mode: str | None = Query(None),
    period_id: str | None = Query(None),
    df: str | None = Query(None),
    dt: str | None = Query(None),
    current_user: AuthUser = Depends(require_role(UserRole.ADMIN_SUPER)),
    db: Session = Depends(get_db),
):
    today = date.today()
    month_start = today.replace(day=1)
    d0, d1 = _report_detail_dates(df, dt, month_start, today)
    rows = list_report_visits(db, d0, d1)
    reports_nav_q = _reports_nav_query(report_mode, period_id, d0, d1)
    return templates.TemplateResponse(
        "admin_report_visits.html",
        _ctx(request, current_user=current_user, title="Визиты за период", rows=rows, date_from=d0, date_to=d1, reports_nav_q=reports_nav_q),
    )


@router.get("/admin/reports/sales", response_class=HTMLResponse)
def admin_report_sales_list(
    request: Request,
    report_mode: str | None = Query(None),
    period_id: str | None = Query(None),
    df: str | None = Query(None),
    dt: str | None = Query(None),
    current_user: AuthUser = Depends(require_role(UserRole.ADMIN_SUPER)),
    db: Session = Depends(get_db),
):
    today = date.today()
    month_start = today.replace(day=1)
    d0, d1 = _report_detail_dates(df, dt, month_start, today)
    rows = list_report_sales(db, d0, d1)
    reports_nav_q = _reports_nav_query(report_mode, period_id, d0, d1)
    return templates.TemplateResponse(
        "admin_report_sales.html",
        _ctx(
            request,
            current_user=current_user,
            title="Продажи за период",
            rows=rows,
            date_from=d0,
            date_to=d1,
            reports_nav_q=reports_nav_q,
            ProductSaleKind=ProductSaleKind,
        ),
    )


@router.get("/admin/reports/works", response_class=HTMLResponse)
def admin_report_works_list(
    request: Request,
    report_mode: str | None = Query(None),
    period_id: str | None = Query(None),
    df: str | None = Query(None),
    dt: str | None = Query(None),
    current_user: AuthUser = Depends(require_role(UserRole.ADMIN_SUPER)),
    db: Session = Depends(get_db),
):
    today = date.today()
    month_start = today.replace(day=1)
    d0, d1 = _report_detail_dates(df, dt, month_start, today)
    rows = list_report_works(db, d0, d1)
    reports_nav_q = _reports_nav_query(report_mode, period_id, d0, d1)
    return templates.TemplateResponse(
        "admin_report_works.html",
        _ctx(request, current_user=current_user, title="Работы за период", rows=rows, date_from=d0, date_to=d1, reports_nav_q=reports_nav_q),
    )


@router.get("/admin/reports/consultations", response_class=HTMLResponse)
def admin_report_consultations_list(
    request: Request,
    report_mode: str | None = Query(None),
    period_id: str | None = Query(None),
    df: str | None = Query(None),
    dt: str | None = Query(None),
    current_user: AuthUser = Depends(require_role(UserRole.ADMIN_SUPER)),
    db: Session = Depends(get_db),
):
    today = date.today()
    month_start = today.replace(day=1)
    d0, d1 = _report_detail_dates(df, dt, month_start, today)
    rows = list_report_consultations(db, d0, d1)
    reports_nav_q = _reports_nav_query(report_mode, period_id, d0, d1)
    return templates.TemplateResponse(
        "admin_report_consultations.html",
        _ctx(
            request,
            current_user=current_user,
            title="Консультации за период",
            rows=rows,
            date_from=d0,
            date_to=d1,
            reports_nav_q=reports_nav_q,
        ),
    )


@router.get("/admin/reports/hourly", response_class=HTMLResponse)
def admin_report_hourly_list(
    request: Request,
    report_mode: str | None = Query(None),
    period_id: str | None = Query(None),
    df: str | None = Query(None),
    dt: str | None = Query(None),
    current_user: AuthUser = Depends(require_role(UserRole.ADMIN_SUPER)),
    db: Session = Depends(get_db),
):
    today = date.today()
    month_start = today.replace(day=1)
    d0, d1 = _report_detail_dates(df, dt, month_start, today)
    rows = list_report_hourly(db, d0, d1)
    reports_nav_q = _reports_nav_query(report_mode, period_id, d0, d1)
    return templates.TemplateResponse(
        "admin_report_hourly.html",
        _ctx(
            request,
            current_user=current_user,
            title="Почасовая за период",
            rows=rows,
            date_from=d0,
            date_to=d1,
            reports_nav_q=reports_nav_q,
        ),
    )

