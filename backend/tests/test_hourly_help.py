"""Тесты почасовой помощи в визите (1.7)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.db.models import VisitMastersScope
from app.hourly_help import (
    HourlyHelpRow,
    apply_hourly_help_to_staff_profits,
    apply_hourly_help_to_visit,
    collect_visit_participant_master_ids,
    hourly_help_total,
    parse_hourly_help_from_form,
)


class _FakeForm(dict):
    def keys(self):
        return super().keys()

    def getlist(self, key):
        v = self.get(key)
        if v is None:
            return []
        if isinstance(v, list):
            return v
        return [v]


def test_parse_hourly_help_from_form_skips_empty_rows():
    form = _FakeForm(
        {
            "hourly_help_hours_1": "2",
            "hourly_help_minutes_1": "30",
            "hourly_help_amount_1": "500",
            "hourly_help_hours_2": "",
            "hourly_help_minutes_2": "",
            "hourly_help_amount_2": "",
        }
    )
    rows = parse_hourly_help_from_form(form)
    assert len(rows) == 1
    assert rows[0].master_id == 1
    assert rows[0].hours == 2
    assert rows[0].minutes == 30
    assert rows[0].amount == 500.0


def test_apply_hourly_help_to_visit_scales_service_pools():
    vs1 = SimpleNamespace(is_cancelled=False, masters_pool=600.0)
    vs2 = SimpleNamespace(is_cancelled=False, masters_pool=400.0)
    visit = SimpleNamespace(services=[vs1, vs2], masters_pool=0.0, hourly_help_json=None, hourly_help_total=0.0)
    rows = [HourlyHelpRow(master_id=99, hours=1, minutes=0, amount=200.0)]

    total = apply_hourly_help_to_visit(visit, rows)

    assert total == 200.0
    assert visit.hourly_help_total == 200.0
    assert vs1.masters_pool == 480.0
    assert vs2.masters_pool == 320.0
    assert visit.masters_pool == 800.0


def test_apply_hourly_help_to_visit_rejects_excess_amount():
    visit = SimpleNamespace(
        services=[SimpleNamespace(is_cancelled=False, masters_pool=100.0)],
        masters_pool=0.0,
        hourly_help_json=None,
        hourly_help_total=0.0,
    )
    rows = [HourlyHelpRow(master_id=99, hours=0, minutes=0, amount=150.0)]

    with pytest.raises(ValueError, match="превышает пул"):
        apply_hourly_help_to_visit(visit, rows)


def test_apply_hourly_help_to_visit_rejects_negative_pool_without_help():
    visit = SimpleNamespace(
        services=[SimpleNamespace(is_cancelled=False, masters_pool=-185.0)],
        masters_pool=0.0,
        hourly_help_json=None,
        hourly_help_total=0.0,
    )
    with pytest.raises(ValueError, match="отрицательный"):
        apply_hourly_help_to_visit(visit, [])


def test_apply_hourly_help_to_visit_zero_help_ok_with_positive_pool():
    visit = SimpleNamespace(
        services=[SimpleNamespace(is_cancelled=False, masters_pool=500.0)],
        masters_pool=0.0,
        hourly_help_json=None,
        hourly_help_total=0.0,
    )
    total = apply_hourly_help_to_visit(visit, [])
    assert total == 0.0
    assert visit.masters_pool == 500.0


def test_apply_hourly_help_to_staff_profits_keeps_total():
    staff = {10: 700.0, 20: 300.0}
    rows = [HourlyHelpRow(master_id=99, hours=1, minutes=15, amount=200.0)]

    new_staff, helpers = apply_hourly_help_to_staff_profits(staff, {10, 20}, rows)

    assert helpers == {99: 200.0}
    assert new_staff[10] == 560.0
    assert new_staff[20] == 240.0
    assert sum(new_staff.values()) + sum(helpers.values()) == pytest.approx(1000.0)


def test_collect_visit_participant_master_ids_visit_scope():
    ids = collect_visit_participant_master_ids(
        masters_scope=VisitMastersScope.VISIT,
        visit_master_allocations=[(1, 60), (2, 40)],
        line_master_rows={},
        mix_bonus_master_ids={3},
        correction_master_ids={4},
    )
    assert ids == {1, 2, 3, 4}


def test_hourly_help_total():
    rows = [
        HourlyHelpRow(master_id=1, hours=1, minutes=0, amount=100.0),
        HourlyHelpRow(master_id=2, hours=0, minutes=30, amount=50.0),
    ]
    assert hourly_help_total(rows) == 150.0
