from __future__ import annotations

from datetime import date

from app.client_status import (
    ClientActivity,
    ClientStatus,
    compute_activity_status,
    display_status,
    stats_bucket,
)


def test_never_when_no_activity() -> None:
    assert compute_activity_status(ClientActivity(dates=())) == ClientStatus.NEVER


def test_regular_two_in_last_three_months() -> None:
    today = date(2026, 9, 29)
    act = ClientActivity(dates=(date(2026, 8, 1), date(2026, 9, 10)))
    assert compute_activity_status(act, today=today) == ClientStatus.REGULAR


def test_new_single_recent_activity() -> None:
    today = date(2026, 9, 29)
    act = ClientActivity(dates=(date(2026, 9, 1),))
    assert compute_activity_status(act, today=today) == ClientStatus.NEW


def test_sleeping_and_lost() -> None:
    today = date(2026, 9, 29)
    sleeping = ClientActivity(dates=(date(2026, 5, 1),))
    lost = ClientActivity(dates=(date(2026, 1, 1),))
    assert compute_activity_status(sleeping, today=today) == ClientStatus.SLEEPING
    assert compute_activity_status(lost, today=today) == ClientStatus.LOST


def test_blocked_overrides_activity() -> None:
    act = ClientActivity(dates=(date(2026, 9, 1), date(2026, 9, 10)))
    assert display_status(is_blacklisted=True, activity=act, today=date(2026, 9, 29)) == ClientStatus.BLOCKED
    assert stats_bucket(is_blacklisted=True, activity=act) == ClientStatus.BLOCKED


def test_recent_not_in_stats_cards() -> None:
    today = date(2026, 9, 29)
    # Были раньше и ещё одна недавно — не постоянный и не новый
    act = ClientActivity(dates=(date(2025, 1, 1), date(2026, 9, 1)))
    assert compute_activity_status(act, today=today) == ClientStatus.RECENT
    assert stats_bucket(is_blacklisted=False, activity=act, today=today) is None
