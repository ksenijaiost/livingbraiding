"""Журнал notification_outbox для суперадмина и техспеца."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.auth import AuthUser, require_role
from app.booking_status_notify import SKIP_ALREADY, SKIP_DISABLED, SKIP_NO_CHANNEL, SKIP_PAST
from app.client_notify_history import outbox_subject_flags
from app.db.models import NotificationOutbox, NotificationOutboxStatus, UserRole
from app.db.session import get_db
from app.display_time import format_naive_utc_datetime, get_display_timezone
from app.forms_parse import parse_int
from app.notifications import (
    explain_outbox_error,
    list_recent_notification_outbox,
    retry_notification_outbox_entry,
)
from app.webui import ctx as _ctx, templates

router = APIRouter()

_OUTBOX_ROLES = Depends(require_role(UserRole.ADMIN_SUPER, UserRole.TECHSPEC))
# Сообщение клиенту сознательно не ставили в очередь. Это не сбой доставки.
_SKIP_REASONS = frozenset({SKIP_NO_CHANNEL, SKIP_PAST, SKIP_DISABLED, SKIP_ALREADY})

_EVENT_RU = {
    "booking_created": "бронь создана",
    "booking_updated": "бронь изменена",
    "booking_cancelled": "бронь отменена",
    "booking_reminder": "напоминание о записи",
    "staff_assignment_created": "назначение",
    "client_booking_reminder": "напоминание клиенту",
    "client_after_booking": "после визита",
    "client_booking_status": "статус брони",
    "booking_client_cancel_request": "просьба отменить",
    "booking_client_reschedule_request": "просьба перенести",
    "booking_client_confirmed": "клиент подтвердил",
    "client_contact_request": "просьба связаться",
    "client_bot_reply": "ответ бота",
    "client_care_memo_error": "памятка не ушла",
    "client_route_video_error": "видео маршрута не ушло",
}

_ENTITY_SUBJECT = {
    "visit": ("Визит", "/visits/{id}"),
    "work": ("Работа", "/sales/work/{id}"),
    "product_sale": ("Продажа", "/sales/products/{id}"),
    "consultation": ("Консультация", "/consultations/{id}"),
    "work_plan": ("План", "/work-plans/{id}"),
    "hourly_work": ("Почасовая", "/hourly-work/{id}"),
}


def _as_int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _payload(row: NotificationOutbox) -> dict:
    try:
        data = json.loads(row.payload_json or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _status_ru(status: NotificationOutboxStatus | str, *, skip_reason: str = "") -> str:
    raw = status.value if isinstance(status, NotificationOutboxStatus) else str(status)
    reason = (skip_reason or "").strip()
    if raw == "cancelled" and reason in _SKIP_REASONS:
        return f"не отправлено: {reason}"
    if raw == "failed":
        return "ошибка доставки"
    return {
        "pending": "ожидает",
        "sending": "отправка",
        "sent": "отправлено",
        "cancelled": "отменено",
    }.get(raw, raw)


def _event_ru(event_type: str) -> str:
    return _EVENT_RU.get(event_type, event_type)


def _subject_links(row: NotificationOutbox) -> list[dict[str, str]]:
    """Ссылка на бронь, визит или другую сущность. Пустой список — прочерк."""
    payload = _payload(row)
    entity_type = str(payload.get("entity_type") or "").strip()
    entity_id = _as_int(payload.get("entity_id"))
    if entity_type and entity_id:
        label, href = _ENTITY_SUBJECT.get(entity_type, ("Событие", ""))
        link = {"label": f"{label} №{entity_id}"}
        if href:
            link["href"] = href.format(id=entity_id)
        return [link]

    links: list[dict[str, str]] = []
    if row.visit_id:
        links.append({"label": f"Визит №{int(row.visit_id)}", "href": f"/visits/{int(row.visit_id)}"})
    if row.booking_id:
        links.append({"label": f"Бронь №{int(row.booking_id)}", "href": f"/bookings/{int(row.booking_id)}"})
    if links:
        return links

    if _as_int(payload.get("visit_id")):
        return [{"label": "визит удалён"}]
    if _as_int(payload.get("booking_id")):
        return [{"label": "бронь удалена"}]
    return []


def _recipient(row: NotificationOutbox, *, client_removed: bool) -> dict[str, str]:
    kind = (row.target_kind or "").strip().lower()
    if kind == "admin_chat":
        return {"name": "Чат админов", "mark": "чат", "href": ""}
    if kind == "general_chat":
        return {"name": "Общий чат", "mark": "чат", "href": ""}
    if row.user is not None:
        name = (row.user.display_name or row.user.username or "").strip() or f"#{row.user_id}"
        return {"name": name, "mark": "сотрудник", "href": ""}
    if row.client is not None:
        name = (row.client.name or "").strip() or f"#{row.client_id}"
        return {"name": name, "mark": "клиент", "href": f"/clients/{int(row.client.id)}"}
    if client_removed:
        return {"name": "клиент удалён", "mark": "", "href": ""}
    if row.user_id:
        return {"name": f"#{row.user_id}", "mark": "сотрудник", "href": ""}
    return {"name": "—", "mark": "", "href": ""}


@router.get("/admin/notification-outbox", response_class=HTMLResponse)
def admin_notification_outbox_page(
    request: Request,
    msg: str | None = None,
    err: str | None = None,
    current_user: AuthUser = _OUTBOX_ROLES,
    db: Session = Depends(get_db),
):
    tz = get_display_timezone(db)
    rows_raw = list_recent_notification_outbox(db, limit=50)
    rows = []
    for r in rows_raw:
        flags = outbox_subject_flags(r)
        who = _recipient(r, client_removed=bool(flags["client_removed"]))
        skip_reason = (r.error or "").strip()
        delivery_error = ""
        if not (r.status == NotificationOutboxStatus.CANCELLED and skip_reason in _SKIP_REASONS):
            delivery_error = explain_outbox_error(
                r.error,
                channel=r.channel.value if r.channel else None,
            )[:400]
        rows.append(
            {
                "id": int(r.id),
                "created_at": format_naive_utc_datetime(r.created_at, tz) or "—",
                "sent_at": format_naive_utc_datetime(r.sent_at, tz) if r.sent_at else "—",
                "recipient": who["name"],
                "recipient_mark": who["mark"],
                "recipient_href": who["href"],
                "channel": r.channel.value if r.channel else "—",
                "event": _event_ru(r.event_type or ""),
                "status": _status_ru(r.status, skip_reason=skip_reason),
                "status_raw": r.status.value if r.status else "",
                "attempts": int(r.attempt_count or 0),
                "error": delivery_error,
                "subjects": _subject_links(r),
            }
        )
    msg_ru = {
        "retried_ok": "Повторная отправка успешна.",
        "retried_fail": "Повтор выполнен, снова ошибка доставки — смотрите колонку «Ошибка».",
    }.get(msg or "", msg)
    err_ru = {
        "bad_id": "Некорректный ID записи.",
        "retry_fail": "Не удалось повторить отправку.",
    }.get(err or "", err)
    return templates.TemplateResponse(
        "admin_notification_outbox.html",
        _ctx(
            request,
            current_user=current_user,
            rows=rows,
            msg=msg_ru,
            err=err_ru,
            help_page_id="notification_outbox",
        ),
    )


@router.post("/admin/notification-outbox/{entry_id}/retry")
def admin_notification_outbox_retry(
    entry_id: int,
    current_user: AuthUser = _OUTBOX_ROLES,
    db: Session = Depends(get_db),
):
    try:
        eid = parse_int(entry_id, min=1, field_name="entry_id")
    except ValueError:
        return RedirectResponse(url="/admin/notification-outbox?err=bad_id", status_code=303)
    try:
        row = retry_notification_outbox_entry(db, eid)
        db.commit()
    except ValueError:
        return RedirectResponse(url="/admin/notification-outbox?err=retry_fail", status_code=303)
    except Exception:
        db.rollback()
        return RedirectResponse(url="/admin/notification-outbox?err=retry_fail", status_code=303)
    if row.status == NotificationOutboxStatus.SENT:
        return RedirectResponse(url="/admin/notification-outbox?msg=retried_ok", status_code=303)
    return RedirectResponse(url="/admin/notification-outbox?msg=retried_fail", status_code=303)
