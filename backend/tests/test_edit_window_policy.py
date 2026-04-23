from __future__ import annotations

from datetime import datetime, timedelta


class _SettingRow:
    def __init__(self, value: str | None):
        self.value = value


class _FakeDB:
    def __init__(self, value: str | None):
        self._row = _SettingRow(value) if value is not None else None

    def get(self, model, key):  # noqa: ANN001
        return self._row


class _Visit:
    def __init__(self, created_at: datetime):
        self.created_at = created_at


def test_edit_window_days_default_and_invalid() -> None:
    from app.visit_edit_policy import edit_window_days

    assert edit_window_days(_FakeDB(None)) == 2
    assert edit_window_days(_FakeDB("")) == 2
    assert edit_window_days(_FakeDB("nope")) == 2


def test_edit_window_days_parses_and_clamps_to_zero() -> None:
    from app.visit_edit_policy import edit_window_days

    assert edit_window_days(_FakeDB("5")) == 5
    assert edit_window_days(_FakeDB("-10")) == 0


def test_within_edit_window_boundary_inclusive() -> None:
    from app.visit_edit_policy import within_edit_window

    created = datetime(2026, 4, 1, 12, 0, 0)
    v = _Visit(created_at=created)
    days = 2

    # inclusive boundary: created_at + days >= now
    assert within_edit_window(v, days, now=created + timedelta(days=2)) is True
    assert within_edit_window(v, days, now=created + timedelta(days=2, seconds=1)) is False


def test_within_edit_window_days_zero_is_false() -> None:
    from app.visit_edit_policy import within_edit_window

    v = _Visit(created_at=datetime(2026, 4, 1, 12, 0, 0))
    assert within_edit_window(v, 0, now=v.created_at) is False
