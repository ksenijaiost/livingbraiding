"""Куда слать внутренние уведомления: чат админов, общий чат, ЛС.

Хранится в studio settings (таблица settings), ключ internal_notify_prefs_json.
Если ключа нет — дефолты ниже.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.admin_chat import CHAT_KIND_ADMIN, CHAT_KIND_GENERAL, list_admin_chat_targets
from app.db.models import NotificationChannel, NotificationOutbox, NotificationOutboxStatus, Setting
from app.time_utils import utcnow_naive

PREFS_KEY = "internal_notify_prefs_json"

SLOT_ADMIN = "admin"
SLOT_GENERAL = "general"
SLOT_DM = "dm"

_TARGET_BY_SLOT = {
    SLOT_ADMIN: ("admin_chat", CHAT_KIND_ADMIN),
    SLOT_GENERAL: ("general_chat", CHAT_KIND_GENERAL),
}


def _row(admin: bool, general: bool, dm: bool | None) -> dict[str, bool | None]:
    return {"admin": admin, "general": general, "dm": dm}


DEFAULTS: dict[str, dict[str, bool | None]] = {
    "client_confirm": _row(False, False, False),
    "client_reschedule": _row(True, False, False),
    "client_cancel": _row(True, False, True),
    "client_contact": _row(True, False, None),
    "booking_created": _row(False, False, True),
    "booking_updated": _row(False, False, True),
    "booking_cancelled": _row(False, False, True),
    "booking_reminder": _row(False, False, True),
    "staff_visit": _row(False, False, True),
    "staff_work": _row(False, False, True),
    "staff_hourly_work": _row(False, False, True),
    "staff_product_sale": _row(False, False, True),
    "staff_consultation": _row(False, False, True),
    "staff_work_plan": _row(False, False, True),
}

EVENTS: list[dict[str, Any]] = [
    {
        "key": "client_confirm",
        "label": "Клиент подтвердил запись (ответ 1)",
        "group": "Ответы клиентов боту",
        "dm": True,
    },
    {
        "key": "client_reschedule",
        "label": "Клиент просит перенести запись (ответ 2)",
        "group": "Ответы клиентов боту",
        "dm": True,
    },
    {
        "key": "client_cancel",
        "label": "Клиент просит отменить запись (ответ 3)",
        "group": "Ответы клиентов боту",
        "dm": True,
    },
    {
        "key": "client_contact",
        "label": "Клиент просит связаться с администратором (ответ 4)",
        "group": "Ответы клиентов боту",
        "dm": False,
    },
    {"key": "booking_created", "label": "Бронь создана", "group": "События по броням в CRM", "dm": True},
    {"key": "booking_updated", "label": "Бронь изменена", "group": "События по броням в CRM", "dm": True},
    {
        "key": "booking_cancelled",
        "label": "Бронь отменена в CRM",
        "group": "События по броням в CRM",
        "dm": True,
    },
    {
        "key": "booking_reminder",
        "label": "Напоминание о предстоящей брони",
        "group": "События по броням в CRM",
        "dm": True,
    },
    {"key": "staff_visit", "label": "Визит", "group": "Создание записей", "dm": True},
    {"key": "staff_work", "label": "Работа (товары)", "group": "Создание записей", "dm": True},
    {"key": "staff_hourly_work", "label": "Почасовая работа", "group": "Создание записей", "dm": True},
    {"key": "staff_product_sale", "label": "Продажа товара", "group": "Создание записей", "dm": True},
    {"key": "staff_consultation", "label": "Консультация", "group": "Создание записей", "dm": True},
    {"key": "staff_work_plan", "label": "План работ", "group": "Создание записей", "dm": True},
]

STAFF_ENTITY_PREF = {
    "visit": "staff_visit",
    "work": "staff_work",
    "hourly_work": "staff_hourly_work",
    "product_sale": "staff_product_sale",
    "consultation": "staff_consultation",
    "work_plan": "staff_work_plan",
}

BOOKING_EVENT_PREF = {
    "booking_created": "booking_created",
    "booking_updated": "booking_updated",
    "booking_cancelled": "booking_cancelled",
    "booking_reminder": "booking_reminder",
}


def load_internal_notify_prefs(db: Session) -> dict[str, dict[str, bool | None]]:
    row = db.get(Setting, PREFS_KEY)
    saved: dict[str, Any] = {}
    if row is not None and (row.value or "").strip():
        try:
            parsed = json.loads(row.value)
        except json.JSONDecodeError:
            parsed = {}
        if isinstance(parsed, dict):
            saved = parsed
    out: dict[str, dict[str, bool | None]] = {}
    for key, default in DEFAULTS.items():
        item = {"admin": bool(default["admin"]), "general": bool(default["general"]), "dm": default["dm"]}
        raw = saved.get(key)
        if isinstance(raw, dict):
            if "admin" in raw:
                item["admin"] = bool(raw["admin"])
            if "general" in raw:
                item["general"] = bool(raw["general"])
            if item["dm"] is not None and "dm" in raw and raw["dm"] is not None:
                item["dm"] = bool(raw["dm"])
        out[key] = item
    return out


def wants_internal_notify(db: Session, event_key: str, slot: str) -> bool:
    prefs = load_internal_notify_prefs(db)
    row = prefs.get(event_key)
    if row is None:
        return False
    value = row.get(slot)
    return bool(value)


def save_internal_notify_prefs(db: Session, form: Any, *, user_id: int | None) -> None:
    """Чекбоксы формы: admin_<key>, general_<key>, dm_<key>. Нет в форме — выключено."""
    payload: dict[str, dict[str, bool | None]] = {}
    for event in EVENTS:
        key = str(event["key"])
        item: dict[str, bool | None] = {
            "admin": form.get(f"admin_{key}") is not None,
            "general": form.get(f"general_{key}") is not None,
        }
        if event["dm"]:
            item["dm"] = form.get(f"dm_{key}") is not None
        else:
            item["dm"] = None
        payload[key] = item
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    now = utcnow_naive()
    row = db.get(Setting, PREFS_KEY)
    if row is None:
        db.add(Setting(key=PREFS_KEY, value=raw, updated_at=now, updated_by_user_id=user_id))
    else:
        row.value = raw
        row.updated_at = now
        row.updated_by_user_id = user_id
    db.flush()


def events_for_ui(db: Session) -> list[dict[str, Any]]:
    prefs = load_internal_notify_prefs(db)
    out: list[dict[str, Any]] = []
    last_group = None
    for event in EVENTS:
        key = str(event["key"])
        flags = prefs[key]
        group = str(event["group"])
        out.append(
            {
                "key": key,
                "label": event["label"],
                "group": group if group != last_group else "",
                "dm_applicable": bool(event["dm"]),
                "admin": bool(flags["admin"]),
                "general": bool(flags["general"]),
                "dm": bool(flags["dm"]) if flags["dm"] is not None else False,
            }
        )
        last_group = group
    return out


def enqueue_pref_chats(
    db: Session,
    *,
    event_key: str,
    text: str,
    event_type: str,
    dedupe_for: Callable[[str, str], str],
    booking_id: int | None = None,
    client_id: int | None = None,
) -> list[NotificationOutbox]:
    """Поставить по одному сообщению в подключённые чаты включённых видов."""
    created: list[NotificationOutbox] = []
    for slot, (target_kind, chat_kind) in _TARGET_BY_SLOT.items():
        if not wants_internal_notify(db, event_key, slot):
            continue
        for ch, row in list_admin_chat_targets(db, chat_kind=chat_kind).items():
            if row is None:
                continue
            key = dedupe_for(chat_kind, ch)
            if len(key) > 200:
                key = key[:200]
            if db.scalar(select(NotificationOutbox.id).where(NotificationOutbox.dedupe_key == key).limit(1)):
                continue
            channel_enum = NotificationChannel(ch)
            payload = {
                "text": text,
                "target_id": int(row.chat_id),
                "target_kind": target_kind,
                "event_type": event_type,
                "booking_id": int(booking_id) if booking_id else None,
                "client_id": int(client_id) if client_id else None,
            }
            out = NotificationOutbox(
                user_id=None,
                client_id=int(client_id) if client_id else None,
                target_kind=target_kind,
                booking_id=int(booking_id) if booking_id else None,
                channel=channel_enum,
                event_type=event_type,
                payload_json=json.dumps(payload, ensure_ascii=False),
                body=text,
                status=NotificationOutboxStatus.PENDING,
                dedupe_key=key,
                attempt_count=0,
                created_at=utcnow_naive(),
            )
            try:
                with db.begin_nested():
                    db.add(out)
                    db.flush()
            except IntegrityError:
                continue
            created.append(out)
    return created
