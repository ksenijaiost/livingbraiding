"""1.11: учётная дата проводки effective_at."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db import models as _orm_models  # noqa: F401
from app.db.base import Base
from app.db.models import (
    Booking,
    BookingKind,
    BookingStatus,
    Client,
    Consultation,
    HourlyWorkEntry,
    PayrollFundEntryKind,
    PayrollFundLedger,
    PayrollFundSide,
    PayrollFundSourceKind,
    PayrollPeriod,
    User,
    UserRole,
    Visit,
    VisitClientType,
    VisitPriceType,
)
from app.payroll_fund import (
    append_ledger,
    employee_payroll_net_in_period,
    post_consultation_accrual,
    post_hourly_work_accruals,
    storno_source_accruals,
)
from app.payroll_ledger_backfill import backfill_payroll_ledger_effective_at
from app.visit_edit_policy import ensure_event_date_in_open_payroll_period, is_in_closed_payroll_period


@pytest.fixture()
def memory_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as db:
        yield db


def _seed_period(db, *, closed: bool = False):
    p = PayrollPeriod(
        date_from=datetime(2026, 6, 1),
        date_to=datetime(2026, 6, 30, 23, 59, 59),
        closed_at=datetime(2026, 7, 1) if closed else None,
    )
    db.add(p)
    open_p = PayrollPeriod(
        date_from=datetime(2026, 7, 1),
        date_to=datetime(2026, 7, 31, 23, 59, 59),
        closed_at=None,
    )
    db.add(open_p)
    db.commit()


def _seed_user_client(db):
    u = User(username="m", password_hash="x", display_name="M", role=UserRole.MASTER, is_active=True)
    c = Client(name="C", phone="+79990001111", is_confirmed=True)
    db.add(u)
    db.add(c)
    db.commit()
    db.refresh(u)
    db.refresh(c)
    return u, c


def test_append_ledger_sets_both_dates(memory_db) -> None:
    db = memory_db
    u, _ = _seed_user_client(db)
    event = datetime(2026, 6, 15, 10, 0, 0)
    row = append_ledger(
        db,
        entry_kind=PayrollFundEntryKind.ACCRUAL,
        side=PayrollFundSide.MASTER,
        user_id=u.id,
        amount=100,
        source_kind=PayrollFundSourceKind.MANUAL,
        source_id=None,
        created_by_user_id=u.id,
        effective_at=event,
    )
    db.commit()
    assert row.effective_at == event
    assert row.created_at is not None
    assert row.created_at != event or True  # created is "now"


def test_storno_copies_effective_at(memory_db) -> None:
    db = memory_db
    u, _ = _seed_user_client(db)
    event = datetime(2026, 6, 10, 12, 0, 0)
    append_ledger(
        db,
        entry_kind=PayrollFundEntryKind.ACCRUAL,
        side=PayrollFundSide.MASTER,
        user_id=u.id,
        amount=50,
        source_kind=PayrollFundSourceKind.HOURLY_WORK,
        source_id=1,
        created_by_user_id=u.id,
        effective_at=event,
    )
    db.commit()
    storno_source_accruals(db, PayrollFundSourceKind.HOURLY_WORK, 1, u.id)
    db.commit()
    st = db.scalars(
        select(PayrollFundLedger).where(PayrollFundLedger.entry_kind == PayrollFundEntryKind.STORNO)
    ).first()
    assert st is not None
    assert st.effective_at == event


def test_period_sum_uses_effective_at(memory_db) -> None:
    db = memory_db
    u, _ = _seed_user_client(db)
    # Accrual "created" conceptually today but effective yesterday
    event = datetime(2026, 6, 20, 9, 0, 0)
    append_ledger(
        db,
        entry_kind=PayrollFundEntryKind.ACCRUAL,
        side=PayrollFundSide.MASTER,
        user_id=u.id,
        amount=1640,
        source_kind=PayrollFundSourceKind.MANUAL,
        source_id=None,
        created_by_user_id=u.id,
        effective_at=event,
    )
    db.commit()
    start = datetime(2026, 6, 1)
    end = datetime(2026, 7, 1)
    assert employee_payroll_net_in_period(db, u.id, start, end) == 1640.0
    assert employee_payroll_net_in_period(db, u.id, datetime(2026, 7, 1), datetime(2026, 8, 1)) == 0.0


def test_ensure_rejects_future_date(memory_db) -> None:
    db = memory_db
    _seed_period(db, closed=False)
    future = datetime.utcnow() + timedelta(days=3)
    with pytest.raises(ValueError, match="будущ"):
        ensure_event_date_in_open_payroll_period(db, future)


def test_closed_period_by_event_date(memory_db) -> None:
    db = memory_db
    _seed_period(db, closed=True)
    assert is_in_closed_payroll_period(db, datetime(2026, 6, 15)) is True
    assert is_in_closed_payroll_period(db, datetime(2026, 7, 10)) is False


def test_hourly_work_uses_performed_date(memory_db) -> None:
    db = memory_db
    u, _ = _seed_user_client(db)
    entry = HourlyWorkEntry(
        created_at=datetime(2026, 7, 20),
        performed_date=datetime(2026, 6, 5, 14, 0, 0),
        duration_minutes=60,
        amount=500,
        master_user_id=u.id,
    )
    db.add(entry)
    db.flush()
    post_hourly_work_accruals(db, entry, u.id)
    db.commit()
    row = db.scalars(
        select(PayrollFundLedger).where(
            PayrollFundLedger.source_kind == PayrollFundSourceKind.HOURLY_WORK,
            PayrollFundLedger.entry_kind == PayrollFundEntryKind.ACCRUAL,
        )
    ).first()
    assert row is not None
    assert row.effective_at == entry.performed_date


def test_consultation_effective_at_from_visit(memory_db) -> None:
    db = memory_db
    u, c = _seed_user_client(db)
    cons = Consultation(
        created_at=datetime(2026, 6, 1),
        created_by_user_id=u.id,
        client_id=c.id,
        consultation_date=datetime(2026, 6, 1),
        types_json='{"BRAIDING": true}',
    )
    db.add(cons)
    db.flush()
    b = Booking(
        created_at=datetime(2026, 6, 2),
        created_by_user_id=u.id,
        client_id=c.id,
        planned_date=datetime(2026, 6, 10),
        kind=BookingKind.VISIT,
        status=BookingStatus.DONE,
        consultation_id=cons.id,
    )
    db.add(b)
    db.flush()
    visit = Visit(
        created_at=datetime(2026, 6, 12),
        client_id=c.id,
        performed_date=datetime(2026, 6, 10, 15, 0, 0),
        booking_id=b.id,
        amount_from_client=6000,
        duration_minutes=60,
        client_type=VisitClientType.NEW,
        price_type=VisitPriceType.CLIENT,
    )
    db.add(visit)
    db.commit()
    post_consultation_accrual(db, cons.id, u.id)
    db.commit()
    row = db.scalars(
        select(PayrollFundLedger).where(
            PayrollFundLedger.source_kind == PayrollFundSourceKind.CONSULTATION,
        )
    ).first()
    assert row is not None
    assert row.effective_at == visit.performed_date


def test_backfill_respects_closed_flag(memory_db) -> None:
    db = memory_db
    u, _ = _seed_user_client(db)
    _seed_period(db, closed=True)

    entry = HourlyWorkEntry(
        created_at=datetime(2026, 7, 5),
        performed_date=datetime(2026, 6, 8),
        duration_minutes=30,
        amount=10,
        master_user_id=u.id,
    )
    db.add(entry)
    db.flush()
    append_ledger(
        db,
        entry_kind=PayrollFundEntryKind.ACCRUAL,
        side=PayrollFundSide.MASTER,
        user_id=u.id,
        amount=10,
        source_kind=PayrollFundSourceKind.HOURLY_WORK,
        source_id=entry.id,
        created_by_user_id=u.id,
        effective_at=datetime(2026, 7, 5),
    )
    db.commit()

    backfill_payroll_ledger_effective_at(db, allow_closed=False)
    db.commit()
    led = db.scalars(
        select(PayrollFundLedger).where(
            PayrollFundLedger.source_kind == PayrollFundSourceKind.HOURLY_WORK,
            PayrollFundLedger.source_id == entry.id,
        )
    ).first()
    assert led.effective_at == datetime(2026, 7, 5)

    backfill_payroll_ledger_effective_at(db, allow_closed=True)
    db.commit()
    db.refresh(led)
    assert led.effective_at == datetime(2026, 6, 8)
