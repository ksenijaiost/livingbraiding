"""1.12: поиск по журналу фонда ЗП."""

from __future__ import annotations

from datetime import date, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import models as _orm_models  # noqa: F401
from app.db.base import Base
from app.db.models import (
    PayrollFundEntryKind,
    PayrollFundSide,
    PayrollFundSourceKind,
    User,
    UserRole,
)
from app.payroll_fund import append_ledger, search_ledger_rows


@pytest.fixture()
def memory_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as db:
        yield db


def _user(db, name: str = "M") -> User:
    u = User(username=name, password_hash="x", display_name=name, role=UserRole.MASTER, is_active=True)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _row(db, *, user_id: int | None, effective_at: datetime, source_kind: PayrollFundSourceKind, amount: float = 10.0):
    return append_ledger(
        db,
        entry_kind=PayrollFundEntryKind.ACCRUAL,
        side=PayrollFundSide.MASTER if user_id else PayrollFundSide.STUDIO,
        user_id=user_id,
        amount=amount,
        source_kind=source_kind,
        source_id=1,
        created_by_user_id=user_id,
        effective_at=effective_at,
    )


def test_search_by_effective_date_range(memory_db) -> None:
    db = memory_db
    u = _user(db)
    _row(db, user_id=u.id, effective_at=datetime(2026, 6, 1), source_kind=PayrollFundSourceKind.VISIT)
    mid = _row(db, user_id=u.id, effective_at=datetime(2026, 6, 15), source_kind=PayrollFundSourceKind.WORK)
    _row(db, user_id=u.id, effective_at=datetime(2026, 7, 1), source_kind=PayrollFundSourceKind.VISIT)
    db.commit()

    rows = search_ledger_rows(db, effective_from=date(2026, 6, 10), effective_to=date(2026, 6, 20))
    assert [r.id for r in rows] == [mid.id]


def test_search_by_whom_and_source(memory_db) -> None:
    db = memory_db
    a = _user(db, "A")
    b = _user(db, "B")
    keep = _row(db, user_id=a.id, effective_at=datetime(2026, 6, 5), source_kind=PayrollFundSourceKind.WORK)
    _row(db, user_id=a.id, effective_at=datetime(2026, 6, 5), source_kind=PayrollFundSourceKind.VISIT)
    _row(db, user_id=b.id, effective_at=datetime(2026, 6, 5), source_kind=PayrollFundSourceKind.WORK)
    studio = _row(db, user_id=None, effective_at=datetime(2026, 6, 5), source_kind=PayrollFundSourceKind.VISIT)
    db.commit()

    by_user = search_ledger_rows(db, user_id=a.id, source_kind=PayrollFundSourceKind.WORK)
    assert [r.id for r in by_user] == [keep.id]

    by_studio = search_ledger_rows(db, studio_only=True)
    assert [r.id for r in by_studio] == [studio.id]


def test_search_without_filters_is_recent(memory_db) -> None:
    db = memory_db
    u = _user(db)
    first = _row(db, user_id=u.id, effective_at=datetime(2026, 1, 1), source_kind=PayrollFundSourceKind.MANUAL)
    second = _row(db, user_id=u.id, effective_at=datetime(2026, 2, 1), source_kind=PayrollFundSourceKind.MANUAL)
    db.commit()
    rows = search_ledger_rows(db, limit=1)
    assert len(rows) == 1
    assert rows[0].id == second.id
    assert first.id != second.id


def test_search_visit_includes_visit_service(memory_db) -> None:
    db = memory_db
    u = _user(db)
    visit_row = _row(
        db, user_id=u.id, effective_at=datetime(2026, 6, 5), source_kind=PayrollFundSourceKind.VISIT
    )
    svc_row = _row(
        db, user_id=u.id, effective_at=datetime(2026, 6, 6), source_kind=PayrollFundSourceKind.VISIT_SERVICE
    )
    _row(db, user_id=u.id, effective_at=datetime(2026, 6, 7), source_kind=PayrollFundSourceKind.WORK)
    db.commit()
    rows = search_ledger_rows(db, source_kind=PayrollFundSourceKind.VISIT)
    assert {r.id for r in rows} == {visit_row.id, svc_row.id}
