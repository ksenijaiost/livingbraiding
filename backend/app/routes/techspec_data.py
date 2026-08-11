from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.auth import AuthUser, require_techspec_user
from app.db.session import get_db
from app.techspec_home import collect_db_table_stats, collect_techspec_home_stats, execute_readonly_sql
from app.webui import ctx as _ctx, templates

router = APIRouter(prefix="/techspec", tags=["techspec"])


def _render_page(
    request: Request,
    current_user: AuthUser,
    db: Session,
    *,
    sql_query: str = "",
    sql_result: dict | None = None,
    sql_error: str | None = None,
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
    return _render_page(request, current_user, db)


@router.post("/data/sql", response_class=HTMLResponse)
def techspec_data_sql(
    request: Request,
    sql_query: str = Form(""),
    current_user: AuthUser = Depends(require_techspec_user()),
    db: Session = Depends(get_db),
):
    try:
        sql_result = execute_readonly_sql(db, sql_query)
        return _render_page(
            request,
            current_user,
            db,
            sql_query=sql_query,
            sql_result=sql_result,
        )
    except Exception as exc:
        return _render_page(
            request,
            current_user,
            db,
            sql_query=sql_query,
            sql_error=str(exc),
            status_code=400,
        )
