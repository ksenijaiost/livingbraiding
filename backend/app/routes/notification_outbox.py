"""Журнал notification_outbox для суперадмина и техспеца."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.auth import AuthUser, require_role
from app.db.models import NotificationOutboxStatus, UserRole
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


def _status_ru(status: NotificationOutboxStatus | str) -> str:
    raw = status.value if isinstance(status, NotificationOutboxStatus) else str(status)
    return {
        "pending": "ожидает",
        "sending": "отправка",
        "sent": "отправлено",
        "failed": "ошибка",
    }.get(raw, raw)


def _event_ru(event_type: str) -> str:
    return {
        "booking_created": "бронь создана",
        "booking_updated": "бронь изменена",
        "booking_cancelled": "бронь отменена",
    }.get(event_type, event_type)


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
        rows.append(
            {
                "id": int(r.id),
                "created_at": format_naive_utc_datetime(r.created_at, tz) or "—",
                "sent_at": format_naive_utc_datetime(r.sent_at, tz) if r.sent_at else "—",
                "master": (r.user.display_name if r.user else None) or f"#{r.user_id}",
                "channel": r.channel.value if r.channel else "—",
                "event": _event_ru(r.event_type or ""),
                "status": _status_ru(r.status),
                "status_raw": r.status.value if r.status else "",
                "attempts": int(r.attempt_count or 0),
                "error": explain_outbox_error(
                    r.error,
                    channel=r.channel.value if r.channel else None,
                )[:400],
                "booking_id": r.booking_id,
            }
        )
    msg_ru = {
        "retried_ok": "Повторная отправка успешна.",
        "retried_fail": "Повтор выполнен, статус снова ошибка — смотрите колонку «Ошибка».",
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
