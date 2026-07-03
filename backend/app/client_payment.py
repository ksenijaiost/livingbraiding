"""Способ оплаты от клиента (нал / безнал) при закрытии визита, продажи, работы."""

from __future__ import annotations

from collections.abc import Iterable

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


def format_client_payment_kinds(kinds: Iterable[ClientPaymentKind | str | None]) -> str:
    """Краткая сводка способов оплаты: «нал», «безнал» или «нал, безнал»."""
    has_cash = False
    has_non_cash = False
    for raw in kinds:
        if isinstance(raw, ClientPaymentKind):
            k = raw
        elif raw is None:
            k = ClientPaymentKind.CASH
        else:
            k = parse_client_payment_kind(str(raw))
        if k == ClientPaymentKind.NON_CASH:
            has_non_cash = True
        else:
            has_cash = True
    parts: list[str] = []
    if has_cash:
        parts.append("нал")
    if has_non_cash:
        parts.append("безнал")
    return ", ".join(parts) if parts else "—"
