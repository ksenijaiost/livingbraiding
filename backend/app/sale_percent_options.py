"""Доступные проценты с продажи: список в work_rates, по умолчанию 10 и 15."""

from __future__ import annotations

import json
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import diff_fields, write_audit_rows
from app.db.models import WorkRate, WorkRateAuditLog
from app.forms_parse import parse_float
from app.time_utils import utcnow_naive
from app.work_rate_keys import SALE_PERCENT_OPTIONS

DEFAULT_SALE_PERCENTS: tuple[int, ...] = (10, 15)
MIN_SALE_PERCENT = 1
MAX_SALE_PERCENT = 100


def list_sale_percents(db: Session) -> list[int]:
    """Сохранённый список. Если строки ещё нет — 10 и 15."""
    row = db.scalar(select(WorkRate).where(WorkRate.key == SALE_PERCENT_OPTIONS, WorkRate.is_active.is_(True)))
    if row is None:
        return list(DEFAULT_SALE_PERCENTS)
    return _parse_stored(row.value_json)


def sale_percent_choices(db: Session, current: int | None = None) -> list[int]:
    """Варианты для формы. Уже сохранённый на продаже процент остаётся в списке."""
    opts = list_sale_percents(db)
    if current is None:
        return opts
    try:
        n = int(current)
    except (TypeError, ValueError):
        return opts
    if n < MIN_SALE_PERCENT or n > MAX_SALE_PERCENT or n in opts:
        return opts
    return sorted([*opts, n])


def stored_sale_percent(sale: object) -> int | None:
    """Процент, записанный на продаже. Настройки списка на него не влияют."""
    raw = getattr(sale, "sale_percent", None)
    if raw is None:
        return None
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return None
    if n < MIN_SALE_PERCENT or n > MAX_SALE_PERCENT:
        return None
    return n


def parse_sale_percent_input(raw: str | None, allowed: list[int]) -> int:
    s = (raw or "").strip()
    if not allowed:
        raise ValueError("Нет доступных процентов с продажи. Добавьте их в настройках.")
    labels = ", ".join(f"{p}%" for p in allowed)
    if not s:
        raise ValueError(f"Выберите процент с продажи: {labels}.")
    try:
        pct = _whole_percent(s)
    except ValueError as exc:
        raise ValueError(f"Выберите процент с продажи: {labels}.") from exc
    if pct not in allowed:
        raise ValueError(f"Выберите процент с продажи: {labels}.")
    return pct


def apply_sale_percent_save(current: list[int], *, original: int | None, new_value: int) -> list[int]:
    if original is None:
        if new_value in current:
            raise ValueError("Такой процент уже есть.")
        return sorted([*current, new_value])
    if original not in current:
        raise ValueError("Процент не найден.")
    if new_value != original and new_value in current:
        raise ValueError("Такой процент уже есть.")
    return sorted(new_value if x == original else x for x in current)


def apply_sale_percent_delete(current: list[int], percent: int) -> list[int]:
    if percent not in current:
        raise ValueError("Процент не найден.")
    if len(current) <= 1:
        raise ValueError("Нельзя удалить последний процент.")
    return [x for x in current if x != percent]


def parse_settings_percent(raw: object | None) -> int:
    s = str(raw or "").strip()
    if not s:
        raise ValueError("Укажите процент.")
    return _whole_percent(s)


def store_sale_percents(db: Session, percents: list[int], *, user_id: int) -> None:
    payload = json.dumps(sorted(percents), ensure_ascii=False)
    now = utcnow_naive()
    row = db.scalar(select(WorkRate).where(WorkRate.key == SALE_PERCENT_OPTIONS))
    if row is None:
        row = WorkRate(
            key=SALE_PERCENT_OPTIONS,
            value_json=payload,
            is_active=True,
            updated_at=now,
            updated_by_user_id=user_id,
        )
        db.add(row)
        db.flush()
        before = SimpleNamespace(value_json=None, is_active=None)
    else:
        if row.value_json == payload and row.is_active:
            return
        before = SimpleNamespace(value_json=row.value_json, is_active=row.is_active)
        row.value_json = payload
        row.is_active = True
        row.updated_at = now
        row.updated_by_user_id = user_id
    write_audit_rows(
        db,
        log_model=WorkRateAuditLog,
        entity_field="work_rate_id",
        entity_id=row.id,
        changed_by_user_id=user_id,
        changes=diff_fields(before, row, ("value_json", "is_active")),
    )


def _whole_percent(raw: str) -> int:
    val = parse_float(raw, field_name="percent")
    if abs(val - round(val)) > 1e-6:
        raise ValueError("Процент должен быть целым числом от 1 до 100.")
    pct = int(round(val))
    if pct < MIN_SALE_PERCENT or pct > MAX_SALE_PERCENT:
        raise ValueError("Процент должен быть целым числом от 1 до 100.")
    return pct


def _parse_stored(value_json: str) -> list[int]:
    try:
        data = json.loads(value_json)
    except (TypeError, ValueError):
        return list(DEFAULT_SALE_PERCENTS)
    if not isinstance(data, list):
        return list(DEFAULT_SALE_PERCENTS)
    out: list[int] = []
    for item in data:
        try:
            n = int(item)
        except (TypeError, ValueError):
            continue
        if MIN_SALE_PERCENT <= n <= MAX_SALE_PERCENT and n not in out:
            out.append(n)
    return sorted(out)
