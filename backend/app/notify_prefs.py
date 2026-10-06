"""Типы уведомлений сотрудника (общие для VK/Telegram, не per-channel)."""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from app.db.models import User, UserRole
from app.forms_parse import parse_bool

# Ключ prefs → (поле User, короткий UI-заголовок, роли, которым галочка доступна)
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

# entity_type из staff_assignment_created → ключ prefs
_ENTITY_TO_PREF: dict[str, str] = {
    "visit": "visit",
    "work": "work",
    "hourly_work": "hourly_work",
    "product_sale": "product_sale",
    "consultation": "consultation",
    "work_plan": "work_plan",
}


def _spec_by_key(key: str) -> tuple[str, str, str, frozenset[UserRole]]:
    for spec in NOTIFY_TYPE_SPECS:
        if spec[0] == key:
            return spec
    raise KeyError(key)


def notify_type_available(roles: Iterable[UserRole], key: str) -> bool:
    _key, _field, _label, allowed = _spec_by_key(key)
    return bool(set(roles).intersection(allowed))


def notify_type_field(key: str) -> str:
    return _spec_by_key(key)[1]


def notify_type_label(key: str) -> str:
    return _spec_by_key(key)[2]


def get_notify_pref(user: User, key: str) -> bool:
    return bool(getattr(user, notify_type_field(key), False))


def set_notify_pref(user: User, key: str, value: bool) -> None:
    setattr(user, notify_type_field(key), bool(value))


def sync_notify_enabled_from_prefs(user: User) -> bool:
    """notify_enabled = OR всех типов (для списка сотрудников и legacy)."""
    enabled = any(get_notify_pref(user, key) for key in NOTIFY_TYPE_KEYS)
    user.notify_enabled = enabled
    return enabled


def apply_notify_prefs_from_legacy_flag(user: User, roles: Sequence[UserRole], *, enabled: bool) -> None:
    """Как в миграции: True → включить доступные по ролям; False → всё выкл."""
    role_set = set(roles)
    for key, field, _label, allowed in NOTIFY_TYPE_SPECS:
        on = bool(enabled) and bool(role_set.intersection(allowed))
        setattr(user, field, on)
    sync_notify_enabled_from_prefs(user)


def apply_default_notify_prefs_for_new_user(user: User, roles: Sequence[UserRole]) -> None:
    """Новый сотрудник: по умолчанию включены все типы, доступные ролям."""
    apply_notify_prefs_from_legacy_flag(user, roles, enabled=True)


def user_wants_booking_notifications(user: User) -> bool:
    return get_notify_pref(user, "bookings")


def user_wants_staff_assignment(user: User, entity_type: str) -> bool:
    key = _ENTITY_TO_PREF.get(entity_type)
    if key is None:
        return False
    return get_notify_pref(user, key)


def notify_prefs_for_ui(user: User, roles: Sequence[UserRole]) -> list[dict[str, Any]]:
    """Колонки для /me: key, label, enabled, available."""
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
    """Обновить доступные типы из POST. Возвращает audit-изменения (field, old, new)."""
    changes: list[tuple[str, str, str]] = []
    role_list = list(roles)
    for key, field, _label, _allowed in NOTIFY_TYPE_SPECS:
        if not notify_type_available(role_list, key):
            continue
        old = bool(getattr(user, field))
        raw = form.get(f"notify_{key}")
        new = False if raw is None else parse_bool(raw)
        if old != new:
            setattr(user, field, new)
            changes.append((field, str(old), str(new)))
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
