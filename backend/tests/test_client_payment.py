"""Способ оплаты с клиента (Fix 100)."""

from __future__ import annotations

from app.client_payment import (
    client_payment_kind_label,
    format_client_payment_kinds,
    parse_client_payment_kind,
)
from app.db.models import ClientPaymentKind


def test_parse_client_payment_kind() -> None:
    assert parse_client_payment_kind("CASH") == ClientPaymentKind.CASH
    assert parse_client_payment_kind("NON_CASH") == ClientPaymentKind.NON_CASH
    assert parse_client_payment_kind("") == ClientPaymentKind.CASH
    assert client_payment_kind_label(ClientPaymentKind.CASH) == "Нал"
    assert client_payment_kind_label(ClientPaymentKind.NON_CASH) == "Безнал"


def test_format_client_payment_kinds() -> None:
    assert format_client_payment_kinds([ClientPaymentKind.CASH]) == "нал"
    assert format_client_payment_kinds([ClientPaymentKind.NON_CASH]) == "безнал"
    assert format_client_payment_kinds(
        [ClientPaymentKind.CASH, ClientPaymentKind.NON_CASH]
    ) == "нал, безнал"
    assert format_client_payment_kinds([]) == "—"
