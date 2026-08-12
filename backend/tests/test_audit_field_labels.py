"""Общие подписи полей аудита."""

from __future__ import annotations

from app.audit import FieldChange
from app.audit_field_labels import apply_audit_field_labels, audit_field_is_json


def test_apply_audit_field_labels_visit_fields() -> None:
    out = apply_audit_field_labels([FieldChange("amount_from_client", "1000", "1200")])
    assert out[0].field_name == "Сумма от клиента"


def test_apply_audit_field_labels_kit_fields() -> None:
    out = apply_audit_field_labels([FieldChange("discount_percent", "10", "15")])
    assert out[0].field_name == "Скидка (%)"


def test_audit_field_is_json() -> None:
    assert audit_field_is_json("details_json")
    assert audit_field_is_json("Параметры (JSON)")
    assert not audit_field_is_json("Статус")
