from __future__ import annotations

from datetime import date

from app.client_retention import (
    compute_retention_slice,
    default_retention_periods,
    is_new_in_visit_period,
)


def test_default_periods_prev_month_and_current() -> None:
    today = date(2026, 9, 29)
    vf, vt, rf, rt = default_retention_periods(today=today)
    assert vf == date(2026, 8, 1)
    assert vt == date(2026, 8, 31)
    assert rf == date(2026, 9, 1)
    assert rt == today


def test_default_periods_january() -> None:
    today = date(2026, 1, 15)
    vf, vt, rf, rt = default_retention_periods(today=today)
    assert vf == date(2025, 12, 1)
    assert vt == date(2025, 12, 31)
    assert rf == date(2026, 1, 1)
    assert rt == today


def test_is_new_in_visit_period() -> None:
    assert is_new_in_visit_period(date(2022, 12, 10), visit_from=date(2022, 12, 1), visit_to=date(2022, 12, 31))
    assert not is_new_in_visit_period(date(2022, 11, 30), visit_from=date(2022, 12, 1), visit_to=date(2022, 12, 31))
    assert not is_new_in_visit_period(None, visit_from=date(2022, 12, 1), visit_to=date(2022, 12, 31))


def test_compute_retention_slice_company_example() -> None:
    # 45 visited, 13 new; 19 returned (2 of them new) → 26 lost (11 new)
    visited = set(range(1, 46))
    new = set(range(1, 14))  # 1..13
    # returned: 2 new (1,2) + 17 old (14..30)
    returned = {1, 2} | set(range(14, 31))
    slice_, lost_t, lost_n = compute_retention_slice(
        visited_ids=visited,
        new_ids=new,
        returned_ids=returned,
    )
    assert slice_.visited_total == 45
    assert slice_.visited_new == 13
    assert slice_.returned_total == 19
    assert slice_.returned_new == 2
    assert slice_.lost_total == 26
    assert slice_.lost_new == 11
    assert abs(slice_.returned_total_pct - 42.2) < 0.2
    assert len(lost_t) == 26
    assert len(lost_n) == 11
    assert set(lost_n).issubset(set(lost_t))


def test_compute_empty() -> None:
    slice_, lost_t, lost_n = compute_retention_slice(visited_ids=[], new_ids=[], returned_ids=[])
    assert slice_.visited_total == 0
    assert slice_.returned_total_pct == 0.0
    assert lost_t == ()
    assert lost_n == ()
