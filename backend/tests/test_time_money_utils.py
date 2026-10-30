"""4.4: общие утилиты времени и денег (бывшие локальные копии в роутах и модулях)."""

from __future__ import annotations

from datetime import datetime, timezone

from app.money_utils import money_or_zero, money_q2
from app.time_utils import local_naive_to_utc_naive, utc_naive_to_local


def test_utc_naive_to_local_none_passthrough() -> None:
    assert utc_naive_to_local(None, "Asia/Yekaterinburg") is None


def test_utc_naive_to_local_shifts_naive_utc() -> None:
    # Екатеринбург UTC+5 без перехода на летнее время
    assert utc_naive_to_local(datetime(2026, 1, 10, 7, 30), "Asia/Yekaterinburg") == datetime(2026, 1, 10, 12, 30)
    assert utc_naive_to_local(datetime(2026, 1, 10, 22, 0), "Asia/Yekaterinburg") == datetime(2026, 1, 11, 3, 0)


def test_utc_naive_to_local_accepts_aware_datetime_and_returns_naive() -> None:
    aware = datetime(2026, 1, 10, 7, 30, tzinfo=timezone.utc)
    out = utc_naive_to_local(aware, "Asia/Yekaterinburg")
    assert out == datetime(2026, 1, 10, 12, 30)
    assert out.tzinfo is None


def test_local_naive_to_utc_naive_roundtrip() -> None:
    local = datetime(2026, 7, 1, 9, 15)
    utc = local_naive_to_utc_naive(local, "Europe/Moscow")
    assert utc == datetime(2026, 7, 1, 6, 15)
    assert utc.tzinfo is None
    assert utc_naive_to_local(utc, "Europe/Moscow") == local


def test_money_q2_rounds_to_kopecks() -> None:
    assert money_q2(10) == 10.0
    assert money_q2(3.14159) == 3.14
    assert money_q2(7.999) == 8.0
    assert money_q2(-2.501) == -2.5
    assert money_q2("7.5") == 7.5  # строки из форм тоже принимаются (float(x))


def test_money_or_zero_none_and_tiny_values() -> None:
    assert money_or_zero(None) == 0.0
    assert money_or_zero(0.0004) == 0.0
    assert money_or_zero(-0.0004) == 0.0
    assert str(money_or_zero(-0.0004)) == "0.0"
    assert money_or_zero(0.0005) == 0.0005
    assert money_or_zero(-150.5) == -150.5
    assert money_or_zero(1200) == 1200.0
