"""Сводка на главной: ЗП и остаток фонда за текущий период."""

from __future__ import annotations

from datetime import date, datetime, time

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import models as _orm_models  # noqa: F401
from app.db.base import Base
from app.db.models import (
    PayrollFundEntryKind,
    PayrollFundPayoutPaymentKind,
    PayrollFundSide,
    PayrollFundSourceKind,
    PayrollPeriod,
    User,
    UserRole,
)
from app.payroll_fund import (
    append_ledger,
    build_home_payroll_period_ctx,
    post_payout,
)
from app.payroll_utils import payroll_period_day_end, payroll_period_day_start


@pytest.fixture()
def memory_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as db:
        yield db


def _seed_user(db, username: str = "m1") -> User:
    u = User(
        username=username,
        password_hash="x",
        display_name=username,
        role=UserRole.MASTER,
        is_active=True,
    )
    db.add(u)
    db.flush()
    return u


def test_build_home_payroll_period_ctx_personal_with_payout(memory_db) -> None:
    db = memory_db
    master = _seed_user(db)
    today = date(2026, 7, 20)
    db.add(
        PayrollPeriod(
            date_from=payroll_period_day_start(date(2026, 7, 1)),
            date_to=payroll_period_day_start(date(2026, 7, 1)),
            closed_at=None,
        )
    )
    db.flush()
    append_ledger(
        db,
        entry_kind=PayrollFundEntryKind.ACCRUAL,
        side=PayrollFundSide.MASTER,
        user_id=master.id,
        amount=10000.0,
        source_kind=PayrollFundSourceKind.VISIT,
        source_id=1,
        created_by_user_id=master.id,
    )
    post_payout(
        db,
        side=PayrollFundSide.MASTER,
        user_id=master.id,
        amount=3000.0,
        created_by_user_id=master.id,
        comment="аванс",
        payout_payment_kind=PayrollFundPayoutPaymentKind.CASH,
    )
    db.commit()

    ctx = build_home_payroll_period_ctx(db, today=today, user_id=master.id, include_studio=False)
    assert ctx is not None
    assert ctx["date_from"].date() == date(2026, 7, 1)
    assert ctx["personal_accrued"] == 10000.0
    assert ctx["personal_paid"] == 3000.0
    assert ctx["personal_balance"] == 7000.0
    assert "studio_accrued" not in ctx


def test_build_home_payroll_period_ctx_studio(memory_db) -> None:
    db = memory_db
    admin = _seed_user(db, "sa")
    admin.role = UserRole.ADMIN_SUPER
    today = date(2026, 7, 20)
    db.add(
        PayrollPeriod(
            date_from=payroll_period_day_start(date(2026, 7, 10)),
            date_to=payroll_period_day_end(date(2026, 7, 10)),
            closed_at=None,
        )
    )
    db.flush()
    append_ledger(
        db,
        entry_kind=PayrollFundEntryKind.ACCRUAL,
        side=PayrollFundSide.STUDIO,
        user_id=None,
        amount=5000.0,
        source_kind=PayrollFundSourceKind.VISIT,
        source_id=2,
        created_by_user_id=admin.id,
    )
    post_payout(
        db,
        side=PayrollFundSide.STUDIO,
        user_id=admin.id,
        amount=1500.0,
        created_by_user_id=admin.id,
        comment="выплата из студии",
    )
    db.commit()

    ctx = build_home_payroll_period_ctx(db, today=today, user_id=admin.id, include_studio=True)
    assert ctx is not None
    assert ctx["studio_accrued"] == 5000.0
    assert ctx["studio_paid"] == 1500.0
    assert ctx["studio_balance"] == 3500.0
