from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.auth import AuthUser, require_techspec_user
from app.db.session import get_db
from app.techspec_home import collect_db_table_stats, collect_techspec_home_stats, execute_readonly_sql
from app.webui import ctx as _ctx, templates

router = APIRouter(prefix="/techspec", tags=["techspec"])


def _render_data_page(
    request: Request,
    current_user: AuthUser,
    db: Session,
    *,
    status_code: int = 200,
):
    return templates.TemplateResponse(
        "techspec_data.html",
        _ctx(
            request,
            current_user=current_user,
            title="Технические данные",
            techspec_home=collect_techspec_home_stats(db),
            db_tables=collect_db_table_stats(db),
        ),
        status_code=status_code,
    )


def _render_sql_page(
    request: Request,
    current_user: AuthUser,
    *,
    sql_query: str = "",
    sql_result: dict | None = None,
    sql_error: str | None = None,
    status_code: int = 200,
):
    return templates.TemplateResponse(
        "techspec_sql.html",
        _ctx(
            request,
            current_user=current_user,
            title="SQL",
            sql_query=sql_query,
            sql_result=sql_result,
            sql_error=sql_error,
        ),
        status_code=status_code,
    )


@router.get("/data", response_class=HTMLResponse)
def techspec_data_page(
    request: Request,
    current_user: AuthUser = Depends(require_techspec_user()),
    db: Session = Depends(get_db),
):
    return _render_data_page(request, current_user, db)


@router.get("/sql", response_class=HTMLResponse)
def techspec_sql_page(
    request: Request,
    current_user: AuthUser = Depends(require_techspec_user()),
):
    return _render_sql_page(request, current_user)


@router.post("/data/sql", response_class=HTMLResponse)
def techspec_data_sql_legacy(
    request: Request,
    sql_query: str = Form(""),
    current_user: AuthUser = Depends(require_techspec_user()),
    db: Session = Depends(get_db),
):
    """Старый URL формы — делегирует на /techspec/sql."""
    return techspec_sql_execute(request, sql_query=sql_query, current_user=current_user, db=db)


@router.post("/sql", response_class=HTMLResponse)
def techspec_sql_execute(
    request: Request,
    sql_query: str = Form(""),
    current_user: AuthUser = Depends(require_techspec_user()),
    db: Session = Depends(get_db),
):
    try:
        sql_result = execute_readonly_sql(db, sql_query)
        return _render_sql_page(
            request,
            current_user,
            sql_query=sql_query,
            sql_result=sql_result,
        )
    except Exception as exc:
        return _render_sql_page(
            request,
            current_user,
            sql_query=sql_query,
            sql_error=str(exc),
            status_code=400,
        )
