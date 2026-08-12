"""Человекочитаемые подписи полей аудита брони (совместимость)."""

from __future__ import annotations

from app.audit import FieldChange
from app.audit_field_labels import (
    apply_audit_field_labels as apply_booking_audit_field_labels,
    audit_field_label as booking_audit_field_label,
    diff_planned_service_masters_audit,
    planned_service_masters_audit_field_label,
)

# Историческое имя — те же подписи, что и для всех аудитов.
BOOKING_AUDIT_FIELD_LABELS = {}  # deprecated; используйте audit_field_labels.AUDIT_FIELD_LABELS

__all__ = [
    "BOOKING_AUDIT_FIELD_LABELS",
    "apply_booking_audit_field_labels",
    "booking_audit_field_label",
    "diff_planned_service_masters_audit",
    "planned_service_masters_audit_field_label",
]
