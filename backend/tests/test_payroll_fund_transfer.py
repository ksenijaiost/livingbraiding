"""1.93: перевод из фонда студии в личный фонд сотрудника."""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db import models as _orm_models  # noqa: F401
from app.db.base import Base
from app.db.models import (
    Client,
    PayrollFundEntryKind,
    PayrollFundLedger,
    PayrollFundSide,
    User,
    UserRole,
)
from app.payroll_fund import (
    employee_fund_balance,
    employee_payroll_net_in_period,
    post_studio_to_employee_transfer,
    storno_manual_ledger_entry,
    studio_fund_balance,
)


@pytest.fixture()
def memory_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as db:
        yield db


def _seed_user(db):
    u = User(username="m", password_hash="x", display_name="M", role=UserRole.MASTER, is_active=True)
    db.add(u)
    db.add(Client(name="C", phone="+79990001111", is_confirmed=True))
    db.commit()
    db.refresh(u)
    return u


def test_post_studio_to_employee_transfer_pair_and_balances(memory_db) -> None:
    db = memory_db
    u = _seed_user(db)
    event = datetime(2026, 8, 15)
    studio_row, master_row = post_studio_to_employee_transfer(
        db,
        user_id=u.id,
        amount=2000.0,
        created_by_user_id=u.id,
        comment="Доплата",
        effective_at=event,
    )
    db.commit()

    assert studio_row.entry_kind == PayrollFundEntryKind.TRANSFER
    assert studio_row.side == PayrollFundSide.STUDIO
    assert studio_row.amount == -2000.0
    assert studio_row.user_id == u.id
    assert studio_row.source_id == master_row.id
    assert studio_row.effective_at == event

    assert master_row.entry_kind == PayrollFundEntryKind.TRANSFER
    assert master_row.side == PayrollFundSide.MASTER
    assert master_row.amount == 2000.0
    assert master_row.user_id == u.id
    assert master_row.source_id == studio_row.id
    assert master_row.comment == "Доплата"

    assert studio_fund_balance(db) == -2000.0
    assert employee_fund_balance(db, u.id) == 2000.0
    assert employee_payroll_net_in_period(
        db, u.id, datetime(2026, 8, 1), datetime(2026, 9, 1)
    ) == 2000.0


def test_post_transfer_rejects_non_positive(memory_db) -> None:
    db = memory_db
    u = _seed_user(db)
    with pytest.raises(ValueError):
        post_studio_to_employee_transfer(
            db,
            user_id=u.id,
            amount=0,
            created_by_user_id=u.id,
            comment=None,
        )
    with pytest.raises(ValueError):
        post_studio_to_employee_transfer(
            db,
            user_id=u.id,
            amount=-100,
            created_by_user_id=u.id,
            comment=None,
        )


def test_storno_transfer_cancels_both_legs(memory_db) -> None:
    db = memory_db
    u = _seed_user(db)
    studio_row, master_row = post_studio_to_employee_transfer(
        db,
        user_id=u.id,
        amount=1500.0,
        created_by_user_id=u.id,
        comment="Оклад",
        effective_at=datetime(2026, 8, 20),
    )
    db.commit()

    storno_manual_ledger_entry(db, int(master_row.id), u.id)
    db.commit()

    stornos = list(
        db.scalars(
            select(PayrollFundLedger).where(PayrollFundLedger.entry_kind == PayrollFundEntryKind.STORNO)
        ).all()
    )
    assert len(stornos) == 2
    storno_of = {int(s.storno_of_id) for s in stornos}
    assert storno_of == {int(studio_row.id), int(master_row.id)}
    assert studio_fund_balance(db) == 0.0
    assert employee_fund_balance(db, u.id) == 0.0
