"""Тесты почасовой работы (1.8)."""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import select

from app.db.models import (
    HourlyWorkEntry,
    PayrollFundEntryKind,
    PayrollFundLedger,
    PayrollFundSide,
    PayrollFundSourceKind,
    PayrollPeriod,
    User,
    UserRole,
    UserRoleAssignment,
)
from app.hourly_work import create_hourly_work_entry, parse_hourly_work_form


class _FakeForm(dict):
    def keys(self):
        return super().keys()


@pytest.fixture()
def memory_db():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.db import models as _orm_models  # noqa: F401
    from app.db.base import Base

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        yield db
    finally:
        db.close()


def _seed_master(db, *, uid: int = 10) -> User:
    u = User(
        id=uid,
        username=f"m{uid}",
        display_name=f"Master {uid}",
        password_hash="x",
        role=UserRole.MASTER,
        is_active=True,
    )
    db.add(u)
    db.flush()
    db.add(UserRoleAssignment(user_id=u.id, role=UserRole.MASTER))
    db.commit()
    return u


def test_parse_hourly_work_form_master_self():
    form = _FakeForm(
        {
            "performed_date": "2026-07-20",
            "duration_h": "1",
            "duration_m": "30",
            "amount": "1500",
            "comment": "уборка",
        }
    )
    entry, err = parse_hourly_work_form(form, current_user_id=10, is_admin=False)
    assert err is None
    assert entry is not None
    assert entry.master_user_id == 10
    assert entry.duration_minutes == 90
    assert entry.amount == 1500.0


def test_create_hourly_work_posts_master_and_studio_ledger(memory_db):
    master = _seed_master(memory_db, uid=10)
    admin = User(
        id=1,
        username="admin",
        display_name="Admin",
        password_hash="x",
        role=UserRole.ADMIN_SUPER,
        is_active=True,
    )
    memory_db.add(admin)
    memory_db.flush()
    memory_db.add(UserRoleAssignment(user_id=admin.id, role=UserRole.ADMIN_SUPER))
    memory_db.add(
        PayrollPeriod(
            date_from=datetime(2020, 1, 1),
            date_to=datetime(2030, 12, 31, 23, 59, 59),
            closed_at=None,
        )
    )
    memory_db.commit()

    entry = HourlyWorkEntry(
        performed_date=datetime(2026, 7, 20),
        duration_minutes=60,
        amount=800.0,
        comment=None,
        master_user_id=int(master.id),
    )
    saved = create_hourly_work_entry(memory_db, entry, created_by_user_id=int(admin.id))
    assert saved.id is not None

    rows = list(
        memory_db.scalars(
            select(PayrollFundLedger).where(
                PayrollFundLedger.source_kind == PayrollFundSourceKind.HOURLY_WORK,
                PayrollFundLedger.source_id == int(saved.id),
            )
        ).all()
    )
    assert len(rows) == 2
    master_row = next(r for r in rows if r.side == PayrollFundSide.MASTER)
    studio_row = next(r for r in rows if r.side == PayrollFundSide.STUDIO)
    assert master_row.entry_kind == PayrollFundEntryKind.ACCRUAL
    assert float(master_row.amount) == 800.0
    assert int(master_row.user_id) == int(master.id)
    assert studio_row.entry_kind == PayrollFundEntryKind.EXPENSE
    assert float(studio_row.amount) == -800.0


def test_update_hourly_work_replaces_payroll(memory_db):
    from app.hourly_work import update_hourly_work_entry

    master = _seed_master(memory_db, uid=10)
    admin = User(
        id=1,
        username="admin",
        display_name="Admin",
        password_hash="x",
        role=UserRole.ADMIN_SUPER,
        is_active=True,
    )
    memory_db.add(admin)
    memory_db.flush()
    memory_db.add(UserRoleAssignment(user_id=admin.id, role=UserRole.ADMIN_SUPER))
    memory_db.add(
        PayrollPeriod(
            date_from=datetime(2020, 1, 1),
            date_to=datetime(2030, 12, 31, 23, 59, 59),
            closed_at=None,
        )
    )
    memory_db.commit()

    entry = HourlyWorkEntry(
        performed_date=datetime(2026, 7, 20),
        duration_minutes=60,
        amount=800.0,
        comment=None,
        master_user_id=int(master.id),
    )
    saved = create_hourly_work_entry(memory_db, entry, created_by_user_id=int(admin.id))

    draft = HourlyWorkEntry(
        performed_date=datetime(2026, 7, 21),
        duration_minutes=90,
        amount=1200.0,
        comment="правка",
        master_user_id=int(master.id),
    )
    updated = update_hourly_work_entry(
        memory_db,
        saved,
        draft,
        updated_by_user_id=int(admin.id),
        is_admin=True,
    )
    assert updated.amount == 1200.0
    assert updated.duration_minutes == 90

    rows = list(
        memory_db.scalars(
            select(PayrollFundLedger).where(
                PayrollFundLedger.source_kind == PayrollFundSourceKind.HOURLY_WORK,
                PayrollFundLedger.source_id == int(updated.id),
            )
        ).all()
    )
    accruals = [r for r in rows if r.entry_kind == PayrollFundEntryKind.ACCRUAL]
    stornos = [r for r in rows if r.entry_kind == PayrollFundEntryKind.STORNO]
    assert len(accruals) == 2  # old + new
    assert len(stornos) >= 2  # master + studio of old
    latest_master = max(
        (r for r in accruals if r.side == PayrollFundSide.MASTER),
        key=lambda r: int(r.id),
    )
    assert float(latest_master.amount) == 1200.0


def test_can_access_hourly_work_entry():
    from types import SimpleNamespace

    from app.hourly_work import can_access_hourly_work_entry

    entry = SimpleNamespace(master_user_id=10)
    assert can_access_hourly_work_entry(entry, current_user_id=10, is_admin=False) is True
    assert can_access_hourly_work_entry(entry, current_user_id=11, is_admin=False) is False
    assert can_access_hourly_work_entry(entry, current_user_id=11, is_admin=True) is True


def test_void_hourly_work_stornos_ledger(memory_db):
    from app.hourly_work import void_hourly_work_entry

    master = _seed_master(memory_db, uid=10)
    admin = User(
        id=1,
        username="admin",
        display_name="Admin",
        password_hash="x",
        role=UserRole.ADMIN_SUPER,
        is_active=True,
    )
    memory_db.add(admin)
    memory_db.flush()
    memory_db.add(UserRoleAssignment(user_id=admin.id, role=UserRole.ADMIN_SUPER))
    memory_db.add(
        PayrollPeriod(
            date_from=datetime(2020, 1, 1),
            date_to=datetime(2030, 12, 31, 23, 59, 59),
            closed_at=None,
        )
    )
    memory_db.commit()

    entry = HourlyWorkEntry(
        performed_date=datetime(2026, 7, 20),
        duration_minutes=60,
        amount=500.0,
        comment=None,
        master_user_id=int(master.id),
    )
    saved = create_hourly_work_entry(memory_db, entry, created_by_user_id=int(admin.id))
    voided = void_hourly_work_entry(memory_db, saved, voided_by_user_id=int(admin.id))
    assert voided.is_voided is True
    assert voided.voided_at is not None

    accruals = list(
        memory_db.scalars(
            select(PayrollFundLedger).where(
                PayrollFundLedger.source_kind == PayrollFundSourceKind.HOURLY_WORK,
                PayrollFundLedger.source_id == int(saved.id),
                PayrollFundLedger.entry_kind == PayrollFundEntryKind.ACCRUAL,
            )
        ).all()
    )
    assert len(accruals) >= 1
    # после сторно чистый ACCRUAL по мастеру с учётом сторно = 0 (есть storno_of)
    stornos = list(
        memory_db.scalars(
            select(PayrollFundLedger).where(
                PayrollFundLedger.source_kind == PayrollFundSourceKind.HOURLY_WORK,
                PayrollFundLedger.source_id == int(saved.id),
                PayrollFundLedger.storno_of_id.is_not(None),
            )
        ).all()
    )
    assert len(stornos) >= 1
