"""Типы уведомлений сотрудника отдельно для каждого канала (vk, max, telegram)."""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.db.models import NotificationChannel, User, UserNotifyChannelPref, UserRole
from app.forms_parse import parse_bool

# Ключ prefs → (поле User-сводки, короткий UI-заголовок, роли, которым галочка доступна)
NOTIFY_TYPE_SPECS: tuple[tuple[str, str, str, frozenset[UserRole]], ...] = (
    ("bookings", "notify_bookings", "Брони", frozenset({UserRole.MASTER, UserRole.ADMIN, UserRole.ADMIN_SUPER})),
    (
        "visit",
        "notify_visit",
        "Визит",
        frozenset({UserRole.MASTER, UserRole.HELPER, UserRole.ADMIN, UserRole.ADMIN_SUPER}),
    ),
    ("work", "notify_work", "Работа (товары)", frozenset({UserRole.MASTER})),
    ("hourly_work", "notify_hourly_work", "Почасовая", frozenset({UserRole.MASTER, UserRole.HELPER})),
    (
        "product_sale",
        "notify_product_sale",
        "Продажа",
        frozenset({UserRole.MASTER, UserRole.ADMIN, UserRole.ADMIN_SUPER}),
    ),
    (
        "consultation",
        "notify_consultation",
        "Консультация",
        frozenset({UserRole.MASTER, UserRole.ADMIN, UserRole.ADMIN_SUPER}),
    ),
    ("work_plan", "notify_work_plan", "План работ", frozenset({UserRole.MASTER, UserRole.HELPER})),
)

NOTIFY_TYPE_KEYS: tuple[str, ...] = tuple(spec[0] for spec in NOTIFY_TYPE_SPECS)

NOTIFY_CHANNEL_SPECS: tuple[tuple[str, str], ...] = (
    ("vk", "VK"),
    ("max", "Max"),
    ("telegram", "Telegram"),
)
NOTIFY_CHANNEL_IDS: tuple[str, ...] = tuple(spec[0] for spec in NOTIFY_CHANNEL_SPECS)

# entity_type из staff_assignment_created → ключ prefs
_ENTITY_TO_PREF: dict[str, str] = {
    "visit": "visit",
    "work": "work",
    "hourly_work": "hourly_work",
    "product_sale": "product_sale",
    "consultation": "consultation",
    "work_plan": "work_plan",
}

_SHARED_PREF_COLUMNS: tuple[tuple[str, str], ...] = tuple((spec[0], spec[1]) for spec in NOTIFY_TYPE_SPECS)


def _spec_by_key(key: str) -> tuple[str, str, str, frozenset[UserRole]]:
    for spec in NOTIFY_TYPE_SPECS:
        if spec[0] == key:
            return spec
    raise KeyError(key)


def channel_id(channel: str | NotificationChannel) -> str:
    if isinstance(channel, NotificationChannel):
        return channel.value
    return str(channel or "").strip().lower()


def notify_type_available(roles: Iterable[UserRole], key: str) -> bool:
    _key, _field, _label, allowed = _spec_by_key(key)
    return bool(set(roles).intersection(allowed))


def notify_type_field(key: str) -> str:
    return _spec_by_key(key)[1]


def notify_type_label(key: str) -> str:
    return _spec_by_key(key)[2]


def channel_connected(user: User, channel: str | NotificationChannel) -> bool:
    ch = channel_id(channel)
    if ch == "vk":
        return user.vk_user_id is not None
    if ch == "max":
        return user.max_user_id is not None
    if ch == "telegram":
        return user.telegram_chat_id is not None
    return False


def _channel_rows(user: User) -> list[UserNotifyChannelPref]:
    return list(user.notify_channel_prefs or [])


def _find_row(user: User, key: str, channel: str) -> UserNotifyChannelPref | None:
    ch = channel_id(channel)
    for row in _channel_rows(user):
        if row.channel == ch and row.pref_key == key:
            return row
    return None


def get_channel_pref(user: User, key: str, channel: str | NotificationChannel) -> bool:
    """Галочка канала. Если строк ещё нет — общее значение со столбца users."""
    row = _find_row(user, key, channel)
    if row is not None:
        return bool(row.enabled)
    if _channel_rows(user):
        return False
    return bool(getattr(user, notify_type_field(key), False))


def get_notify_pref(user: User, key: str) -> bool:
    """Включён хотя бы в одном канале (сводка и legacy)."""
    if _channel_rows(user):
        return any(get_channel_pref(user, key, ch) for ch in NOTIFY_CHANNEL_IDS)
    return bool(getattr(user, notify_type_field(key), False))


def _upsert_channel_pref(user: User, key: str, channel: str, value: bool) -> None:
    ch = channel_id(channel)
    row = _find_row(user, key, ch)
    if row is None:
        user.notify_channel_prefs.append(
            UserNotifyChannelPref(channel=ch, pref_key=key, enabled=bool(value))
        )
        return
    row.enabled = bool(value)


def set_notify_pref(
    user: User,
    key: str,
    value: bool,
    channel: str | NotificationChannel | None = None,
) -> None:
    targets = (channel_id(channel),) if channel is not None else NOTIFY_CHANNEL_IDS
    for ch in targets:
        _upsert_channel_pref(user, key, ch, bool(value))
    sync_notify_enabled_from_prefs(user)


def sync_notify_enabled_from_prefs(user: User) -> bool:
    """Столбцы users и notify_enabled = OR по подключённым каналам (если ни одного — по всем сохранённым)."""
    if _channel_rows(user):
        connected = [ch for ch in NOTIFY_CHANNEL_IDS if channel_connected(user, ch)]
        scope = connected or list(NOTIFY_CHANNEL_IDS)
        for key, field, _label, _allowed in NOTIFY_TYPE_SPECS:
            setattr(user, field, any(get_channel_pref(user, key, ch) for ch in scope))
    enabled = any(bool(getattr(user, field, False)) for _key, field, _label, _allowed in NOTIFY_TYPE_SPECS)
    user.notify_enabled = enabled
    return enabled


def apply_notify_prefs_from_legacy_flag(user: User, roles: Sequence[UserRole], *, enabled: bool) -> None:
    """Как раньше: True → включить доступные по ролям во всех каналах; False → всё выкл."""
    role_set = set(roles)
    for key, field, _label, allowed in NOTIFY_TYPE_SPECS:
        on = bool(enabled) and bool(role_set.intersection(allowed))
        setattr(user, field, on)
        for ch in NOTIFY_CHANNEL_IDS:
            _upsert_channel_pref(user, key, ch, on)
    sync_notify_enabled_from_prefs(user)


def apply_default_notify_prefs_for_new_user(user: User, roles: Sequence[UserRole]) -> None:
    """Новый сотрудник: по умолчанию включены все типы, доступные ролям, во всех каналах."""
    apply_notify_prefs_from_legacy_flag(user, roles, enabled=True)


def user_wants_booking_notifications(user: User, channel: str | NotificationChannel | None = None) -> bool:
    if channel is None:
        return get_notify_pref(user, "bookings")
    return get_channel_pref(user, "bookings", channel)


def user_wants_staff_assignment(
    user: User,
    entity_type: str,
    channel: str | NotificationChannel | None = None,
) -> bool:
    key = _ENTITY_TO_PREF.get(entity_type)
    if key is None:
        return False
    if channel is None:
        return get_notify_pref(user, key)
    return get_channel_pref(user, key, channel)


def notify_channel_meta(user: User) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for ch, label in NOTIFY_CHANNEL_SPECS:
        connected = channel_connected(user, ch)
        out.append(
            {
                "id": ch,
                "label": label,
                "connected": connected,
                "status_label": "подключён" if connected else "не подключён",
                "connect_url": f"/me/{ch}/connect",
                "disconnect_url": f"/me/{ch}/disconnect",
                "test_url": f"/me/{ch}/test",
            }
        )
    return out


def notify_matrix_for_ui(user: User, roles: Sequence[UserRole]) -> dict[str, Any]:
    """Строки — типы, столбцы — каналы."""
    role_list = list(roles)
    types: list[dict[str, Any]] = []
    for key, _field, label, _allowed in NOTIFY_TYPE_SPECS:
        types.append({"key": key, "label": label, "available": notify_type_available(role_list, key)})
    channels: list[dict[str, Any]] = []
    for meta in notify_channel_meta(user):
        cells: list[dict[str, Any]] = []
        for typ in types:
            cells.append(
                {
                    "key": typ["key"],
                    "available": typ["available"],
                    "enabled": get_channel_pref(user, typ["key"], meta["id"]) if typ["available"] else False,
                    "form_name": f"notify_{meta['id']}_{typ['key']}",
                }
            )
        channels.append({**meta, "cells": cells})
    return {"types": types, "channels": channels}


def notify_prefs_for_ui(user: User, roles: Sequence[UserRole]) -> list[dict[str, Any]]:
    """Плоский список типов (совместимость). enabled — хотя бы один канал."""
    role_list = list(roles)
    out: list[dict[str, Any]] = []
    for key, _field, label, _allowed in NOTIFY_TYPE_SPECS:
        available = notify_type_available(role_list, key)
        out.append(
            {
                "key": key,
                "form_name": f"notify_{key}",
                "label": label,
                "available": available,
                "enabled": get_notify_pref(user, key) if available else False,
            }
        )
    return out


def apply_notify_prefs_from_form(user: User, roles: Sequence[UserRole], form: Any) -> list[tuple[str, str, str]]:
    """Обновить галочки подключённых каналов. Недоступные по ролям и чужие каналы не меняются."""
    changes: list[tuple[str, str, str]] = []
    role_list = list(roles)
    for ch in NOTIFY_CHANNEL_IDS:
        if not channel_connected(user, ch):
            continue
        # Столбец пришёл в форме. Нет метки — канал не трогаем (частичное сохранение).
        if form.get(f"notify_channel_{ch}") is None:
            continue
        for key, _field, _label, _allowed in NOTIFY_TYPE_SPECS:
            if not notify_type_available(role_list, key):
                continue
            form_name = f"notify_{ch}_{key}"
            old = get_channel_pref(user, key, ch)
            raw = form.get(form_name)
            new = False if raw is None else parse_bool(raw)
            if old != new:
                _upsert_channel_pref(user, key, ch, new)
                changes.append((form_name, str(old), str(new)))
    sync_notify_enabled_from_prefs(user)
    return changes


def enabled_notify_types_summary(user: User, roles: Sequence[UserRole] | None = None) -> str:
    """Текст для админ-инфо: «Брони, Визит» или «выключены»."""
    labels: list[str] = []
    for key, _field, label, _allowed in NOTIFY_TYPE_SPECS:
        if roles is not None and not notify_type_available(roles, key):
            continue
        if get_notify_pref(user, key):
            labels.append(label)
    if not labels:
        return "выключены"
    return ", ".join(labels)


def copy_shared_notify_prefs_to_channels(connection: Connection) -> None:
    """Скопировать общие столбцы users во все три канала. Повторный запуск не дублирует строки."""
    for channel in NOTIFY_CHANNEL_IDS:
        for pref_key, column in _SHARED_PREF_COLUMNS:
            connection.execute(
                text(
                    f"""
                    INSERT INTO user_notify_channel_prefs (user_id, channel, pref_key, enabled)
                    SELECT id, :channel, :pref_key, {column}
                    FROM users
                    WHERE NOT EXISTS (
                        SELECT 1 FROM user_notify_channel_prefs p
                        WHERE p.user_id = users.id
                          AND p.channel = :channel
                          AND p.pref_key = :pref_key
                    )
                    """
                ),
                {"channel": channel, "pref_key": pref_key},
            )
