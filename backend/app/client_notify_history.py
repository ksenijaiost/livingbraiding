"""История уведомлений клиенту: outbox по брони и входящие ответы."""

from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload

from app.db.models import (
    ClientInboundMessage,
    ClientNotificationRule,
    NotificationOutbox,
    NotificationOutboxStatus,
    Visit,
)
from app.display_time import format_naive_utc_datetime

_CHANNEL = {"vk": "VK", "max": "Max", "telegram": "Telegram"}

_PARSED = {
    "confirm": "1 — подтвердила",
    "reschedule": "2 — просит перенести",
    "cancel": "3 — просит отменить",
    "contact_admin": "4 — просит связаться",
    "directions": "5 — как добраться",
    "unknown": "не распознано",
}

_BOT = {
    "confirm_1": "Подтверждение",
    "reschedule_2": "Перенос",
    "cancel_3": "Отмена",
    "contact_vk": "Связь с администратором",
    "contact_telegram": "Связь с администратором",
    "contact_max": "Связь с администратором",
    "directions_5": "Как добраться",
    "hint": "Подсказка",
    "no_booking": "Нет активной записи",
    "no_booking_reschedule": "Нет активной записи",
    "already_confirmed": "Уже подтверждена",
}

_EVENT = {
    "booking_client_cancel_request": "Запрос отмены",
    "booking_client_reschedule_request": "Просьба перенести",
    "client_contact_request": "Просьба связаться",
    "booking_created": "Бронь создана",
    "booking_updated": "Бронь изменена",
    "booking_cancelled": "Бронь отменена",
}


def _payload(row: NotificationOutbox) -> dict:
    try:
        data = json.loads(row.payload_json or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _channel_label(channel: object) -> str:
    raw = channel.value if hasattr(channel, "value") else str(channel or "")
    return _CHANNEL.get(raw.strip().lower(), raw or "—")


def _fmt_when(dt: datetime | None, tz_name: str) -> str:
    if dt is None:
        return ""
    return format_naive_utc_datetime(dt, tz_name, "%d.%m.%Y %H:%M")


def _hours_label(hours: float | None) -> str:
    if hours is None:
        return "Напоминание"
    val = float(hours)
    if abs(val - round(val)) < 1e-9:
        shown = str(int(round(val)))
    else:
        shown = f"{val:.1f}".replace(".", ",")
    return f"Напоминание за {shown} ч"


def _rule_hours(db: Session, rows: list[NotificationOutbox]) -> dict[int, float]:
    ids: set[int] = set()
    for row in rows:
        if row.rule_id is not None:
            ids.add(int(row.rule_id))
            continue
        raw = _payload(row).get("rule_id")
        if raw is not None:
            try:
                ids.add(int(raw))
            except (TypeError, ValueError):
                pass
    if not ids:
        return {}
    found = db.scalars(select(ClientNotificationRule).where(ClientNotificationRule.id.in_(ids))).all()
    return {int(rule.id): float(rule.hours) for rule in found}


def _out_type(row: NotificationOutbox, hours_by_rule: dict[int, float]) -> tuple[str, str]:
    event = str(row.event_type or "")
    payload = _payload(row)
    kind = (row.target_kind or payload.get("target_kind") or "user").strip().lower()
    mark = ""
    if kind == "admin_chat":
        mark = "в чат админов"
    elif kind == "user":
        mark = "мастеру"
    if event == "client_booking_reminder":
        rid = row.rule_id
        if rid is None and payload.get("rule_id") is not None:
            try:
                rid = int(payload["rule_id"])
            except (TypeError, ValueError):
                rid = None
        return _hours_label(hours_by_rule.get(int(rid)) if rid is not None else None), mark
    if event == "client_after_booking":
        return "После визита", mark
    if event == "client_care_memo_error":
        return ((row.body or "").strip() or "Памятка — ошибка"), mark
    if event == "client_bot_reply":
        key = str(payload.get("reply_key") or "")
        return _BOT.get(key, "Ответ бота"), mark
    return _EVENT.get(event, "Уведомление"), mark


def _destination(row: NotificationOutbox) -> str:
    kind = (row.target_kind or "").strip().lower()
    if kind == "admin_chat":
        return "в чат админов"
    if kind == "client":
        return "клиенту"
    name = ""
    user = row.user
    if user is not None:
        name = (user.display_name or user.username or "").strip()
    if not name and row.user_id:
        name = f"#{int(row.user_id)}"
    return f"мастеру {name}".strip() if name else "мастеру"


def _status_label(row: NotificationOutbox, tz_name: str) -> str:
    status = row.status.value if hasattr(row.status, "value") else str(row.status or "")
    if status == NotificationOutboxStatus.SENT.value:
        when = _fmt_when(row.sent_at, tz_name)
        return f"отправлено ({when})" if when else "отправлено"
    if status == NotificationOutboxStatus.FAILED.value:
        err = (row.error or "").strip() or "без текста"
        return f"ошибка: {err} (попыток: {int(row.attempt_count or 0)})"
    if status == NotificationOutboxStatus.CANCELLED.value:
        return "отменено"
    if status == NotificationOutboxStatus.SENDING.value:
        return "отправляется"
    when = _fmt_when(row.scheduled_at or row.created_at, tz_name)
    return f"запланировано ({when})" if when else "запланировано"


def _memos_label(row: NotificationOutbox) -> str:
    names = _payload(row).get("care_memos_attached") or []
    if not isinstance(names, list):
        return ""
    clean = [str(name).strip() for name in names if str(name).strip()]
    if not clean:
        return ""
    return "Памятки: " + ", ".join(clean)


def _out_item(row: NotificationOutbox, tz_name: str, hours_by_rule: dict[int, float]) -> dict:
    type_label, mark = _out_type(row, hours_by_rule)
    text = (row.body or "").strip() or str(_payload(row).get("text") or "").strip()
    at = row.sent_at or row.scheduled_at or row.created_at
    return {
        "kind": "out",
        "seq": int(row.id or 0),
        "at": at,
        "at_label": _fmt_when(at, tz_name),
        "type_label": type_label,
        "mark": mark,
        "channel_label": _channel_label(row.channel),
        "destination": _destination(row),
        "status_label": _status_label(row, tz_name),
        "text": text,
        "parsed_label": "",
        "memos_label": _memos_label(row),
        "booking_id": int(row.booking_id) if row.booking_id else None,
    }


def _in_item(row: ClientInboundMessage, tz_name: str) -> dict:
    parsed = _PARSED.get((row.parsed_action or "").strip(), "не распознано")
    return {
        "kind": "in",
        "seq": int(row.id or 0),
        "at": row.received_at,
        "at_label": _fmt_when(row.received_at, tz_name),
        "type_label": "Ответ клиента",
        "mark": "",
        "channel_label": _channel_label(row.channel),
        "destination": "",
        "status_label": "",
        "text": row.text or "",
        "parsed_label": parsed,
        "memos_label": "",
        "booking_id": int(row.booking_id) if row.booking_id else None,
    }


def booking_notify_timeline(db: Session, booking_id: int, *, tz_name: str) -> list[dict]:
    """Сообщения по брони: исходящие (включая «после визита» по визиту) и ответы клиентки."""
    visit_ids = list(
        db.scalars(select(Visit.id).where(Visit.booking_id == int(booking_id))).all()
    )
    conds = [NotificationOutbox.booking_id == int(booking_id)]
    if visit_ids:
        conds.append(NotificationOutbox.visit_id.in_([int(v) for v in visit_ids]))
    rows = list(
        db.scalars(
            select(NotificationOutbox)
            .where(or_(*conds))
            .options(selectinload(NotificationOutbox.user))
            .order_by(NotificationOutbox.id.asc())
        ).all()
    )
    inbound = list(
        db.scalars(
            select(ClientInboundMessage)
            .where(ClientInboundMessage.booking_id == int(booking_id))
            .order_by(ClientInboundMessage.id.asc())
        ).all()
    )
    hours = _rule_hours(db, rows)
    items = [_out_item(row, tz_name, hours) for row in rows]
    items.extend(_in_item(row, tz_name) for row in inbound)
    items.sort(key=lambda item: (item["at"] or datetime.min, 0 if item["kind"] == "out" else 1, item["seq"]))
    return items


def client_recent_messages(db: Session, client_id: int, *, tz_name: str, limit: int = 10) -> list[dict]:
    """Последние входящие и исходящие клиенту. Новые сверху."""
    lim = max(1, int(limit))
    rows = list(
        db.scalars(
            select(NotificationOutbox)
            .where(
                NotificationOutbox.client_id == int(client_id),
                NotificationOutbox.target_kind == "client",
            )
            .options(selectinload(NotificationOutbox.user))
            .order_by(NotificationOutbox.id.desc())
            .limit(lim * 3)
        ).all()
    )
    inbound = list(
        db.scalars(
            select(ClientInboundMessage)
            .where(ClientInboundMessage.client_id == int(client_id))
            .order_by(ClientInboundMessage.id.desc())
            .limit(lim * 3)
        ).all()
    )
    hours = _rule_hours(db, rows)
    items = [_out_item(row, tz_name, hours) for row in rows]
    items.extend(_in_item(row, tz_name) for row in inbound)
    items.sort(key=lambda item: (item["at"] or datetime.min, item["seq"]), reverse=True)
    return items[:lim]
