"""Страница «Моя карточка» (/me) — профиль текущего сотрудника."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.audit import FieldChange, write_audit_rows
from app.auth import AuthUser, get_current_user
from app.db.models import (
    Booking,
    BookingKind,
    BookingMaster,
    BookingStaff,
    BookingStatus,
    User,
    UserAuditLog,
    UserRole,
)
from app.db.session import get_db
from app.display_time import format_naive_utc_datetime, get_display_timezone
from app.forms_parse import parse_bool
from app.master_schedule import build_master_schedule_banner
from app.notifications import send_telegram
from app.payroll_fund import (
    build_home_payroll_period_ctx,
    employee_fund_balance,
    employee_payout_total_net,
    studio_fund_balance,
)
from app.role_access import role_is_master_schedule_admin
from app.ru_labels import ru_user_role
from app.security import hash_password, verify_password
from app.settings import get_settings
from app.telegram_link import create_telegram_link_token, telegram_deep_link, unlink_telegram_chat
from app.time_utils import utcnow_naive
from app.user_roles import get_roles_for_user
from app.webui import ctx as _ctx, templates

router = APIRouter(tags=["me"])


def _money0(x: float | None) -> float:
    return float(x or 0.0)


def _load_self_user(db: Session, user_id: int) -> User:
    u = db.get(User, int(user_id))
    if u is None:
        raise RuntimeError("Пользователь сессии не найден")
    return u


def _build_payroll_home(db: Session, current_user: AuthUser, *, today: date, display_tz: str) -> dict[str, Any] | None:
    """Та же сводка, что на главной (без новых расчётов)."""
    is_helper = current_user.role == UserRole.HELPER
    show_studio = (not is_helper) and (UserRole.ADMIN_SUPER in current_user.roles)
    period_ctx = build_home_payroll_period_ctx(
        db,
        today=today,
        user_id=current_user.id,
        include_studio=show_studio,
    )
    return {
        "personal_balance": _money0(employee_fund_balance(db, current_user.id)),
        "paid_net": _money0(employee_payout_total_net(db, current_user.id)),
        "show_studio": show_studio,
        "studio_balance": _money0(studio_fund_balance(db)) if show_studio else None,
        "display_tz": display_tz,
        "period": period_ctx,
    }


def list_upcoming_bookings_for_user(
    db: Session,
    user_id: int,
    *,
    now_utc: datetime,
    until_utc: datetime,
) -> list[dict[str, Any]]:
    """Ближайшие брони пользователя (тот же критерий назначения, что /master/mywork)."""
    visit_ids = list(
        db.scalars(
            select(Booking.id)
            .join(BookingMaster, BookingMaster.booking_id == Booking.id)
            .where(
                Booking.status.in_((BookingStatus.PENDING_CONFIRMATION, BookingStatus.ACTIVE)),
                BookingMaster.master_id == int(user_id),
                Booking.planned_date >= now_utc,
                Booking.planned_date < until_utc,
            )
        ).all()
    )
    sale_ids = list(
        db.scalars(
            select(Booking.id)
            .join(BookingStaff, BookingStaff.booking_id == Booking.id)
            .where(
                Booking.status.in_((BookingStatus.PENDING_CONFIRMATION, BookingStatus.ACTIVE)),
                BookingStaff.user_id == int(user_id),
                Booking.planned_date >= now_utc,
                Booking.planned_date < until_utc,
            )
        ).all()
    )
    ids = sorted({int(x) for x in (visit_ids + sale_ids) if x is not None})
    if not ids:
        return []
    bookings = list(
        db.scalars(
            select(Booking)
            .where(Booking.id.in_(ids))
            .options(selectinload(Booking.client), selectinload(Booking.planned_service))
            .order_by(Booking.planned_date.asc(), Booking.id.asc())
        ).all()
    )
    out: list[dict[str, Any]] = []
    for b in bookings:
        if b.kind == BookingKind.VISIT:
            label = (b.planned_service.name if b.planned_service else None) or "Визит"
        elif b.kind == BookingKind.CONSULTATION:
            label = "Консультация"
        else:
            label = "Продажа" if b.kind == BookingKind.PRODUCT_SALE else (b.kind.value if b.kind else "Бронь")
        client_name = (b.client.name if b.client else None) or "—"
        out.append(
            {
                "id": int(b.id),
                "planned_date": b.planned_date,
                "label": label,
                "client": client_name,
                "kind": b.kind.value if b.kind else "",
                "status": b.status.value if b.status else "",
            }
        )
    return out


def _me_page_response(
    request: Request,
    *,
    current_user: AuthUser,
    db: Session,
    error: str | None = None,
    info: str | None = None,
    telegram_deep_link_url: str | None = None,
):
    u = _load_self_user(db, current_user.id)
    display_tz = get_display_timezone(db)
    tz = ZoneInfo(display_tz)
    now_local = utcnow_naive().replace(tzinfo=ZoneInfo("UTC")).astimezone(tz)
    now_utc = utcnow_naive()
    until_utc = now_utc + timedelta(days=7)

    roles = get_roles_for_user(db, current_user.id) or list(current_user.roles)
    role_labels = [ru_user_role(r) for r in roles]

    payroll_home = _build_payroll_home(db, current_user, today=now_local.date(), display_tz=display_tz)

    # На /me: мастер — всегда; иначе — только при наличии данных графика.
    schedule_banner = build_master_schedule_banner(
        db,
        user_id=current_user.id,
        is_master_active=current_user.role == UserRole.MASTER,
        is_schedule_admin=role_is_master_schedule_admin(current_user.role),
    )

    upcoming_raw = list_upcoming_bookings_for_user(db, current_user.id, now_utc=now_utc, until_utc=until_utc)
    upcoming = [
        {
            **row,
            "when": format_naive_utc_datetime(row["planned_date"], display_tz) or "—",
        }
        for row in upcoming_raw
    ]

    settings = get_settings()
    return templates.TemplateResponse(
        "me.html",
        _ctx(
            request,
            current_user=current_user,
            help_page_id="me",
            user=u,
            role_labels=role_labels,
            payroll_home=payroll_home,
            master_schedule_banner=schedule_banner,
            upcoming=upcoming,
            telegram_bot_username=settings.telegram_bot_username,
            telegram_deep_link=telegram_deep_link_url,
            error=error,
            info=info,
        ),
    )


@router.get("/me", response_class=HTMLResponse)
def me_page(
    request: Request,
    msg: str | None = None,
    err: str | None = None,
    tg_start: str | None = None,
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    error = None
    info = None
    deep = None
    if err == "tg_bot_username":
        error = "Задайте TELEGRAM_BOT_USERNAME в настройках окружения, затем создайте ссылку снова."
    elif err == "tg_not_linked":
        error = "Сначала подключите Telegram."
    elif err == "tg_test_fail":
        error = "Не удалось отправить тестовое сообщение. Проверьте токен бота и привязку."
    elif err == "pwd_current":
        error = "Неверный текущий пароль."
    elif err == "pwd_mismatch":
        error = "Новый пароль и подтверждение не совпадают."
    elif err == "pwd_short":
        error = "Новый пароль должен быть не короче 6 символов."
    elif err == "notify_fail":
        error = "Не удалось сохранить настройку уведомлений."
    elif msg == "tg_connect" and (tg_start or "").strip():
        deep = telegram_deep_link(tg_start.strip())
        info = "Ссылка для привязки Telegram создана (действует 24 часа). Откройте её в Telegram."
        if deep is None:
            error = "Код создан, но TELEGRAM_BOT_USERNAME не задан — ссылку t.me собрать нельзя."
    elif msg == "tg_disconnected":
        info = "Telegram отключён."
    elif msg == "tg_test_ok":
        info = "Тестовое сообщение отправлено."
    elif msg == "notify_saved":
        info = "Настройка уведомлений сохранена."
    elif msg == "pwd_ok":
        info = "Пароль изменён."
    return _me_page_response(
        request,
        current_user=current_user,
        db=db,
        error=error,
        info=info,
        telegram_deep_link_url=deep,
    )


@router.post("/me/telegram/connect")
def me_telegram_connect(
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    u = _load_self_user(db, current_user.id)
    if not get_settings().telegram_bot_username:
        return RedirectResponse(url="/me?err=tg_bot_username", status_code=303)
    plain, _deep = create_telegram_link_token(db, u.id)
    write_audit_rows(
        db,
        log_model=UserAuditLog,
        entity_field="user_id",
        entity_id=u.id,
        changed_by_user_id=current_user.id,
        changes=[FieldChange("telegram_link", None, "код создан (моя карточка)")],
    )
    db.commit()
    return RedirectResponse(
        url=f"/me?msg=tg_connect&tg_start={quote(plain, safe='')}",
        status_code=303,
    )


@router.post("/me/telegram/disconnect")
def me_telegram_disconnect(
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    u = _load_self_user(db, current_user.id)
    old = str(u.telegram_chat_id) if u.telegram_chat_id is not None else None
    unlink_telegram_chat(db, u)
    write_audit_rows(
        db,
        log_model=UserAuditLog,
        entity_field="user_id",
        entity_id=u.id,
        changed_by_user_id=current_user.id,
        changes=[FieldChange("telegram_chat_id", old, None)],
    )
    db.commit()
    return RedirectResponse(url="/me?msg=tg_disconnected", status_code=303)


@router.post("/me/telegram/notify")
async def me_telegram_notify(
    request: Request,
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    u = _load_self_user(db, current_user.id)
    form = await request.form()
    # Игнорируем любой подложенный user_id — только сессия.
    new_notify = parse_bool(form.get("notify_enabled"))
    old = bool(u.notify_enabled)
    u.notify_enabled = new_notify
    if old != new_notify:
        write_audit_rows(
            db,
            log_model=UserAuditLog,
            entity_field="user_id",
            entity_id=u.id,
            changed_by_user_id=current_user.id,
            changes=[FieldChange("notify_enabled", str(old), str(new_notify))],
        )
    db.commit()
    return RedirectResponse(url="/me?msg=notify_saved", status_code=303)


@router.post("/me/telegram/test")
def me_telegram_test(
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    u = _load_self_user(db, current_user.id)
    if u.telegram_chat_id is None:
        return RedirectResponse(url="/me?err=tg_not_linked", status_code=303)
    try:
        send_telegram(
            int(u.telegram_chat_id),
            f"Тест LivingBraiding: уведомления для {u.display_name} работают.",
        )
    except Exception:
        return RedirectResponse(url="/me?err=tg_test_fail", status_code=303)
    return RedirectResponse(url="/me?msg=tg_test_ok", status_code=303)


@router.post("/me/password")
async def me_password_change(
    request: Request,
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    u = _load_self_user(db, current_user.id)
    form = await request.form()
    current_pwd = str(form.get("current_password") or "")
    new_pwd = str(form.get("new_password") or "")
    confirm = str(form.get("new_password_confirm") or "")
    if not verify_password(current_pwd, u.password_hash):
        return RedirectResponse(url="/me?err=pwd_current", status_code=303)
    if new_pwd != confirm:
        return RedirectResponse(url="/me?err=pwd_mismatch", status_code=303)
    if len(new_pwd) < 6:
        return RedirectResponse(url="/me?err=pwd_short", status_code=303)
    u.password_hash = hash_password(new_pwd)
    write_audit_rows(
        db,
        log_model=UserAuditLog,
        entity_field="user_id",
        entity_id=u.id,
        changed_by_user_id=current_user.id,
        changes=[FieldChange("password", None, "изменён (моя карточка)")],
    )
    db.commit()
    return RedirectResponse(url="/me?msg=pwd_ok", status_code=303)
