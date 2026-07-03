"""Способ оплаты от клиента (нал / безнал) при закрытии визита, продажи, работы."""

from __future__ import annotations

from app.db.models import ClientPaymentKind


def parse_client_payment_kind(raw: str | None) -> ClientPaymentKind:
    v = (raw or "").strip().upper()
    if v == ClientPaymentKind.NON_CASH.value:
        return ClientPaymentKind.NON_CASH
    return ClientPaymentKind.CASH


def client_payment_kind_label(kind: ClientPaymentKind | object | str | None) -> str:
    if kind is None:
        return "—"
    if isinstance(kind, ClientPaymentKind):
        return "Безнал" if kind == ClientPaymentKind.NON_CASH else "Нал"
    val = getattr(kind, "value", kind)
    if str(val).upper() == ClientPaymentKind.NON_CASH.value:
        return "Безнал"
    return "Нал"
