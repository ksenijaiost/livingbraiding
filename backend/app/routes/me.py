"""Страница «Моя карточка» (/me) — профиль текущего сотрудника."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.audit import FieldChange, write_audit_rows
from app.auth import AuthUser, get_current_user
from app.booking_reminders import (
    MAX_REMINDERS_PER_USER,
    add_user_reminder_hours,
    clear_user_reminders,
    hours_to_minutes,
    list_reminder_settings_for_ui,
    parse_reminder_hours,
    remove_user_reminder_minutes,
    replace_user_reminder_minutes,
)
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
from app.master_schedule import build_master_schedule_banner
from app.notifications import send_telegram, send_vk
from app.notify_link_flash import pop_notify_link_flash, set_notify_link_flash
from app.notify_prefs import apply_notify_prefs_from_form, notify_prefs_for_ui
from app.payroll_fund import (
    build_home_payroll_period_ctx,
    employee_fund_balance,
    employee_payout_total_net,
    studio_fund_balance,
)
from app.ru_labels import ru_user_role
from app.security import hash_password, verify_password
from app.settings import get_settings
from app.telegram_link import (
    CHANNEL_TELEGRAM,
    CHANNEL_VK,
    create_telegram_link_token,
    create_vk_link_token,
    telegram_deep_link,
    unlink_telegram_chat,
    unlink_vk_user,
    vk_deep_link,
)
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
    is_self_scoped = current_user.role in (UserRole.MASTER, UserRole.HELPER)
    # Как на главной: фонд студии не показываем в кабинете мастера (даже с ADMIN_SUPER).
    show_studio = (
        (not is_helper)
        and (not is_self_scoped)
        and (
            UserRole.ADMIN_SUPER in current_user.roles
            or UserRole.TECHSPEC in current_user.roles
        )
    )
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
    response: RedirectResponse | None = None,
    *,
    current_user: AuthUser,
    db: Session,
    error: str | None = None,
    info: str | None = None,
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

    is_master = current_user.role == UserRole.MASTER
    schedule_banner = None
    upcoming: list[dict[str, Any]] = []
    if is_master:
        schedule_banner = build_master_schedule_banner(
            db,
            user_id=current_user.id,
            is_master_active=True,
            is_schedule_admin=False,
        )
        upcoming_raw = list_upcoming_bookings_for_user(
            db, current_user.id, now_utc=now_utc, until_utc=until_utc
        )
        upcoming = [
            {
                **row,
                "when": format_naive_utc_datetime(row["planned_date"], display_tz) or "—",
            }
            for row in upcoming_raw
        ]

    settings = get_settings()
    notify_type_cols = notify_prefs_for_ui(u, roles)
    notify_channels = [
        {
            "id": "vk",
            "label": "VK",
            "connected": u.vk_user_id is not None,
            "status_label": "подключён" if u.vk_user_id is not None else "не подключён",
            "connect_url": "/me/vk/connect",
            "disconnect_url": "/me/vk/disconnect",
            "test_url": "/me/vk/test",
        },
        {
            "id": "telegram",
            "label": "Telegram",
            "connected": u.telegram_chat_id is not None,
            "status_label": "подключён" if u.telegram_chat_id is not None else "не подключён",
            "connect_url": "/me/telegram/connect",
            "disconnect_url": "/me/telegram/disconnect",
            "test_url": "/me/telegram/test",
        },
    ]
    first_connected_notify_idx = next(
        (i for i, ch in enumerate(notify_channels) if ch["connected"]),
        None,
    )

    # Сначала читаем flash из cookie (сброс cookie повесим на ответ шаблона).
    from starlette.responses import Response as StarletteResponse

    cookie_sink = StarletteResponse()
    flash = pop_notify_link_flash(request, cookie_sink)
    link_flash = None
    if flash:
        ch = flash["channel"]
        code = flash["code"]
        if ch == CHANNEL_VK:
            deep = vk_deep_link(code)
            link_flash = {
                "channel": "vk",
                "label": "VK",
                "deep_link": deep,
                "code": code,
                "hint": f"если ссылка не сработала, напишите сообществу: привязка {code}",
            }
        elif ch == CHANNEL_TELEGRAM:
            deep = telegram_deep_link(code)
            link_flash = {
                "channel": "telegram",
                "label": "Telegram",
                "deep_link": deep,
                "code": code,
                "hint": "Откройте ссылку в Telegram (или перешлите сотруднику).",
            }

    tmpl = templates.TemplateResponse(
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
            is_master=is_master,
            notify_channels=notify_channels,
            notify_type_cols=notify_type_cols,
            first_connected_notify_idx=first_connected_notify_idx,
            has_connected_notify_channel=first_connected_notify_idx is not None,
            reminder_items=list_reminder_settings_for_ui(db, u),
            reminders_configured=bool(u.reminders_configured),
            reminder_max=MAX_REMINDERS_PER_USER,
            reminder_quick_hours=(1, 2, 6, 24),
            telegram_bot_username=settings.telegram_bot_username,
            vk_group_domain=settings.vk_group_domain,
            link_flash=link_flash,
            error=error,
            info=info,
        ),
    )
    for key, val in cookie_sink.headers.items():
        if key.lower() == "set-cookie":
            tmpl.headers.append(key, val)
    return tmpl


@router.get("/me", response_class=HTMLResponse)
def me_page(
    request: Request,
    msg: str | None = None,
    err: str | None = None,
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    error = None
    info = None
    if err == "tg_bot_username":
        error = "Задайте TELEGRAM_BOT_USERNAME в настройках окружения, затем создайте ссылку снова."
    elif err == "vk_group_domain":
        error = "Задайте VK_GROUP_DOMAIN в окружении, затем создайте ссылку снова."
    elif err == "tg_not_linked":
        error = "Сначала подключите Telegram."
    elif err == "vk_not_linked":
        error = "Сначала подключите VK."
    elif err == "tg_test_fail":
        error = "Не удалось отправить тестовое сообщение в Telegram."
    elif err == "vk_test_fail":
        error = "Не удалось отправить тестовое сообщение во VK."
    elif err == "pwd_current":
        error = "Неверный текущий пароль."
    elif err == "pwd_mismatch":
        error = "Новый пароль и подтверждение не совпадают."
    elif err == "pwd_short":
        error = "Новый пароль должен быть не короче 6 символов."
    elif err == "reminder_invalid":
        error = "Некорректное значение напоминания (от 0.5 до 72 часов, без повторов, не больше 5)."
    elif err == "reminder_limit":
        error = f"Не больше {MAX_REMINDERS_PER_USER} напоминаний."
    elif err == "reminder_dup":
        error = "Такое напоминание уже есть."
    elif err == "reminder_missing":
        error = "Напоминание не найдено."
    elif msg == "tg_connect":
        info = "Ссылка для привязки Telegram создана (действует 24 часа)."
    elif msg == "vk_connect":
        info = "Ссылка для привязки VK создана (действует 24 часа)."
    elif msg == "tg_disconnected":
        info = "Telegram отключён."
    elif msg == "vk_disconnected":
        info = "VK отключён."
    elif msg == "tg_test_ok":
        info = "Тестовое сообщение в Telegram отправлено."
    elif msg == "vk_test_ok":
        info = "Тестовое сообщение во VK отправлено."
    elif msg == "notify_saved":
        info = "Настройка уведомлений сохранена."
    elif msg == "reminders_saved":
        info = "Напоминания сохранены."
    elif msg == "reminder_added":
        info = "Напоминание добавлено."
    elif msg == "reminder_removed":
        info = "Напоминание удалено."
    elif msg == "reminders_cleared":
        info = "Список напоминаний очищен."
    elif msg == "pwd_ok":
        info = "Пароль изменён."
    return _me_page_response(
        request,
        current_user=current_user,
        db=db,
        error=error,
        info=info,
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
    resp = RedirectResponse(url="/me?msg=tg_connect", status_code=303)
    set_notify_link_flash(resp, channel=CHANNEL_TELEGRAM, plain_code=plain)
    return resp


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


@router.post("/me/vk/connect")
def me_vk_connect(
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    u = _load_self_user(db, current_user.id)
    if not get_settings().vk_group_domain:
        return RedirectResponse(url="/me?err=vk_group_domain", status_code=303)
    plain, _deep = create_vk_link_token(db, u.id)
    write_audit_rows(
        db,
        log_model=UserAuditLog,
        entity_field="user_id",
        entity_id=u.id,
        changed_by_user_id=current_user.id,
        changes=[FieldChange("vk_link", None, "код создан (моя карточка)")],
    )
    db.commit()
    resp = RedirectResponse(url="/me?msg=vk_connect", status_code=303)
    set_notify_link_flash(resp, channel=CHANNEL_VK, plain_code=plain)
    return resp


@router.post("/me/vk/disconnect")
def me_vk_disconnect(
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    u = _load_self_user(db, current_user.id)
    old = str(u.vk_user_id) if u.vk_user_id is not None else None
    unlink_vk_user(db, u)
    write_audit_rows(
        db,
        log_model=UserAuditLog,
        entity_field="user_id",
        entity_id=u.id,
        changed_by_user_id=current_user.id,
        changes=[FieldChange("vk_user_id", old, None)],
    )
    db.commit()
    return RedirectResponse(url="/me?msg=vk_disconnected", status_code=303)


@router.post("/me/telegram/notify")
@router.post("/me/notify")
async def me_notify_settings(
    request: Request,
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    u = _load_self_user(db, current_user.id)
    form = await request.form()
    roles = get_roles_for_user(db, int(u.id))
    old_enabled = bool(u.notify_enabled)
    pref_changes = apply_notify_prefs_from_form(u, roles, form)
    changes = [FieldChange(field, old, new) for field, old, new in pref_changes]
    if old_enabled != bool(u.notify_enabled):
        changes.append(FieldChange("notify_enabled", str(old_enabled), str(bool(u.notify_enabled))))
    if changes:
        write_audit_rows(
            db,
            log_model=UserAuditLog,
            entity_field="user_id",
            entity_id=u.id,
            changed_by_user_id=current_user.id,
            changes=changes,
        )
    db.commit()
    return RedirectResponse(url="/me?msg=notify_saved", status_code=303)


def _reminder_err_redirect(exc: ValueError) -> RedirectResponse:
    msg = str(exc or "")
    if "уже есть" in msg:
        code = "reminder_dup"
    elif "Не больше" in msg:
        code = "reminder_limit"
    elif "не найдено" in msg.lower() or "Не найдено" in msg:
        code = "reminder_missing"
    else:
        code = "reminder_invalid"
    return RedirectResponse(url=f"/me?err={code}", status_code=303)


@router.post("/me/reminders/add")
async def me_reminders_add(
    request: Request,
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    u = _load_self_user(db, current_user.id)
    form = await request.form()
    raw_hours = None
    if hasattr(form, "getlist"):
        vals = [v for v in form.getlist("hours") if v is not None and str(v).strip() != ""]
        raw_hours = vals[-1] if vals else None
    if raw_hours is None:
        raw_hours = form.get("hours")
    try:
        add_user_reminder_hours(db, u, raw_hours)
        db.commit()
    except ValueError as e:
        db.rollback()
        return _reminder_err_redirect(e)
    return RedirectResponse(url="/me?msg=reminder_added", status_code=303)


@router.post("/me/reminders/delete")
async def me_reminders_delete(
    request: Request,
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    u = _load_self_user(db, current_user.id)
    form = await request.form()
    try:
        minutes = int(str(form.get("minutes_before") or "0"))
        remove_user_reminder_minutes(db, u, minutes)
        db.commit()
    except (ValueError, TypeError) as e:
        db.rollback()
        if isinstance(e, ValueError) and str(e):
            return _reminder_err_redirect(e)
        return RedirectResponse(url="/me?err=reminder_invalid", status_code=303)
    return RedirectResponse(url="/me?msg=reminder_removed", status_code=303)


@router.post("/me/reminders/clear")
def me_reminders_clear(
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    u = _load_self_user(db, current_user.id)
    clear_user_reminders(db, u)
    db.commit()
    return RedirectResponse(url="/me?msg=reminders_cleared", status_code=303)


@router.post("/me/reminders/save")
async def me_reminders_save(
    request: Request,
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Сохранить полный список (hours из формы, можно несколько полей hours)."""
    u = _load_self_user(db, current_user.id)
    form = await request.form()
    raw_list = form.getlist("hours") if hasattr(form, "getlist") else [form.get("hours")]
    try:
        minutes_list: list[int] = []
        for raw in raw_list:
            if raw is None or str(raw).strip() == "":
                continue
            hours = parse_reminder_hours(raw)
            minutes_list.append(hours_to_minutes(hours))
        replace_user_reminder_minutes(db, u, minutes_list)
        db.commit()
    except ValueError as e:
        db.rollback()
        return _reminder_err_redirect(e)
    return RedirectResponse(url="/me?msg=reminders_saved", status_code=303)


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


@router.post("/me/vk/test")
def me_vk_test(
    current_user: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    u = _load_self_user(db, current_user.id)
    if u.vk_user_id is None:
        return RedirectResponse(url="/me?err=vk_not_linked", status_code=303)
    try:
        send_vk(
            int(u.vk_user_id),
            f"Тест LivingBraiding: уведомления VK для {u.display_name} работают.",
        )
    except Exception:
        return RedirectResponse(url="/me?err=vk_test_fail", status_code=303)
    return RedirectResponse(url="/me?msg=vk_test_ok", status_code=303)


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
