from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import AuthUser, require_role
from app.db.models import PayrollPeriod, User, UserRole
from app.db.session import get_db
from app.forms_parse import parse_date_iso, parse_float, parse_int
from app.payroll_fund import (
    PayrollFundPayoutPaymentKind,
    PayrollFundSide,
    current_fund_balance,
    ledger_balances,
    post_manual_adjustment,
    post_payout,
    recent_ledger_rows,
)
from app.payroll_utils import payroll_period_day_end, payroll_period_day_start
from app.time_utils import utcnow_naive
from app.user_roles import get_roles_for_user, select_users_with_any_role, user_has_any_role
from app.webui import templates, ctx as _ctx
from app.ru_labels import ru_user_roles_payout_suffix


router = APIRouter()


def _payroll_msg_ru(code: str | None) -> str | None:
    if not code:
        return None
    return {"created": "Открыт новый период.", "closed": "Период закрыт.", "already_closed": "Период уже был закрыт."}.get(code, code)


def _payroll_err_ru(code: str | None) -> str | None:
    if not code:
        return None
    return {
        "bad_date": "Некорректная дата.",
        "range": "Дата «По» не может быть раньше даты «С».",
        "not_found": "Период не найден.",
        "open_exists": "Сначала закройте текущий открытый период.",
        "empty_po": "Укажите дату «По», чтобы закрыть период.",
    }.get(code, code)


@router.get("/admin/payroll-periods", response_class=HTMLResponse)
def admin_payroll_periods_list(
    request: Request,
    msg: str | None = None,
    err: str | None = None,
    current_user: AuthUser = Depends(require_role(UserRole.ADMIN_SUPER)),
    db: Session = Depends(get_db),
):
    periods = list(db.scalars(select(PayrollPeriod).order_by(PayrollPeriod.date_from.desc(), PayrollPeriod.id.desc())).all())
    has_open = any(p.closed_at is None for p in periods)
    can_open_next = not has_open
    return templates.TemplateResponse(
        "admin_payroll_periods.html",
        _ctx(request, current_user=current_user, periods=periods, msg=_payroll_msg_ru(msg), err=_payroll_err_ru(err), can_open_next=can_open_next),
    )


@router.post("/admin/payroll-periods/open-next")
def admin_payroll_periods_open_next(
    current_user: AuthUser = Depends(require_role(UserRole.ADMIN_SUPER)),
    db: Session = Depends(get_db),
):
    if db.scalar(select(PayrollPeriod.id).where(PayrollPeriod.closed_at.is_(None)).limit(1)) is not None:
        return RedirectResponse(url="/admin/payroll-periods?err=open_exists", status_code=303)

    last = db.scalar(select(PayrollPeriod).where(PayrollPeriod.closed_at.is_not(None)).order_by(PayrollPeriod.date_to.desc(), PayrollPeriod.id.desc()).limit(1))
    if last is None:
        df_d = utcnow_naive().date()
    else:
        df_d = last.date_to.date() + timedelta(days=1)

    day_start = payroll_period_day_start(df_d)
    db.add(PayrollPeriod(date_from=day_start, date_to=day_start, closed_at=None))
    db.commit()
    return RedirectResponse(url="/admin/payroll-periods?msg=created", status_code=303)


@router.post("/admin/payroll-periods/{period_id}/close")
async def admin_payroll_periods_close(
    period_id: int,
    request: Request,
    current_user: AuthUser = Depends(require_role(UserRole.ADMIN_SUPER)),
    db: Session = Depends(get_db),
):
    p = db.get(PayrollPeriod, period_id)
    if not p:
        return RedirectResponse(url="/admin/payroll-periods?err=not_found", status_code=303)
    if p.closed_at:
        return RedirectResponse(url="/admin/payroll-periods?msg=already_closed", status_code=303)

    form = await request.form()
    raw = str(form.get("date_to") or "").strip()
    if not raw:
        return RedirectResponse(url="/admin/payroll-periods?err=empty_po", status_code=303)
    try:
        d_to = parse_date_iso(raw, field_name="date_to")
    except ValueError:
        return RedirectResponse(url="/admin/payroll-periods?err=bad_date", status_code=303)

    if d_to < p.date_from.date():
        return RedirectResponse(url="/admin/payroll-periods?err=range", status_code=303)

    p.date_to = payroll_period_day_end(d_to)
    p.closed_at = utcnow_naive()
    p.closed_by_name = current_user.display_name
    p.closed_by_role = current_user.role.value
    db.commit()
    return RedirectResponse(url="/admin/payroll-periods?msg=closed", status_code=303)


def _payroll_fund_msg_ru(code: str | None) -> str | None:
    return {
        "paid": "Выплата записана в журнал.",
        "adjusted": "Корректировка записана в журнал.",
    }.get(code or "", code)


def _payroll_fund_err_ru(code: str | None) -> str | None:
    return {
        "bad_side": "Укажите корректный фонд-источник.",
        "bad_amount": "Укажите ненулевую сумму (для возврата в фонд можно ввести отрицательное число).",
        "bad_user": "Выберите сотрудника.",
        "bad_payment": "Укажите тип оплаты.",
        "bad_mode": "Укажите корректный режим корректировки.",
    }.get(code or "", code)


@router.get("/admin/payroll-fund", response_class=HTMLResponse)
def admin_payroll_fund_page(
    request: Request,
    msg: str | None = None,
    err: str | None = None,
    current_user: AuthUser = Depends(require_role(UserRole.ADMIN_SUPER)),
    db: Session = Depends(get_db),
):
    masters_bal, studio_bal = ledger_balances(db)
    # Показываем всех сотрудников (даже с 0), а нулевые выносим в конец списка.
    bal_by_uid = {int(m["user_id"]): float(m["balance"]) for m in masters_bal}
    ledger_rows = recent_ledger_rows(db)
    payout_users = list(
        db.scalars(
            select_users_with_any_role(UserRole.MASTER, UserRole.ADMIN, UserRole.ADMIN_SUPER).order_by(User.display_name.asc())
        ).all()
    )
    all_user_rows: list[dict[str, Any]] = []
    for u in payout_users:
        all_user_rows.append(
            {
                "user_id": int(u.id),
                "display_name": u.display_name,
                "role": u.role,
                "balance": float(bal_by_uid.get(int(u.id), 0.0)),
            }
        )
    master_rows_nonzero = [r for r in all_user_rows if abs(float(r["balance"])) > 0.0001]
    master_rows_zero = [r for r in all_user_rows if abs(float(r["balance"])) <= 0.0001]
    payout_user_options: list[dict[str, Any]] = []
    for u in payout_users:
        payout_user_options.append({"user": u, "roles_ru": ru_user_roles_payout_suffix(get_roles_for_user(db, u.id))})
    payout_employee_balances = {str(o["user"].id): round(float(bal_by_uid.get(o["user"].id, 0.0)), 2) for o in payout_user_options}
    payout_fund_balances_json = json.dumps({"studio": round(float(studio_bal), 2), "employees": payout_employee_balances}, ensure_ascii=False)
    return templates.TemplateResponse(
        "admin_payroll_fund.html",
        _ctx(
            request,
            current_user=current_user,
            master_rows_nonzero=master_rows_nonzero,
            master_rows_zero=master_rows_zero,
            studio_balance=studio_bal,
            ledger_rows=ledger_rows,
            payout_user_options=payout_user_options,
            payout_fund_balances_json=payout_fund_balances_json,
            msg=_payroll_fund_msg_ru(msg),
            err=_payroll_fund_err_ru(err),
        ),
    )


@router.post("/admin/payroll-fund/payout")
async def admin_payroll_fund_payout(
    request: Request,
    current_user: AuthUser = Depends(require_role(UserRole.ADMIN_SUPER)),
    db: Session = Depends(get_db),
):
    form = await request.form()
    side_raw = (str(form.get("side") or "")).strip().upper()
    try:
        side = PayrollFundSide(side_raw)
    except ValueError:
        return RedirectResponse(url="/admin/payroll-fund?err=bad_side", status_code=303)
    try:
        amount = parse_float(form.get("amount"), field_name="amount")
    except ValueError:
        return RedirectResponse(url="/admin/payroll-fund?err=bad_amount", status_code=303)
    comment = str(form.get("comment") or "").strip()
    try:
        user_id = parse_int(form.get("user_id"), min=1, field_name="user_id")
    except ValueError:
        return RedirectResponse(url="/admin/payroll-fund?err=bad_user", status_code=303)
    if db.get(User, user_id) is None:
        return RedirectResponse(url="/admin/payroll-fund?err=bad_user", status_code=303)
    if not user_has_any_role(db, user_id, UserRole.MASTER, UserRole.ADMIN, UserRole.ADMIN_SUPER):
        return RedirectResponse(url="/admin/payroll-fund?err=bad_user", status_code=303)
    pay_raw = (str(form.get("payment_kind") or "")).strip().upper()
    try:
        payment_kind = PayrollFundPayoutPaymentKind(pay_raw)
    except ValueError:
        return RedirectResponse(url="/admin/payroll-fund?err=bad_payment", status_code=303)
    try:
        post_payout(
            db,
            side=side,
            user_id=user_id,
            amount=amount,
            created_by_user_id=current_user.id,
            comment=comment,
            payout_payment_kind=payment_kind,
        )
    except ValueError:
        return RedirectResponse(url="/admin/payroll-fund?err=bad_amount", status_code=303)
    db.commit()
    return RedirectResponse(url="/admin/payroll-fund?msg=paid", status_code=303)


@router.post("/admin/payroll-fund/adjust")
async def admin_payroll_fund_adjust(
    request: Request,
    current_user: AuthUser = Depends(require_role(UserRole.ADMIN_SUPER)),
    db: Session = Depends(get_db),
):
    form = await request.form()
    side_raw = (str(form.get("side") or "")).strip().upper()
    try:
        side = PayrollFundSide(side_raw)
    except ValueError:
        return RedirectResponse(url="/admin/payroll-fund?err=bad_side", status_code=303)

    # For MASTER adjustments we need a user.
    user_id: int | None = None
    if side == PayrollFundSide.MASTER:
        try:
            user_id = parse_int(form.get("user_id"), min=1, field_name="user_id")
        except ValueError:
            return RedirectResponse(url="/admin/payroll-fund?err=bad_user", status_code=303)
        if db.get(User, user_id) is None:
            return RedirectResponse(url="/admin/payroll-fund?err=bad_user", status_code=303)

    mode = (str(form.get("mode") or "")).strip().lower()
    if mode not in ("delta", "set"):
        return RedirectResponse(url="/admin/payroll-fund?err=bad_mode", status_code=303)

    try:
        amount = parse_float(form.get("amount"), field_name="amount")
    except ValueError:
        return RedirectResponse(url="/admin/payroll-fund?err=bad_amount", status_code=303)

    comment = str(form.get("comment") or "").strip()

    try:
        if mode == "set":
            cur = current_fund_balance(db, side=side, user_id=user_id)
            delta = float(amount) - float(cur)
        else:
            delta = float(amount)
        post_manual_adjustment(
            db,
            side=side,
            user_id=user_id,
            amount_delta=delta,
            created_by_user_id=current_user.id,
            comment=comment or ("Начальный остаток" if mode == "set" else None),
        )
    except ValueError:
        return RedirectResponse(url="/admin/payroll-fund?err=bad_amount", status_code=303)

    db.commit()
    return RedirectResponse(url="/admin/payroll-fund?msg=adjusted", status_code=303)

