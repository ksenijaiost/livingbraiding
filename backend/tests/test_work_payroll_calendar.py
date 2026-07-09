"""ЗП по работам в календаре: журнал + fallback из карточки работы."""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db import models as _orm_models  # noqa: F401
from app.db.base import Base
from app.db.models import (
    PayrollFundLedger,
    PayrollFundSide,
    PayrollFundSourceKind,
    PayrollPeriod,
    User,
    UserRole,
    UserRoleAssignment,
    WorkForInventory,
    WorkForInventoryStaff,
    WorkKind,
    WorkScope,
)
from app.payroll_fund import (
    backfill_work_accruals_if_missing,
    sum_work_master_payroll_by_work_id,
    sum_work_studio_payroll_by_work_id,
)


@pytest.fixture()
def memory_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as db:
        yield db


def _seed_master(db) -> User:
    master = User(username="ira", password_hash="x", display_name="Ира", role=UserRole.MASTER, is_active=True)
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
    db.commit()
    return master


def test_sum_work_payroll_fallback_and_backfill(memory_db) -> None:
    db = memory_db
    master = _seed_master(db)
    work = WorkForInventory(
        created_by_user_id=master.id,
        performed_date=datetime(2026, 7, 1, 12, 0),
        kind=WorkKind.OTHER,
        scope=WorkScope.CUSTOM_ORDER,
        master_profit_amount=500.0,
        studio_profit_amount=500.0,
        profit_total_amount=1000.0,
    )
    db.add(work)
    db.flush()
    db.add(
        WorkForInventoryStaff(
            work_id=work.id,
            user_id=master.id,
            share=1.0,
            master_profit_amount=500.0,
        )
    )
    db.commit()

    assert not db.scalars(
        select(PayrollFundLedger).where(
            PayrollFundLedger.source_kind == PayrollFundSourceKind.WORK,
            PayrollFundLedger.source_id == work.id,
        )
    ).all()

    master_pay = sum_work_master_payroll_by_work_id(db, work_ids=[work.id], user_id=master.id)
    studio_pay = sum_work_studio_payroll_by_work_id(db, work_ids=[work.id])
    assert master_pay[work.id] == 500.0
    assert studio_pay[work.id] == 500.0

    backfill_work_accruals_if_missing(db, work)
    db.commit()

    rows = list(
        db.scalars(
            select(PayrollFundLedger).where(
                PayrollFundLedger.source_kind == PayrollFundSourceKind.WORK,
                PayrollFundLedger.source_id == work.id,
            )
        ).all()
    )
    master_rows = [r for r in rows if r.side == PayrollFundSide.MASTER]
    studio_rows = [r for r in rows if r.side == PayrollFundSide.STUDIO]
    assert len(master_rows) == 1
    assert float(master_rows[0].amount) == 500.0
    assert len(studio_rows) == 1
    assert float(studio_rows[0].amount) == 500.0
