"""Статистика по мастеру (Fix 96)."""

from __future__ import annotations

from datetime import date, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import models as _orm_models  # noqa: F401
from app.db.base import Base
from app.db.models import (
    Client,
    MixSource,
    PayrollFundEntryKind,
    PayrollFundSide,
    PayrollFundSourceKind,
    PayrollPeriod,
    Service,
    ServiceCategory,
    ServiceSubcategory,
    User,
    UserRole,
    UserRoleAssignment,
    VisitClientType,
    VisitMastersScope,
)
from app.master_statistics import build_master_statistics, format_discounts
from app.payroll_fund import append_ledger
from app.visit_multi_service import (
    MultiServiceVisitInput,
    VisitHeaderInput,
    VisitServiceLineInput,
    save_visit_with_services,
)


@pytest.fixture()
def memory_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as db:
        yield db


def _seed_master(db) -> tuple[int, int, int]:
    master = User(
        username="m1",
        password_hash="x",
        display_name="Мастер 1",
        role=UserRole.MASTER,
        is_active=True,
    )
    db.add(master)
    db.flush()
    db.add(UserRoleAssignment(user_id=master.id, role=UserRole.MASTER))
    db.add(
        PayrollPeriod(
            date_from=datetime(2020, 1, 1),
            date_to=datetime(2030, 12, 31, 23, 59, 59),
            closed_at=None,
        )
    )
    cat = ServiceCategory(name="Кат", is_active=True)
    db.add(cat)
    db.flush()
    sub = ServiceSubcategory(category_id=cat.id, name="Подкат", is_active=True)
    db.add(sub)
    db.flush()
    svc = Service(subcategory_id=sub.id, name="Услуга", is_active=True)
    db.add(svc)
    db.flush()
    client = Client(name="C", phone="+79990000001", is_confirmed=True)
    db.add(client)
    db.commit()
    return int(master.id), int(svc.id), int(client.id)


def test_format_discounts() -> None:
    assert format_discounts([]) == "0"
    assert format_discounts([0, 0]) == "0"
    assert format_discounts([10]) == "10"
    assert format_discounts([0, 10, 15]) == "10, 15"


def test_build_master_statistics_visit_and_fund(memory_db) -> None:
    db = memory_db
    master_id, svc_id, client_id = _seed_master(db)
    performed = date.today()
    save_visit_with_services(
        db,
        master_id,
        MultiServiceVisitInput(
            header=VisitHeaderInput(
                client_mode="existing",
                existing_client_id=client_id,
                draft_name="",
                draft_phone="",
                draft_telegram="",
                draft_vk="",
                draft_instagram="",
                draft_other_contact="",
                client_type=VisitClientType.RETURNING,
                performed_date=performed,
                duration_minutes=60,
                masters_scope=VisitMastersScope.VISIT,
                same_master_shares_all_services=False,
                visit_master_allocations=[(master_id, 100)],
            ),
            lines=[
                VisitServiceLineInput(
                    service_id=svc_id,
                    client_discount_percent=5,
                    amount_from_client=5000,
                    kanekalon_grams=0,
                    kudri_grams=0,
                    mix_source=MixSource.NO_MIX,
                    mix_complexity=None,
                    mix_bonus_master_id=None,
                    amortization_level=None,
                    kit_kind="STOCK",
                )
            ],
        ),
    )
    db.commit()

    append_ledger(
        db,
        entry_kind=PayrollFundEntryKind.ACCRUAL,
        side=PayrollFundSide.MASTER,
        user_id=master_id,
        amount=100.0,
        source_kind=PayrollFundSourceKind.MANUAL,
        source_id=None,
        created_by_user_id=master_id,
    )
    append_ledger(
        db,
        entry_kind=PayrollFundEntryKind.PAYOUT,
        side=PayrollFundSide.MASTER,
        user_id=master_id,
        amount=-50.0,
        source_kind=PayrollFundSourceKind.MANUAL,
        source_id=None,
        created_by_user_id=master_id,
    )
    db.commit()

    d0 = date.today().replace(day=1)
    d1 = date.today()
    stats = build_master_statistics(db, master_id, d0, d1)
    assert stats is not None
    assert stats.master_name == "Мастер 1"
    assert len(stats.visits) == 1
    assert stats.visits[0].amount_from_client == 5000.0
    assert stats.visits[0].payment_display == "нал"
    assert stats.visits[0].discount_display == "5"
    assert stats.visits[0].master_payroll > 0
    assert stats.visits[0].masters_pay_total == stats.visits[0].master_payroll
    assert stats.payroll_accrued >= 100.0
    assert stats.payouts_total == 50.0
    assert stats.fund_balance_end == stats.fund_balance_start + stats.payroll_accrued - stats.payouts_total
    assert stats.hourly_works == []


def test_build_master_statistics_hourly_work(memory_db) -> None:
    db = memory_db
    master_id, _svc_id, _client_id = _seed_master(db)
    from app.db.models import HourlyWorkEntry

    entry = HourlyWorkEntry(
        performed_date=datetime.combine(date.today(), datetime.min.time()),
        duration_minutes=90,
        amount=300.0,
        comment="тест",
        master_user_id=master_id,
        created_by_user_id=master_id,
    )
    db.add(entry)
    db.commit()
    d0 = date.today().replace(day=1)
    d1 = date.today()
    stats = build_master_statistics(db, master_id, d0, d1)
    assert stats is not None
    assert len(stats.hourly_works) == 1
    assert stats.hourly_works[0].master_payroll == 300.0
    assert stats.hourly_works[0].duration_minutes == 90


def test_visit_statistics_splits_total_and_selected_master_pay(memory_db) -> None:
    db = memory_db
    master_id, svc_id, client_id = _seed_master(db)
    master_b = User(
        username="m2",
        password_hash="x",
        display_name="Мастер 2",
        role=UserRole.MASTER,
        is_active=True,
    )
    db.add(master_b)
    db.flush()
    db.add(UserRoleAssignment(user_id=master_b.id, role=UserRole.MASTER))
    performed = date.today()

    save_visit_with_services(
        db,
        master_id,
        MultiServiceVisitInput(
            header=VisitHeaderInput(
                client_mode="existing",
                existing_client_id=client_id,
                draft_name="",
                draft_phone="",
                draft_telegram="",
                draft_vk="",
                draft_instagram="",
                draft_other_contact="",
                client_type=VisitClientType.RETURNING,
                performed_date=performed,
                duration_minutes=60,
                masters_scope=VisitMastersScope.VISIT,
                same_master_shares_all_services=False,
                visit_master_allocations=[(master_id, 50), (master_b.id, 50)],
            ),
            lines=[
                VisitServiceLineInput(
                    service_id=svc_id,
                    client_discount_percent=0,
                    amount_from_client=10000,
                    kanekalon_grams=0,
                    kudri_grams=0,
                    mix_source=MixSource.NO_MIX,
                    mix_complexity=None,
                    mix_bonus_master_id=None,
                    amortization_level=None,
                    kit_kind="STOCK",
                )
            ],
        ),
    )
    db.commit()

    d0 = date.today().replace(day=1)
    d1 = date.today()
    stats_a = build_master_statistics(db, master_id, d0, d1)
    stats_b = build_master_statistics(db, master_b.id, d0, d1)
    assert stats_a is not None and stats_b is not None
    assert len(stats_a.visits) == 1
    row_a = stats_a.visits[0]
    row_b = stats_b.visits[0]
    assert row_a.masters_pay_total == row_b.masters_pay_total
    assert row_a.masters_pay_total > row_a.master_payroll
    assert row_a.master_payroll == row_b.master_payroll
    assert row_a.masters_pay_total == pytest.approx(row_a.master_payroll + row_b.master_payroll)
