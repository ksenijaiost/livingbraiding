from __future__ import annotations


def money_q2(x: float) -> float:
    """Округление суммы до копеек (2 знака)."""
    return round(float(x), 2)


def money_or_zero(x: float | None) -> float:
    """Сумма для показа: `None` → 0.0, значения меньше 0.0005 по модулю → 0.0 (убирает «-0.0»)."""
    if x is None:
        return 0.0
    v = float(x)
    return 0.0 if abs(v) < 0.0005 else v
