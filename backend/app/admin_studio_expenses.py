"""Этап 8: расходы студии — только суперадмин."""

from __future__ import annotations

from datetime import date, datetime, time
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload
from starlette.datastructures import UploadFile

from app.auth import AuthUser, require_role
from app.audit import diff_fields, write_audit_rows
from app.audit_field_labels import audit_field_label
from app.db.models import (
    StudioExpense,
    StudioExpenseCategory,
    StudioExpenseSubcategory,
    StudioExpenseAuditLog,
    UserRole,
)
from app.db.session import get_db
from app.display_time import format_naive_utc_datetime, get_display_timezone
from app.forms_parse import parse_date_iso, parse_float, parse_int
from app.payroll_fund import replace_studio_expense_ledger, studio_fund_balance
from app.time_utils import utcnow_naive
from app.visit_edit_policy import ensure_event_date_in_open_payroll_period
from app.webui import ctx as _ctx, templates

router = APIRouter(prefix="/admin/expenses", tags=["admin-studio-expenses"])
_SUPER = Depends(require_role(UserRole.ADMIN_SUPER))


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
        d = parse_date_iso(s, field_name="expense_date")
    except ValueError:
        return None
    return datetime.combine(d, datetime.min.time())


def _parse_amount(raw: str) -> float | None:
    s = (raw or "").strip()
    if not s:
        return None
    try:
        return parse_float(s, field_name="amount")
    except ValueError:
        return None


def _expense_touch_allowed(db: Session, expense_dt: datetime) -> tuple[bool, str]:
    try:
        ensure_event_date_in_open_payroll_period(db, expense_dt)
    except ValueError as e:
        return False, str(e)
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
            studio_fund_balance=studio_fund_balance(db),
        ),
    )


@router.get("/{expense_id}/audit", response_class=JSONResponse)
def studio_expense_audit(
    expense_id: int,
    current_user: AuthUser = _SUPER,
    db: Session = Depends(get_db),
):
    row = db.get(StudioExpense, expense_id)
    if not row:
        return JSONResponse({"rows": []}, status_code=404)
    rows = list(
        db.scalars(
            select(StudioExpenseAuditLog)
            .options(selectinload(StudioExpenseAuditLog.changed_by_user))
            .where(StudioExpenseAuditLog.expense_id == expense_id)
            .order_by(StudioExpenseAuditLog.changed_at.desc(), StudioExpenseAuditLog.id.desc())
            .limit(200)
        ).all()
    )
    tz = get_display_timezone(db)
    return JSONResponse(
        {
            "rows": [
                {
                    "changed_at": format_naive_utc_datetime(r.changed_at, tz) if r.changed_at else "",
                    "changed_by": (r.changed_by_user.display_name if r.changed_by_user else None),
                    "field_name": audit_field_label(r.field_name),
                    "old_value": r.old_value,
                    "new_value": r.new_value,
                }
                for r in rows
            ]
        }
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

    try:
        sid = parse_int(_g_str(form, "subcategory_id"), min=1, field_name="subcategory_id")
    except ValueError:
        return RedirectResponse(url="/admin/expenses?new=1&msg=subcat", status_code=303)
    sub = db.get(StudioExpenseSubcategory, sid)
    if not sub or not sub.is_active:
        return RedirectResponse(url="/admin/expenses?new=1&msg=subcat", status_code=303)

    amt = _parse_amount(_g_str(form, "amount"))
    if amt is None or amt < 0:
        return RedirectResponse(url="/admin/expenses?new=1&msg=amount", status_code=303)

    comment = _g_str(form, "comment")

    row = StudioExpense(
        created_by_user_id=current_user.id,
        created_at=utcnow_naive(),
        updated_at=None,
        updated_by_user_id=None,
        date=expense_dt,
        subcategory_id=sub.id,
        amount=float(amt),
        comment=comment or "",
    )
    db.add(row)
    db.flush()
    replace_studio_expense_ledger(db, row, current_user.id)
    db.commit()

    params: list[str] = ["msg=saved"]
    df = _g_str(form, "_filter_date_from")
    dt = _g_str(form, "_filter_date_to")
    fc = _g_str(form, "_filter_category_id")
    if df:
        params.append("date_from=" + df)
    if dt:
        params.append("date_to=" + dt)
    if fc:
        try:
            cid = parse_int(fc, min=1, field_name="category_id")
            params.append("category_id=" + str(cid))
        except ValueError:
            pass
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

    try:
        sid = parse_int(_g_str(form, "subcategory_id"), min=1, field_name="subcategory_id")
    except ValueError:
        return RedirectResponse(url=f"/admin/expenses?edit={expense_id}&msg=subcat", status_code=303)
    sub = db.get(StudioExpenseSubcategory, sid)
    if not sub or not sub.is_active:
        return RedirectResponse(url=f"/admin/expenses?edit={expense_id}&msg=subcat", status_code=303)

    amt = _parse_amount(_g_str(form, "amount"))
    if amt is None or amt < 0:
        return RedirectResponse(url=f"/admin/expenses?edit={expense_id}&msg=amount", status_code=303)

    comment = _g_str(form, "comment")

    before = SimpleNamespace(
        date=row.date,
        subcategory_id=row.subcategory_id,
        amount=row.amount,
        comment=row.comment,
    )
    prev_date = row.date
    prev_sub_id = row.subcategory_id
    prev_amount = float(row.amount or 0.0)
    row.date = expense_dt
    row.subcategory_id = sub.id
    row.amount = float(amt)
    row.comment = comment or ""
    row.updated_at = utcnow_naive()
    row.updated_by_user_id = current_user.id
    write_audit_rows(
        db,
        log_model=StudioExpenseAuditLog,
        entity_field="expense_id",
        entity_id=row.id,
        changed_by_user_id=current_user.id,
        changes=diff_fields(before, row, ("date", "subcategory_id", "amount", "comment")),
    )
    # Леджер меняем только если менялись финансовые поля (дата/подкатегория/сумма) или void.
    if prev_date != row.date or prev_sub_id != row.subcategory_id or abs(prev_amount - float(row.amount or 0.0)) > 0.0001:
        replace_studio_expense_ledger(db, row, current_user.id)
    db.commit()

    params: list[str] = []
    df = _g_str(form, "_filter_date_from")
    dt = _g_str(form, "_filter_date_to")
    fc = _g_str(form, "_filter_category_id")
    if df:
        params.append("date_from=" + df)
    if dt:
        params.append("date_to=" + dt)
    if fc:
        try:
            cid = parse_int(fc, min=1, field_name="category_id")
            params.append("category_id=" + str(cid))
        except ValueError:
            pass
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

    before = SimpleNamespace(
        is_voided=row.is_voided,
        voided_at=row.voided_at,
        voided_by_user_id=row.voided_by_user_id,
    )
    row.is_voided = True
    row.voided_at = utcnow_naive()
    row.voided_by_user_id = current_user.id
    row.updated_at = utcnow_naive()
    row.updated_by_user_id = current_user.id
    write_audit_rows(
        db,
        log_model=StudioExpenseAuditLog,
        entity_field="expense_id",
        entity_id=row.id,
        changed_by_user_id=current_user.id,
        changes=diff_fields(before, row, ("is_voided", "voided_at", "voided_by_user_id")),
    )
    replace_studio_expense_ledger(db, row, current_user.id)
    db.commit()
    return RedirectResponse(url="/admin/expenses?msg=voided", status_code=303)
