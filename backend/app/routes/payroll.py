from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import AuthUser, require_role
from app.db.models import PayrollFundEntryKind, PayrollPeriod, User, UserRole
from app.db.session import get_db
from app.display_time import get_display_timezone
from app.forms_parse import parse_date_iso, parse_float, parse_int
from app.db.models import PayrollFundSourceKind
from app.payroll_fund import (
    LEDGER_JOURNAL_SEARCH_LIMIT,
    PAYROLL_FUND_SOURCE_KIND_RU,
    PayrollFundPayoutPaymentKind,
    PayrollFundSide,
    current_fund_balance,
    existing_storno_id_for_ledger,
    get_manual_ledger_for_storno,
    ledger_balances,
    post_manual_adjustment,
    post_payout,
    search_ledger_rows,
    storno_manual_ledger_entry,
    visit_ids_for_visit_service_source_ids,
)
from app.payroll_utils import payroll_period_day_end, payroll_period_day_start
from app.ru_labels import ru_user_roles_payout_suffix
from app.time_utils import utcnow_naive
from app.user_roles import get_roles_for_user, select_users_with_any_role, user_has_any_role
from app.operational_report import list_closed_payroll_periods
from app.visit_edit_policy import (
    ensure_event_date_in_open_payroll_period,
    is_in_closed_payroll_period,
    require_closed_period_ack,
    user_may_edit_closed_payroll_period,
)
from app.webui import templates, ctx as _ctx


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
        "storno": "Проводка отменена (сторно записано в журнал).",
    }.get(code or "", code)


def _payroll_fund_err_ru(code: str | None) -> str | None:
    return {
        "bad_side": "Укажите корректный фонд-источник.",
        "bad_amount": "Укажите ненулевую сумму (для возврата в фонд можно ввести отрицательное число).",
        "bad_user": "Выберите сотрудника.",
        "bad_payment": "Укажите тип оплаты.",
        "bad_mode": "Укажите корректный режим корректировки.",
        "bad_date": "Укажите корректную дату выплаты.",
        "bad_period": "Дата выплаты недопустима (закрытый период, будущее или раньше открытого периода).",
        "bad_period_ack": "Для выплаты в закрытом периоде ЗП нужно двойное подтверждение (только техспец).",
        "bad_storno_id": "Укажите корректный ID проводки.",
        "storno_fail": "Не удалось отменить проводку.",
    }.get(code or "", code)


@router.get("/admin/payroll-fund", response_class=HTMLResponse)
def admin_payroll_fund_page(
    request: Request,
    msg: str | None = None,
    err: str | None = None,
    df: str | None = None,
    dt: str | None = None,
    whom: str | None = None,
    source: str | None = None,
    type: str | None = None,
    fund: str | None = None,
    by: str | None = None,
    storno_id: str | None = None,
    current_user: AuthUser = Depends(require_role(UserRole.ADMIN_SUPER)),
    db: Session = Depends(get_db),
):
    masters_bal, studio_bal = ledger_balances(db)
    # Показываем всех сотрудников (даже с 0), а нулевые выносим в конец списка.
    bal_by_uid = {int(m["user_id"]): float(m["balance"]) for m in masters_bal}

    journal_err: str | None = None
    effective_from: date | None = None
    effective_to: date | None = None
    form_df = (df or "").strip()
    form_dt = (dt or "").strip()
    form_whom = (whom or "").strip()
    form_source = (source or "").strip().upper()
    form_type = (type or "").strip().upper()
    form_fund = (fund or "").strip().upper()
    form_by = (by or "").strip()
    if form_df:
        try:
            effective_from = parse_date_iso(form_df, field_name="df")
        except ValueError:
            journal_err = "Некорректная дата «с» в фильтре журнала."
            form_df = ""
    if form_dt:
        try:
            effective_to = parse_date_iso(form_dt, field_name="dt")
        except ValueError:
            journal_err = "Некорректная дата «по» в фильтре журнала."
            form_dt = ""
    if effective_from is not None and effective_to is not None and effective_to < effective_from:
        journal_err = "В фильтре журнала дата «по» раньше даты «с»."
        effective_from = None
        effective_to = None
        form_df = ""
        form_dt = ""

    filter_user_id: int | None = None
    studio_only = False
    if form_whom == "studio":
        studio_only = True
    elif form_whom:
        try:
            filter_user_id = parse_int(form_whom, min=1, field_name="whom")
        except ValueError:
            journal_err = "Некорректный сотрудник в фильтре журнала."
            form_whom = ""

    filter_source: PayrollFundSourceKind | None = None
    if form_source:
        try:
            filter_source = PayrollFundSourceKind(form_source)
        except ValueError:
            journal_err = "Некорректный источник в фильтре журнала."
            form_source = ""

    filter_entry_kind: PayrollFundEntryKind | None = None
    if form_type:
        try:
            filter_entry_kind = PayrollFundEntryKind(form_type)
        except ValueError:
            journal_err = "Некорректный тип в фильтре журнала."
            form_type = ""

    filter_side: PayrollFundSide | None = None
    if form_fund:
        try:
            filter_side = PayrollFundSide(form_fund)
        except ValueError:
            journal_err = "Некорректный фонд в фильтре журнала."
            form_fund = ""

    filter_created_by_user_id: int | None = None
    if form_by:
        try:
            filter_created_by_user_id = parse_int(form_by, min=1, field_name="by")
        except ValueError:
            journal_err = "Некорректный пользователь в фильтре «Кто внёс»."
            form_by = ""

    journal_filtered = bool(
        effective_from is not None
        or effective_to is not None
        or filter_user_id is not None
        or studio_only
        or filter_source is not None
        or filter_entry_kind is not None
        or filter_side is not None
        or filter_created_by_user_id is not None
    )
    ledger_rows = search_ledger_rows(
        db,
        effective_from=effective_from,
        effective_to=effective_to,
        user_id=filter_user_id,
        studio_only=studio_only,
        source_kind=filter_source,
        entry_kind=filter_entry_kind,
        side=filter_side,
        created_by_user_id=filter_created_by_user_id,
    )
    vs_source_ids = [
        int(r.source_id)
        for r in ledger_rows
        if r.source_kind == PayrollFundSourceKind.VISIT_SERVICE and r.source_id is not None
    ]
    visit_id_by_service_id = visit_ids_for_visit_service_source_ids(db, vs_source_ids)
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
    tz = ZoneInfo(get_display_timezone(db))
    today_local = utcnow_naive().replace(tzinfo=ZoneInfo("UTC")).astimezone(tz).date()
    source_kind_options = [
        {"value": k.value, "label": PAYROLL_FUND_SOURCE_KIND_RU[k]}
        for k in PayrollFundSourceKind
        if k != PayrollFundSourceKind.VISIT_SERVICE
    ]
    entry_kind_options = [
        {"value": PayrollFundEntryKind.ACCRUAL.value, "label": "Начисление"},
        {"value": PayrollFundEntryKind.STORNO.value, "label": "Сторно"},
        {"value": PayrollFundEntryKind.PAYOUT.value, "label": "Выплата"},
        {"value": PayrollFundEntryKind.EXPENSE.value, "label": "Расход студии"},
    ]
    fund_side_options = [
        {"value": PayrollFundSide.MASTER.value, "label": "Фонд сотрудника"},
        {"value": PayrollFundSide.STUDIO.value, "label": "Фонд студии"},
    ]
    created_by_user_options = payout_user_options
    payout_techspec_closed_override = user_may_edit_closed_payroll_period(current_user)
    closed_periods_json = json.dumps(
        [
            {
                "from": p.date_from.date().isoformat() if hasattr(p.date_from, "date") else str(p.date_from)[:10],
                "to": p.date_to.date().isoformat() if hasattr(p.date_to, "date") else str(p.date_to)[:10],
            }
            for p in list_closed_payroll_periods(db)
        ],
        ensure_ascii=False,
    )
    page_err = _payroll_fund_err_ru(err) or journal_err

    storno_preview = None
    storno_preview_err: str | None = None
    storno_id_form = (storno_id or "").strip()
    storno_already_id: int | None = None
    if storno_id_form:
        try:
            sid = parse_int(storno_id_form, min=1, field_name="storno_id")
        except ValueError:
            storno_preview_err = "Укажите корректный ID проводки."
            storno_id_form = ""
        else:
            try:
                storno_preview = get_manual_ledger_for_storno(db, sid)
                storno_already_id = existing_storno_id_for_ledger(db, sid)
                if storno_already_id is not None:
                    storno_preview_err = f"Проводка уже отменена (сторно #{storno_already_id})."
            except ValueError as e:
                storno_preview_err = str(e)

    return templates.TemplateResponse(
        "admin_payroll_fund.html",
        _ctx(
            request,
            current_user=current_user,
            master_rows_nonzero=master_rows_nonzero,
            master_rows_zero=master_rows_zero,
            studio_balance=studio_bal,
            ledger_rows=ledger_rows,
            visit_id_by_service_id=visit_id_by_service_id,
            payout_user_options=payout_user_options,
            payout_fund_balances_json=payout_fund_balances_json,
            payout_default_date=today_local.isoformat(),
            payout_techspec_closed_override=payout_techspec_closed_override,
            closed_periods_json=closed_periods_json,
            journal_filtered=journal_filtered,
            journal_limit=LEDGER_JOURNAL_SEARCH_LIMIT if journal_filtered else 150,
            form_df=form_df,
            form_dt=form_dt,
            form_whom=form_whom,
            form_source=form_source,
            form_type=form_type,
            form_fund=form_fund,
            form_by=form_by,
            source_kind_options=source_kind_options,
            entry_kind_options=entry_kind_options,
            fund_side_options=fund_side_options,
            created_by_user_options=created_by_user_options,
            storno_id_form=storno_id_form,
            storno_preview=storno_preview,
            storno_preview_err=storno_preview_err,
            storno_already_id=storno_already_id,
            msg=_payroll_fund_msg_ru(msg),
            err=page_err,
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
    date_raw = (str(form.get("payout_date") or "")).strip()
    try:
        payout_day = parse_date_iso(date_raw, field_name="payout_date")
    except ValueError:
        return RedirectResponse(url="/admin/payroll-fund?err=bad_date", status_code=303)
    effective_at = datetime.combine(payout_day, datetime.min.time())
    closed = is_in_closed_payroll_period(db, effective_at)
    allow_closed = bool(closed and user_may_edit_closed_payroll_period(current_user))
    if closed and not allow_closed:
        return RedirectResponse(url="/admin/payroll-fund?err=bad_period", status_code=303)
    try:
        require_closed_period_ack(needed=allow_closed, form_ack=form.get("closed_period_ack"))
    except ValueError:
        return RedirectResponse(url="/admin/payroll-fund?err=bad_period_ack", status_code=303)
    try:
        ensure_event_date_in_open_payroll_period(db, effective_at, allow_closed=allow_closed)
    except ValueError:
        return RedirectResponse(url="/admin/payroll-fund?err=bad_period", status_code=303)
    try:
        post_payout(
            db,
            side=side,
            user_id=user_id,
            amount=amount,
            created_by_user_id=current_user.id,
            comment=comment,
            payout_payment_kind=payment_kind,
            effective_at=effective_at,
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


@router.post("/admin/payroll-fund/storno")
async def admin_payroll_fund_storno(
    request: Request,
    current_user: AuthUser = Depends(require_role(UserRole.ADMIN_SUPER)),
    db: Session = Depends(get_db),
):
    form = await request.form()
    try:
        entry_id = parse_int(form.get("ledger_id"), min=1, field_name="ledger_id")
    except ValueError:
        return RedirectResponse(url="/admin/payroll-fund?err=bad_storno_id", status_code=303)
    try:
        storno_manual_ledger_entry(db, entry_id, current_user.id)
    except ValueError:
        return RedirectResponse(url=f"/admin/payroll-fund?storno_id={entry_id}", status_code=303)
    db.commit()
    return RedirectResponse(url="/admin/payroll-fund?msg=storno", status_code=303)

