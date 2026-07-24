"""Операционный отчёт режет визиты/продажи/работы по дате события, не по created_at."""

from __future__ import annotations

from datetime import date, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import models as _orm_models  # noqa: F401
from app.db.base import Base
from app.db.models import (
    Client,
    PayrollFundEntryKind,
    PayrollFundLedger,
    PayrollFundSide,
    PayrollFundSourceKind,
    User,
    UserRole,
    Visit,
    VisitClientType,
    VisitMaster,
    VisitPriceType,
)
from app.operational_report import build_operational_report, list_report_visits
from app.payroll_fund import money_q2


@pytest.fixture()
def memory_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as db:
        yield db


def _seed_user_client(db):
    u = User(
        username="m",
        password_hash="x",
        display_name="Master",
        role=UserRole.MASTER,
        is_active=True,
    )
    c = Client(name="C", phone="+79990001111", is_confirmed=True)
    db.add_all([u, c])
    db.flush()
    return u, c


def _add_visit(
    db,
    *,
    user: User,
    client: Client,
    performed_date: datetime,
    created_at: datetime,
    amount: float = 1000.0,
    masters_pool: float = 400.0,
    salon_profit: float = 300.0,
) -> Visit:
    visit = Visit(
        created_by_user_id=user.id,
        client_id=client.id,
        client_type=VisitClientType.RETURNING,
        price_type=VisitPriceType.CLIENT,
        performed_date=performed_date,
        duration_minutes=60,
        amount_from_client=amount,
        cost_total=0,
        profit_before_split=amount,
        salon_profit=salon_profit,
        masters_pool=masters_pool,
        studio_fund_amount=0,
        created_at=created_at,
        is_cancelled=False,
    )
    db.add(visit)
    db.flush()
    db.add(
        VisitMaster(
            visit_id=visit.id,
            master_id=user.id,
            percent=100,
        )
    )
    db.flush()
    return visit


def test_report_includes_visit_by_performed_date_not_created_at(memory_db) -> None:
    db = memory_db
    user, client = _seed_user_client(db)
    # Событие в июне, создан в июле → должен попасть в июньский отчёт.
    visit = _add_visit(
        db,
        user=user,
        client=client,
        performed_date=datetime(2026, 6, 15, 10, 0),
        created_at=datetime(2026, 7, 2, 12, 0),
        amount=1000.0,
        masters_pool=400.0,
        salon_profit=300.0,
    )
    db.add(
        PayrollFundLedger(
            created_at=datetime(2026, 7, 2, 12, 0),
            effective_at=datetime(2026, 6, 15, 10, 0),
            entry_kind=PayrollFundEntryKind.ACCRUAL,
            side=PayrollFundSide.MASTER,
            user_id=user.id,
            amount=400.0,
            source_kind=PayrollFundSourceKind.VISIT,
            source_id=visit.id,
            created_by_user_id=user.id,
        )
    )
    db.add(
        PayrollFundLedger(
            created_at=datetime(2026, 7, 2, 12, 0),
            effective_at=datetime(2026, 6, 15, 10, 0),
            entry_kind=PayrollFundEntryKind.ACCRUAL,
            side=PayrollFundSide.STUDIO,
            user_id=None,
            amount=300.0,
            source_kind=PayrollFundSourceKind.VISIT,
            source_id=visit.id,
            created_by_user_id=user.id,
        )
    )
    db.commit()

    june = build_operational_report(db, date(2026, 6, 1), date(2026, 6, 30))
    assert june.visits_count == 1
    assert june.revenue_visits == 1000.0
    assert june.visit_masters_to_fund == 400.0
    assert june.visit_studio_to_fund == 300.0
    assert june.total_to_funds == 700.0
    assert june.ledger_net_accruals == 700.0
    assert june.reconciliation_delta == 0.0

    july = build_operational_report(db, date(2026, 7, 1), date(2026, 7, 31))
    assert july.visits_count == 0
    assert july.revenue_visits == 0.0


def test_report_excludes_visit_created_in_period_but_performed_outside(memory_db) -> None:
    db = memory_db
    user, client = _seed_user_client(db)
    # Создан в июне, событие в мае → в июньский отчёт не входит.
    _add_visit(
        db,
        user=user,
        client=client,
        performed_date=datetime(2026, 5, 20, 10, 0),
        created_at=datetime(2026, 6, 5, 12, 0),
    )
    db.commit()

    june = build_operational_report(db, date(2026, 6, 1), date(2026, 6, 30))
    assert june.visits_count == 0
    assert list_report_visits(db, date(2026, 6, 1), date(2026, 6, 30)) == []

    may = build_operational_report(db, date(2026, 5, 1), date(2026, 5, 31))
    assert may.visits_count == 1
