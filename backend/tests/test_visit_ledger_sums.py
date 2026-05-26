"""Суммирование проводок VISIT + VISIT_SERVICE по визиту."""

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
from app.payroll_fund import append_ledger, sum_visit_ledger_by_visit_id
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


def _seed(db):
    master = User(
        username="m",
        password_hash="x",
        display_name="M",
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
    return master, int(svc.id), int(client.id)


def test_sum_visit_ledger_legacy_visit_with_service_line(memory_db) -> None:
    """После миграции: строка visit_services есть, проводки ещё VISIT → сумма не ноль."""
    db = memory_db
    master, svc_id, client_id = _seed(db)
    visit = save_visit_with_services(
        db,
        master.id,
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
                performed_date=date.today(),
                duration_minutes=60,
                masters_scope=VisitMastersScope.VISIT,
                same_master_shares_all_services=False,
                visit_master_allocations=[(master.id, 100)],
            ),
            lines=[
                VisitServiceLineInput(
                    service_id=svc_id,
                    amount_from_client=6000,
                    client_discount_percent=0,
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
    # Имитация старых проводок до VISIT_SERVICE (как у визитов 9–13).
    append_ledger(
        db,
        entry_kind=PayrollFundEntryKind.ACCRUAL,
        side=PayrollFundSide.MASTER,
        user_id=master.id,
        amount=2640,
        source_kind=PayrollFundSourceKind.VISIT,
        source_id=visit.id,
        created_by_user_id=master.id,
    )
    db.commit()

    totals = sum_visit_ledger_by_visit_id(
        db, side=PayrollFundSide.MASTER, visit_ids=[visit.id], user_id=master.id
    )
    # VISIT 2640 + VISIT_SERVICE из save_visit_with_services (если есть)
    assert totals[int(visit.id)] >= 2640.0
