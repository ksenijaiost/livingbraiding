"""Настраиваемые цифры ответов клиентки и переменные {{reply_hint}} / {{reply_hint_no_confirm}} / {{reply_hint_general}}."""

from __future__ import annotations

from typing import Any, Sequence

from sqlalchemy import select, text
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from app.db.models import ClientReplyDigitAction
from app.time_utils import utcnow_naive

ACTION_CONFIRM = "confirm"
ACTION_RESCHEDULE = "reschedule"
ACTION_CANCEL = "cancel"
ACTION_CONTACT = "contact_admin"
ACTION_DIRECTIONS = "directions"

ACTION_ORDER: tuple[str, ...] = (
    ACTION_CONFIRM,
    ACTION_RESCHEDULE,
    ACTION_CANCEL,
    ACTION_CONTACT,
    ACTION_DIRECTIONS,
)

ACTION_TITLES: dict[str, str] = {
    ACTION_CONFIRM: "Подтвердить",
    ACTION_RESCHEDULE: "Перенести",
    ACTION_CANCEL: "Отменить",
    ACTION_CONTACT: "Написать администратору",
    ACTION_DIRECTIONS: "Как добраться",
}

DEFAULT_HINT_LABELS: dict[str, str] = {
    ACTION_CONFIRM: "подтвердить",
    ACTION_RESCHEDULE: "перенести",
    ACTION_CANCEL: "отменить",
    ACTION_CONTACT: "написать администратору",
    ACTION_DIRECTIONS: "как добраться",
}

DEFAULT_DIGITS: dict[str, str] = {
    ACTION_CONFIRM: "1",
    ACTION_RESCHEDULE: "2",
    ACTION_CANCEL: "3",
    ACTION_CONTACT: "4",
    ACTION_DIRECTIONS: "5",
}

# {{reply_hint_general}} — только эти действия, если они включены.
GENERAL_HINT_ACTIONS = frozenset({ACTION_CONTACT, ACTION_DIRECTIONS})

# Подпись под названием действия: в какие подсказки (1/2/3) входит строка.
HINT_MEMBERSHIP_NOTE: dict[str, str] = {
    ACTION_CONFIRM: "входит в 1",
    ACTION_RESCHEDULE: "входит в 1, 2",
    ACTION_CANCEL: "входит в 1, 2",
    ACTION_CONTACT: "входит в 1, 2, 3",
    ACTION_DIRECTIONS: "входит в 1, 2, 3",
}

# Точная строка из старых шаблонов (с точкой) — её меняет миграция на {{reply_hint}}.
LEGACY_REPLY_HINT_LINE = (
    "Ответьте: 1 — подтвердить, 2 — перенести, 3 — отменить, "
    "4 — написать администратору, 5 — как добраться."
)
LEGACY_REPLY_HINT_LINE_NO_DOT = LEGACY_REPLY_HINT_LINE.rstrip(".")

MANUAL_DIGITS_WARNING = (
    "В тексте вписаны цифры вручную, лучше использовать {{reply_hint}}"
)
DIGITS_CHANGED_WARNING = (
    "Клиентки, которым сообщение ушло раньше, могут ответить по старым цифрам. "
    "Проверьте тексты, где цифры вписаны вручную"
)


class DigitConflictError(ValueError):
    """Конфликт цифр среди включённых действий. field_errors: action -> сообщение."""

    def __init__(self, field_errors: dict[str, str]) -> None:
        self.field_errors = field_errors
        super().__init__("Цифры среди включённых действий должны быть разными.")


def ensure_default_reply_digits(db: Session) -> bool:
    have = set(db.scalars(select(ClientReplyDigitAction.action)).all())
    created = False
    now = utcnow_naive()
    for index, action in enumerate(ACTION_ORDER):
        if action in have:
            continue
        db.add(
            ClientReplyDigitAction(
                action=action,
                digit=DEFAULT_DIGITS[action],
                is_enabled=True,
                hint_label=DEFAULT_HINT_LABELS[action],
                position=index,
                updated_at=now,
            )
        )
        created = True
    if created:
        db.flush()
    return created


def list_reply_digit_actions(db: Session) -> list[ClientReplyDigitAction]:
    """Строки из БД без автосоздания (безопасно при before_flush / рендере)."""
    rows = list(db.scalars(select(ClientReplyDigitAction)).all())
    by_action = {row.action: row for row in rows}
    ordered: list[ClientReplyDigitAction] = []
    for action in ACTION_ORDER:
        row = by_action.get(action)
        if row is not None:
            ordered.append(row)
    for row in rows:
        if row.action not in ACTION_TITLES:
            ordered.append(row)
    return ordered


def _default_action_snapshots() -> list[dict[str, Any]]:
    return [
        {
            "action": action,
            "title": ACTION_TITLES[action],
            "digit": DEFAULT_DIGITS[action],
            "is_enabled": True,
            "hint_label": DEFAULT_HINT_LABELS[action],
            "position": index,
        }
        for index, action in enumerate(ACTION_ORDER)
    ]


def reply_digits_for_ui(db: Session) -> list[dict[str, Any]]:
    ensure_default_reply_digits(db)
    return [
        {
            "action": row.action,
            "title": ACTION_TITLES.get(row.action, row.action),
            "digit": row.digit,
            "is_enabled": bool(row.is_enabled),
            "hint_label": row.hint_label,
            "position": int(row.position),
            "hint_membership": HINT_MEMBERSHIP_NOTE.get(row.action, ""),
        }
        for row in list_reply_digit_actions(db)
    ]


def _parse_digit(raw: Any) -> str:
    s = str(raw if raw is not None else "").strip()
    if len(s) != 1 or s not in "0123456789":
        raise ValueError("Цифра ответа: одна цифра от 0 до 9.")
    return s


def save_reply_digits_from_form(
    db: Session,
    *,
    actions: Sequence[str],
    digits: Sequence[Any],
    enabled_flags: Sequence[Any],
    labels: Sequence[Any],
) -> bool:
    """Сохранить таблицу цифр. True, если хотя бы одна цифра изменилась.

    При конфликте включённых цифр — DigitConflictError.
    """
    if not (len(actions) == len(digits) == len(enabled_flags) == len(labels)):
        raise ValueError("Не удалось сохранить цифры ответов.")
    ensure_default_reply_digits(db)
    parsed: list[tuple[str, str, bool, str]] = []
    for action, raw_digit, raw_enabled, raw_label in zip(
        actions, digits, enabled_flags, labels, strict=True
    ):
        key = str(action or "").strip()
        if key not in ACTION_TITLES:
            raise ValueError("Неизвестное действие ответа.")
        digit = _parse_digit(raw_digit)
        label = str(raw_label or "").strip()
        if not label:
            raise ValueError(f"Укажите подпись для «{ACTION_TITLES[key]}».")
        if len(label) > 80:
            raise ValueError(f"Подпись для «{ACTION_TITLES[key]}» слишком длинная.")
        parsed.append((key, digit, str(raw_enabled) == "1", label))

    enabled_digits: dict[str, str] = {}
    field_errors: dict[str, str] = {}
    for action, digit, enabled, _label in parsed:
        if not enabled:
            continue
        other = enabled_digits.get(digit)
        if other is not None:
            msg = f"Цифра {digit} уже у действия «{ACTION_TITLES.get(other, other)}»."
            field_errors[action] = msg
            if other not in field_errors:
                field_errors[other] = f"Цифра {digit} совпадает с «{ACTION_TITLES.get(action, action)}»."
        else:
            enabled_digits[digit] = action
    if field_errors:
        raise DigitConflictError(field_errors)

    now = utcnow_naive()
    digits_changed = False
    by_action = {row.action: row for row in list_reply_digit_actions(db)}
    for action, digit, enabled, label in parsed:
        row = by_action.get(action)
        if row is None:
            continue
        if str(row.digit) != digit:
            digits_changed = True
        row.digit = digit
        row.is_enabled = enabled
        row.hint_label = label
        row.updated_at = now
    db.flush()
    return digits_changed


def _enabled_snapshots(db: Session) -> list[dict[str, Any]]:
    rows = list_reply_digit_actions(db)
    if not rows:
        return _default_action_snapshots()
    return [
        {
            "action": row.action,
            "digit": str(row.digit),
            "is_enabled": bool(row.is_enabled),
            "hint_label": row.hint_label,
        }
        for row in rows
        if bool(row.is_enabled)
    ]


def action_for_digit(db: Session, digit: str | None) -> str | None:
    """Включённое действие для цифры или None."""
    if not digit:
        return None
    for row in _enabled_snapshots(db):
        if str(row["digit"]) == str(digit):
            return str(row["action"])
    return None


def digit_for_action(db: Session, action: str, *, only_enabled: bool = False) -> str | None:
    rows = list_reply_digit_actions(db)
    if not rows:
        return DEFAULT_DIGITS.get(action)
    for row in rows:
        if row.action == action:
            if only_enabled and not bool(row.is_enabled):
                return None
            return str(row.digit)
    return DEFAULT_DIGITS.get(action)


def build_reply_hint(
    db: Session,
    *,
    exclude_confirm: bool = False,
    only_actions: frozenset[str] | None = None,
) -> str:
    """Собрать подсказку «Ответьте: …».

    only_actions — узкий набор (для {{reply_hint_general}}). Если после фильтра
    строк нет, такая подсказка пустая. Полная подсказка в этом случае остаётся
    фразой «Ответьте сообщением администратору.».
    """
    parts: list[str] = []
    for row in _enabled_snapshots(db):
        action = str(row["action"])
        if exclude_confirm and action == ACTION_CONFIRM:
            continue
        if only_actions is not None and action not in only_actions:
            continue
        parts.append(f"{row['digit']} — {row['hint_label']}")
    if not parts:
        if only_actions is not None:
            return ""
        return "Ответьте сообщением администратору."
    return "Ответьте: " + ", ".join(parts) + "."


def template_has_manual_reply_digits(template: str | None) -> bool:
    text_value = template or ""
    if "{{reply_hint" in text_value:
        return False
    if "Ответьте:" not in text_value:
        return False
    return any(ch.isdigit() for ch in text_value)


def reply_hint_system_values(db: Session) -> dict[str, str]:
    return {
        "reply_hint": build_reply_hint(db, exclude_confirm=False),
        "reply_hint_no_confirm": build_reply_hint(db, exclude_confirm=True),
        "reply_hint_general": build_reply_hint(db, only_actions=GENERAL_HINT_ACTIONS),
    }


def parsed_action_labels(db: Session) -> dict[str, str]:
    """Подписи входящих в истории: «2 — просит перенести»."""

    def _digit(action: str) -> str:
        return digit_for_action(db, action) or DEFAULT_DIGITS.get(action, "?")

    return {
        ACTION_CONFIRM: f"{_digit(ACTION_CONFIRM)} — подтвердила",
        ACTION_RESCHEDULE: f"{_digit(ACTION_RESCHEDULE)} — просит перенести",
        ACTION_CANCEL: f"{_digit(ACTION_CANCEL)} — просит отменить",
        ACTION_CONTACT: f"{_digit(ACTION_CONTACT)} — просит связаться",
        ACTION_DIRECTIONS: f"{_digit(ACTION_DIRECTIONS)} — как добраться",
        "unknown": "не распознано",
    }


def migrate_replace_legacy_reply_hint(connection: Connection) -> dict[str, list[int]]:
    """Заменить точное совпадение старой строки на {{reply_hint}}.

    Для booking_status_notify_rules со статусом ACTIVE — на {{reply_hint_no_confirm}}.

    Возвращает id шаблонов, где строка совпала (replaced) и где осталась
    ручная подсказка с цифрами (manual) — для отчёта.
    """
    replaced: list[int] = []
    manual: list[int] = []

    def _replace_in_rows(
        rows: list,
        *,
        table: str,
        id_idx: int = 0,
        tpl_idx: int = 1,
        status_idx: int | None = None,
    ) -> None:
        for row in rows:
            row_id = int(row[id_idx])
            original = "" if row[tpl_idx] is None else str(row[tpl_idx])
            if LEGACY_REPLY_HINT_LINE in original:
                status = str(row[status_idx]) if status_idx is not None else ""
                token = (
                    "{{reply_hint_no_confirm}}"
                    if status == "ACTIVE"
                    else "{{reply_hint}}"
                )
                new_text = original.replace(LEGACY_REPLY_HINT_LINE, token)
                connection.execute(
                    text(f"UPDATE {table} SET template = :tpl WHERE id = :rid"),
                    {"tpl": new_text, "rid": row_id},
                )
                replaced.append(row_id)
                original = new_text
            if template_has_manual_reply_digits(original):
                manual.append(row_id)

    try:
        rows = connection.execute(
            text("SELECT id, template FROM client_notification_rules")
        ).fetchall()
        _replace_in_rows(rows, table="client_notification_rules")
    except Exception:
        pass
    try:
        rows = connection.execute(
            text("SELECT id, template, status FROM booking_status_notify_rules")
        ).fetchall()
        _replace_in_rows(
            rows,
            table="booking_status_notify_rules",
            status_idx=2,
        )
    except Exception:
        pass
    return {"replaced": replaced, "manual": manual}


def seed_reply_digit_actions(connection: Connection) -> None:
    now = utcnow_naive()
    for index, action in enumerate(ACTION_ORDER):
        connection.execute(
            text(
                """
                INSERT INTO client_reply_digit_actions
                    (action, digit, is_enabled, hint_label, position, updated_at)
                SELECT :action, :digit, :enabled, :label, :position, :updated_at
                WHERE NOT EXISTS (
                    SELECT 1 FROM client_reply_digit_actions WHERE action = :action
                )
                """
            ),
            {
                "action": action,
                "digit": DEFAULT_DIGITS[action],
                "enabled": True,
                "label": DEFAULT_HINT_LABELS[action],
                "position": index,
                "updated_at": now,
            },
        )
