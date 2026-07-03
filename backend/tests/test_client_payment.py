"""Способ оплаты с клиента (Fix 100)."""

from __future__ import annotations

from app.client_payment import client_payment_kind_label, parse_client_payment_kind
from app.db.models import ClientPaymentKind


def test_parse_client_payment_kind() -> None:
    assert parse_client_payment_kind("CASH") == ClientPaymentKind.CASH
    assert parse_client_payment_kind("NON_CASH") == ClientPaymentKind.NON_CASH
    assert parse_client_payment_kind("") == ClientPaymentKind.CASH
    assert client_payment_kind_label(ClientPaymentKind.CASH) == "Нал"
    assert client_payment_kind_label(ClientPaymentKind.NON_CASH) == "Безнал"
