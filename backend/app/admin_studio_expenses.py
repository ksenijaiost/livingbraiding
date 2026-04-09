"""Этап 8: расходы студии — только суперадмин."""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload
from starlette.datastructures import UploadFile

from app.auth import AuthUser, require_role
from app.db.models import (
    StudioExpense,
    StudioExpenseCategory,
    StudioExpenseSubcategory,
    UserRole,
)
from app.db.session import get_db
from app.visit_edit_policy import is_in_closed_payroll_period

templates = Jinja2Templates(directory="app/templates")

router = APIRouter(prefix="/admin/expenses", tags=["admin-studio-expenses"])
_SUPER = Depends(require_role(UserRole.ADMIN_SUPER))


def _ctx(request: Request, current_user: AuthUser, **kwargs):
    return {"request": request, "current_user": current_user, **kwargs}


def _g_str(form: Any, name: str, default: str = "") -> str:
    v = form.get(name)
    if v is None or isinstance(v, UploadFile):
        return default
    if isinstance(v, (bytes, bytearray)):
        return v.decode().strip()
    return str(v).strip()


def _parse_expense_date(raw: str) -> datetime | None:
    s = (raw or "").strip()
    if not s:
        return None
    try:
        return datetime.combine(date.fromisoformat(s), datetime.min.time())
    except ValueError:
        return None


def _parse_amount(raw: str) -> float | None:
    s = (raw or "").strip().replace(",", ".")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _expense_touch_allowed(db: Session, expense_dt: datetime) -> tuple[bool, str]:
    if is_in_closed_payroll_period(db, expense_dt):
        return False, "Дата расхода попадает в закрытый период ЗП — операция запрещена."
    return True, ""


def _load_categories(db: Session) -> list[StudioExpenseCategory]:
    return list(
        db.scalars(
            select(StudioExpenseCategory)
            .where(StudioExpenseCategory.is_active.is_(True))
            .order_by(StudioExpenseCategory.sort_order.asc(), StudioExpenseCategory.name.asc())
            .options(selectinload(StudioExpenseCategory.subcategories))
        ).all()
    )


def _subcats_for_category(db: Session, category_id: int) -> list[StudioExpenseSubcategory]:
    return list(
        db.scalars(
            select(StudioExpenseSubcategory)
            .where(
                StudioExpenseSubcategory.category_id == category_id,
                StudioExpenseSubcategory.is_active.is_(True),
            )
            .order_by(StudioExpenseSubcategory.sort_order.asc(), StudioExpenseSubcategory.name.asc())
        ).all()
    )


@router.get("/api/subcategories", response_class=JSONResponse)
def api_subcategories(
    category_id: int,
    current_user: AuthUser = _SUPER,
    db: Session = Depends(get_db),
):
    rows = _subcats_for_category(db, category_id)
    return JSONResponse({"subcategories": [{"id": s.id, "name": s.name} for s in rows]})


@router.get("", response_class=HTMLResponse)
def studio_expenses_list(
    request: Request,
    current_user: AuthUser = _SUPER,
    db: Session = Depends(get_db),
    date_from: str | None = None,
    date_to: str | None = None,
    category_id: int | None = None,
    edit: int | None = None,
    new: int | None = None,
    msg: str | None = None,
):
    categories = _load_categories(db)
    q = (
        select(StudioExpense)
        .options(
            selectinload(StudioExpense.subcategory).selectinload(StudioExpenseSubcategory.category),
            selectinload(StudioExpense.created_by_user),
            selectinload(StudioExpense.voided_by_user),
        )
        .order_by(StudioExpense.date.desc(), StudioExpense.id.desc())
    )
    if date_from:
        d0 = _parse_expense_date(date_from)
        if d0:
            q = q.where(StudioExpense.date >= d0)
    if date_to:
        d1 = _parse_expense_date(date_to)
        if d1:
            end = datetime.combine(d1.date(), time(23, 59, 59))
            q = q.where(StudioExpense.date <= end)
    if category_id and category_id > 0:
        subq = select(StudioExpenseSubcategory.id).where(
            StudioExpenseSubcategory.category_id == category_id
        )
        q = q.where(StudioExpense.subcategory_id.in_(subq))

    rows = list(db.scalars(q).all())

    filter_params: dict[str, str] = {}
    if date_from:
        filter_params["date_from"] = date_from
    if date_to:
        filter_params["date_to"] = date_to
    if category_id and category_id > 0:
        filter_params["category_id"] = str(category_id)
    filter_query = urlencode(filter_params)

    subcats_json: dict[int, list[dict[str, Any]]] = {}
    for c in categories:
        subcats_json[c.id] = [{"id": s.id, "name": s.name} for s in c.subcategories if s.is_active]

    return templates.TemplateResponse(
        "admin_studio_expenses.html",
        _ctx(
            request,
            current_user=current_user,
            rows=rows,
            categories=categories,
            subcats_json=subcats_json,
            date_from=date_from or "",
            date_to=date_to or "",
            filter_category_id=category_id or 0,
            edit_id=edit,
            show_new=bool(new),
            msg=msg,
            today_iso=date.today().isoformat(),
            filter_query=filter_query,
        ),
    )


@router.post("/new")
async def studio_expense_new(
    request: Request,
    current_user: AuthUser = _SUPER,
    db: Session = Depends(get_db),
):
    form = await request.form()
    expense_dt = _parse_expense_date(_g_str(form, "expense_date"))
    if not expense_dt:
        return RedirectResponse(url="/admin/expenses?new=1&msg=bad_date", status_code=303)
    ok, _ = _expense_touch_allowed(db, expense_dt)
    if not ok:
        return RedirectResponse(url="/admin/expenses?new=1&msg=closed_period", status_code=303)

    sid_raw = _g_str(form, "subcategory_id")
    if not sid_raw.isdigit():
        return RedirectResponse(url="/admin/expenses?new=1&msg=subcat", status_code=303)
    sub = db.get(StudioExpenseSubcategory, int(sid_raw))
    if not sub or not sub.is_active:
        return RedirectResponse(url="/admin/expenses?new=1&msg=subcat", status_code=303)

    amt = _parse_amount(_g_str(form, "amount"))
    if amt is None or amt < 0:
        return RedirectResponse(url="/admin/expenses?new=1&msg=amount", status_code=303)

    comment = _g_str(form, "comment")

    row = StudioExpense(
        created_by_user_id=current_user.id,
        created_at=datetime.utcnow(),
        date=expense_dt,
        subcategory_id=sub.id,
        amount=float(amt),
        comment=comment or "",
    )
    db.add(row)
    db.commit()

    params: list[str] = ["msg=saved"]
    df = _g_str(form, "_filter_date_from")
    dt = _g_str(form, "_filter_date_to")
    fc = _g_str(form, "_filter_category_id")
    if df:
        params.append("date_from=" + df)
    if dt:
        params.append("date_to=" + dt)
    if fc.isdigit() and int(fc) > 0:
        params.append("category_id=" + fc)
    return RedirectResponse(url="/admin/expenses?" + "&".join(params), status_code=303)


@router.post("/{expense_id}/save")
async def studio_expense_save(
    expense_id: int,
    request: Request,
    current_user: AuthUser = _SUPER,
    db: Session = Depends(get_db),
):
    row = db.get(StudioExpense, expense_id)
    if not row or row.is_voided:
        return RedirectResponse(url="/admin/expenses?msg=not_found", status_code=303)

    ok_old, _ = _expense_touch_allowed(db, row.date)
    if not ok_old:
        return RedirectResponse(url="/admin/expenses?msg=closed_period", status_code=303)

    form = await request.form()
    expense_dt = _parse_expense_date(_g_str(form, "expense_date"))
    if not expense_dt:
        return RedirectResponse(url=f"/admin/expenses?edit={expense_id}&msg=bad_date", status_code=303)
    ok_new, _ = _expense_touch_allowed(db, expense_dt)
    if not ok_new:
        return RedirectResponse(url=f"/admin/expenses?edit={expense_id}&msg=closed_period", status_code=303)

    sid_raw = _g_str(form, "subcategory_id")
    if not sid_raw.isdigit():
        return RedirectResponse(url=f"/admin/expenses?edit={expense_id}&msg=subcat", status_code=303)
    sub = db.get(StudioExpenseSubcategory, int(sid_raw))
    if not sub or not sub.is_active:
        return RedirectResponse(url=f"/admin/expenses?edit={expense_id}&msg=subcat", status_code=303)

    amt = _parse_amount(_g_str(form, "amount"))
    if amt is None or amt < 0:
        return RedirectResponse(url=f"/admin/expenses?edit={expense_id}&msg=amount", status_code=303)

    comment = _g_str(form, "comment")

    row.date = expense_dt
    row.subcategory_id = sub.id
    row.amount = float(amt)
    row.comment = comment or ""
    db.commit()

    params: list[str] = []
    df = _g_str(form, "_filter_date_from")
    dt = _g_str(form, "_filter_date_to")
    fc = _g_str(form, "_filter_category_id")
    if df:
        params.append("date_from=" + df)
    if dt:
        params.append("date_to=" + dt)
    if fc.isdigit() and int(fc) > 0:
        params.append("category_id=" + fc)
    params.append("msg=updated")
    return RedirectResponse(url="/admin/expenses?" + "&".join(params), status_code=303)


@router.post("/{expense_id}/void")
async def studio_expense_void(
    expense_id: int,
    current_user: AuthUser = _SUPER,
    db: Session = Depends(get_db),
):
    row = db.get(StudioExpense, expense_id)
    if not row or row.is_voided:
        return RedirectResponse(url="/admin/expenses?msg=not_found", status_code=303)
    ok, _ = _expense_touch_allowed(db, row.date)
    if not ok:
        return RedirectResponse(url="/admin/expenses?msg=void_closed", status_code=303)

    row.is_voided = True
    row.voided_at = datetime.utcnow()
    row.voided_by_user_id = current_user.id
    db.commit()
    return RedirectResponse(url="/admin/expenses?msg=voided", status_code=303)
