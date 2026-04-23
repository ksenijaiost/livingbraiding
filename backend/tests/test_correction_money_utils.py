from __future__ import annotations


def test_corr_wash_catalog_name_without_correction() -> None:
    from app.work_products_compute import CORR_SVC_WASH_WITHOUT, corr_wash_catalog_name

    assert corr_wash_catalog_name(trim_qty=0, hourly_hours=0.0, hourly_avg=False) == CORR_SVC_WASH_WITHOUT


def test_corr_wash_catalog_name_with_correction_flags() -> None:
    from app.work_products_compute import CORR_SVC_WASH_WITH, corr_wash_catalog_name

    assert corr_wash_catalog_name(trim_qty=1, hourly_hours=0.0, hourly_avg=False) == CORR_SVC_WASH_WITH
    assert corr_wash_catalog_name(trim_qty=0, hourly_hours=1.0, hourly_avg=False) == CORR_SVC_WASH_WITH
    assert corr_wash_catalog_name(trim_qty=0, hourly_hours=0.0, hourly_avg=True) == CORR_SVC_WASH_WITH


def test_corr_hourly_pay_units_avg_default() -> None:
    from app.work_products_compute import corr_hourly_pay_units

    assert corr_hourly_pay_units(hourly_hours=0.0, hourly_avg=True) == 2.5
    assert corr_hourly_pay_units(hourly_hours=-10.0, hourly_avg=True) == 2.5


def test_corr_hourly_pay_units_prefers_explicit_hours() -> None:
    from app.work_products_compute import corr_hourly_pay_units

    assert corr_hourly_pay_units(hourly_hours=1.25, hourly_avg=True) == 1.25
    assert corr_hourly_pay_units(hourly_hours=1.25, hourly_avg=False) == 1.25
