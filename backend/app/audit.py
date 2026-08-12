"""Audit helpers: who changed what, when.

Rule of thumb: store simple string snapshots (old/new) per field.
We intentionally avoid historical recalculation and keep audit logs append-only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Iterable, Type

from sqlalchemy.orm import Session

from app.audit_field_labels import apply_audit_field_labels
from app.display_time import DEFAULT_DISPLAY_TIMEZONE, format_naive_utc_datetime, get_display_timezone
from app.time_utils import utcnow_naive


@dataclass(frozen=True)
class FieldChange:
    field_name: str
    old_value: Any
    new_value: Any


def _to_audit_str(v: Any, *, tz_name: str = DEFAULT_DISPLAY_TIMEZONE) -> str | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return format_naive_utc_datetime(v, tz_name) or None
    if isinstance(v, date):
        return v.strftime("%d.%m.%Y")
    if isinstance(v, (str, int, float, bool)):
        return str(v)
    return str(v)


def diff_fields(obj_before: Any, obj_after: Any, fields: Iterable[str]) -> list[FieldChange]:
    out: list[FieldChange] = []
    for f in fields:
        a = getattr(obj_before, f, None)
        b = getattr(obj_after, f, None)
        if a != b:
            out.append(FieldChange(field_name=f, old_value=a, new_value=b))
    return out


def write_audit_rows(
    db: Session,
    *,
    log_model: Type[Any],
    entity_field: str,
    entity_id: int | str,
    changed_by_user_id: int | None,
    changes: list[FieldChange],
    changed_at: datetime | None = None,
) -> None:
    if not changes:
        return
    when = changed_at or utcnow_naive()
    tz = get_display_timezone(db)
    log_table = getattr(log_model, "__tablename__", None)
    labeled = apply_audit_field_labels(changes, log_table=log_table, entity_id=entity_id)
    for ch in labeled:
        db.add(
            log_model(
                **{
                    entity_field: entity_id,
                    "changed_at": when,
                    "changed_by_user_id": changed_by_user_id,
                    "field_name": ch.field_name,
                    "old_value": _to_audit_str(ch.old_value, tz_name=tz),
                    "new_value": _to_audit_str(ch.new_value, tz_name=tz),
                }
            )
        )

