"""Настройки уведомлений клиентам: правила before/after + рендер шаблонов.

Отправка и outbox для клиентов здесь не реализуются.
При первой загрузке пустой таблицы правил создаются дефолты (24ч/2ч before + 0ч after).
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Sequence
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.db.models import (
    Booking,
    BookingMaster,
    BookingPlannedService,
    BookingPlannedServiceMaster,
    Client,
    ClientNotificationRule,
    ClientNotificationRuleKind,
    Setting,
    User,
)
from app.setting_keys import CLIENT_NOTIFY_SERVICES_MARKER
from app.time_utils import utcnow_naive

DEFAULT_SERVICES_MARKER = "•"
MAX_RULES_PER_KIND = 20
MAX_HOURS = 720  # 30 суток

KIND_BEFORE = ClientNotificationRuleKind.BEFORE_BOOKING
KIND_AFTER = ClientNotificationRuleKind.AFTER_BOOKING

DEFAULT_BEFORE_TEMPLATE = (
    "Добрый день, {{name}}!\n\n"
    "Напоминаем: {{date_text}} ({{weekday}}) в {{time}} вы записаны:\n"
    "{{services}}\n"
    "Мастер: {{masters}}\n\n"
    "Ответьте 1 — подтвердить, 3 — отменить."
)
DEFAULT_AFTER_TEMPLATE = "Спасибо, что были у нас!"

_VAR_RE = re.compile(r"\{\{(\w+)\}\}")

_MONTHS_GENITIVE = (
    "",
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)
_WEEKDAYS = (
    "понедельник",
    "вторник",
    "среда",
    "четверг",
    "пятница",
    "суббота",
    "воскресенье",
)


def _minutes_ru(n: int) -> str:
    n = abs(int(n))
    mod10 = n % 10
    mod100 = n % 100
    if mod10 == 1 and mod100 != 11:
        word = "минута"
    elif mod10 in (2, 3, 4) and mod100 not in (12, 13, 14):
        word = "минуты"
    else:
        word = "минут"
    return f"{n} {word}"


def _to_local(dt: datetime | None, tz_name: str) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        utc_dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    else:
        utc_dt = dt.astimezone(ZoneInfo("UTC"))
    return utc_dt.astimezone(ZoneInfo(tz_name))


def _master_label(user: User | None, user_id: int) -> str:
    if user is None:
        return str(user_id)
    return (user.display_name or user.username or str(user_id)).strip() or str(user_id)


def _services_block(booking: Booking, marker: str) -> str:
    mark = (marker or DEFAULT_SERVICES_MARKER).strip() or DEFAULT_SERVICES_MARKER
    lines: list[str] = []
    planned = sorted(
        list(booking.planned_services or []),
        key=lambda x: (int(x.sort_order or 0), int(x.id or 0)),
    )
    for ps in planned:
        svc = ps.service
        name = ((svc.name if svc is not None else "") or "").strip()
        if not name:
            continue
        if ps.duration_minutes is not None:
            line = f"{mark} {name} {_minutes_ru(int(ps.duration_minutes))}"
        else:
            line = f"{mark} {name}"
        lines.append(line)
    if lines:
        return "\n".join(lines)
    # legacy: одна planned_service
    legacy = getattr(booking, "planned_service", None)
    if legacy is not None:
        name = (legacy.name or "").strip()
        if name:
            return f"{mark} {name}"
    return ""


def _masters_csv(booking: Booking) -> str:
    seen: set[int] = set()
    names: list[str] = []

    def _add(user_id: int | None, user: User | None) -> None:
        if user_id is None:
            return
        uid = int(user_id)
        if uid in seen:
            return
        seen.add(uid)
        names.append(_master_label(user, uid))

    for bm in booking.masters or []:
        _add(bm.master_id, getattr(bm, "master", None))
    for ps in sorted(
        list(booking.planned_services or []),
        key=lambda x: (int(x.sort_order or 0), int(x.id or 0)),
    ):
        for m in ps.masters or []:
            _add(m.master_id, getattr(m, "master", None))
    return ", ".join(names)


def render_client_notify_template(
    template: str,
    *,
    booking: Booking,
    client: Client,
    tz_name: str,
    marker: str = DEFAULT_SERVICES_MARKER,
) -> str:
    """Подставить переменные before_booking. Неизвестные {{...}} оставляет как есть."""
    text = template if template is not None else ""
    local = _to_local(booking.planned_date, tz_name)
    if local is None:
        date_s = time_s = datetime_s = date_text = weekday = ""
    else:
        date_s = local.strftime("%d.%m.%Y")
        time_s = local.strftime("%H:%M")
        datetime_s = f"{date_s} {time_s}"
        date_text = f"{local.day} {_MONTHS_GENITIVE[local.month]}"
        weekday = _WEEKDAYS[local.weekday()]

    values = {
        "date": date_s,
        "time": time_s,
        "datetime": datetime_s,
        "date_text": date_text,
        "weekday": weekday,
        "name": (client.name or "").strip(),
        "services": _services_block(booking, marker),
        "masters": _masters_csv(booking),
    }

    def _repl(m: re.Match[str]) -> str:
        key = m.group(1)
        if key in values:
            return values[key]
        return m.group(0)

    return _VAR_RE.sub(_repl, text)


def get_services_marker(db: Session) -> str:
    row = db.get(Setting, CLIENT_NOTIFY_SERVICES_MARKER)
    v = (row.value if row else "").strip()
    return v or DEFAULT_SERVICES_MARKER


def set_services_marker(db: Session, marker: str, *, user_id: int | None = None) -> str:
    raw = (marker or "").strip()
    if not raw:
        raise ValueError("Маркер списка услуг не может быть пустым.")
    if len(raw) > 20:
        raise ValueError("Маркер списка услуг слишком длинный (максимум 20 символов).")
    now = utcnow_naive()
    row = db.get(Setting, CLIENT_NOTIFY_SERVICES_MARKER)
    if row is None:
        row = Setting(key=CLIENT_NOTIFY_SERVICES_MARKER, value=raw, updated_at=now, updated_by_user_id=user_id)
        db.add(row)
    else:
        row.value = raw
        row.updated_at = now
        row.updated_by_user_id = user_id
    db.flush()
    return raw


def list_rules(db: Session, kind: ClientNotificationRuleKind | None = None) -> list[ClientNotificationRule]:
    stmt = select(ClientNotificationRule).order_by(
        ClientNotificationRule.kind.asc(),
        ClientNotificationRule.position.asc(),
        ClientNotificationRule.id.asc(),
    )
    if kind is not None:
        stmt = stmt.where(ClientNotificationRule.kind == kind)
    return list(db.scalars(stmt).all())


def ensure_default_rules(db: Session) -> bool:
    """Если правил нет — создать разумные дефолты. Возвращает True, если создали."""
    n = db.scalar(select(ClientNotificationRule.id).limit(1))
    if n is not None:
        return False
    now = utcnow_naive()
    defaults = [
        ClientNotificationRule(
            kind=KIND_BEFORE,
            hours=24,
            template=DEFAULT_BEFORE_TEMPLATE,
            position=0,
            is_enabled=True,
            created_at=now,
        ),
        ClientNotificationRule(
            kind=KIND_BEFORE,
            hours=2,
            template=DEFAULT_BEFORE_TEMPLATE,
            position=1,
            is_enabled=True,
            created_at=now,
        ),
        ClientNotificationRule(
            kind=KIND_AFTER,
            hours=0,
            template=DEFAULT_AFTER_TEMPLATE,
            position=0,
            is_enabled=True,
            created_at=now,
        ),
    ]
    for row in defaults:
        db.add(row)
    if db.get(Setting, CLIENT_NOTIFY_SERVICES_MARKER) is None:
        db.add(
            Setting(
                key=CLIENT_NOTIFY_SERVICES_MARKER,
                value=DEFAULT_SERVICES_MARKER,
                updated_at=now,
            )
        )
    db.flush()
    return True


def parse_rule_hours(raw: Any, *, kind: ClientNotificationRuleKind) -> int:
    s = str(raw if raw is not None else "").strip().replace(",", ".")
    if not s:
        raise ValueError("Укажите число часов.")
    try:
        # целые часы; «2.0» допускаем как 2
        val = float(s)
    except ValueError as e:
        raise ValueError("Часы должны быть целым числом.") from e
    if abs(val - round(val)) > 1e-9:
        raise ValueError("Часы должны быть целым числом.")
    hours = int(round(val))
    if kind == KIND_BEFORE:
        if hours < 1:
            raise ValueError("Для напоминания перед записью укажите целое число часов больше 0.")
    else:
        if hours < 0:
            raise ValueError("Для сообщения после записи укажите целое число часов не меньше 0.")
    if hours > MAX_HOURS:
        raise ValueError(f"Слишком большой интервал (максимум {MAX_HOURS} ч).")
    return hours


def parse_template(raw: Any) -> str:
    text = str(raw if raw is not None else "")
    # сохраняем переносы, но обрезаем крайние пробелы целиком
    if not text.strip():
        raise ValueError("Текст шаблона не может быть пустым.")
    if len(text) > 4000:
        raise ValueError("Шаблон слишком длинный (максимум 4000 символов).")
    return text


def add_rule(
    db: Session,
    *,
    kind: ClientNotificationRuleKind,
    hours: int,
    template: str,
) -> ClientNotificationRule:
    existing = list_rules(db, kind)
    if len(existing) >= MAX_RULES_PER_KIND:
        raise ValueError(f"Не больше {MAX_RULES_PER_KIND} правил этого типа.")
    now = utcnow_naive()
    row = ClientNotificationRule(
        kind=kind,
        hours=int(hours),
        template=template,
        position=len(existing),
        is_enabled=True,
        created_at=now,
        updated_at=now,
    )
    db.add(row)
    db.flush()
    return row


def delete_rule(db: Session, rule_id: int) -> None:
    row = db.get(ClientNotificationRule, int(rule_id))
    if row is None:
        raise ValueError("Правило не найдено.")
    kind = row.kind
    db.delete(row)
    db.flush()
    _renumber(db, kind)


def move_rule(db: Session, rule_id: int, *, direction: str) -> None:
    row = db.get(ClientNotificationRule, int(rule_id))
    if row is None:
        raise ValueError("Правило не найдено.")
    rows = list_rules(db, row.kind)
    idx = next((i for i, r in enumerate(rows) if int(r.id) == int(row.id)), None)
    if idx is None:
        raise ValueError("Правило не найдено.")
    d = (direction or "").strip().lower()
    if d == "up" and idx > 0:
        rows[idx], rows[idx - 1] = rows[idx - 1], rows[idx]
    elif d == "down" and idx < len(rows) - 1:
        rows[idx], rows[idx + 1] = rows[idx + 1], rows[idx]
    else:
        return
    now = utcnow_naive()
    for i, r in enumerate(rows):
        r.position = i
        r.updated_at = now
    db.flush()


def _renumber(db: Session, kind: ClientNotificationRuleKind) -> None:
    rows = list_rules(db, kind)
    now = utcnow_naive()
    for i, r in enumerate(rows):
        r.position = i
        r.updated_at = now
    db.flush()


def save_rules_from_form(
    db: Session,
    *,
    kind: ClientNotificationRuleKind,
    ids: Sequence[Any],
    hours_list: Sequence[Any],
    templates: Sequence[Any],
    enabled_flags: Sequence[Any],
) -> None:
    """Обновить существующие строки kind в порядке формы; лишние id игнорируются."""
    if not (len(ids) == len(hours_list) == len(templates) == len(enabled_flags)):
        raise ValueError("Некорректные данные формы правил.")
    by_id = {int(r.id): r for r in list_rules(db, kind)}
    seen: set[int] = set()
    now = utcnow_naive()
    position = 0
    for raw_id, raw_h, raw_t, raw_en in zip(ids, hours_list, templates, enabled_flags):
        try:
            rid = int(raw_id)
        except (TypeError, ValueError) as e:
            raise ValueError("Некорректный идентификатор правила.") from e
        row = by_id.get(rid)
        if row is None:
            continue
        if rid in seen:
            continue
        seen.add(rid)
        row.hours = parse_rule_hours(raw_h, kind=kind)
        row.template = parse_template(raw_t)
        row.is_enabled = str(raw_en).strip().lower() in ("1", "true", "on", "yes")
        row.position = position
        row.updated_at = now
        position += 1
    # строки, не пришедшие в форме, не удаляем здесь (удаление отдельным action)
    db.flush()


def rules_for_ui(db: Session) -> dict[str, list[dict[str, Any]]]:
    before = list_rules(db, KIND_BEFORE)
    after = list_rules(db, KIND_AFTER)
    return {
        "before": [
            {
                "id": int(r.id),
                "hours": int(r.hours),
                "template": r.template,
                "is_enabled": bool(r.is_enabled),
                "position": int(r.position),
            }
            for r in before
        ],
        "after": [
            {
                "id": int(r.id),
                "hours": int(r.hours),
                "template": r.template,
                "is_enabled": bool(r.is_enabled),
                "position": int(r.position),
            }
            for r in after
        ],
    }


def load_booking_for_template(db: Session, booking_id: int) -> Booking | None:
    """Удобная загрузка связей для render (на будущее / тесты)."""
    return db.scalar(
        select(Booking)
        .where(Booking.id == int(booking_id))
        .options(
            selectinload(Booking.masters).selectinload(BookingMaster.master),
            selectinload(Booking.planned_services).selectinload(BookingPlannedService.service),
            selectinload(Booking.planned_services)
            .selectinload(BookingPlannedService.masters)
            .selectinload(BookingPlannedServiceMaster.master),
        )
        .limit(1)
    )
