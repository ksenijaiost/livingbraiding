"""Операционный отчёт режет визиты/продажи/работы по дате события, не по created_at."""

from __future__ import annotations

from datetime import date, datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db import models as _orm_models  # noqa: F401
from app.db.base import Base
from app.db.models import (
    Client,
    HourlyWorkEntry,
    PayrollFundEntryKind,
    PayrollFundLedger,
    PayrollFundSide,
    PayrollFundSourceKind,
    User,
    UserRole,
    Visit,
    VisitClientType,
    VisitMaster,
    VisitMastersScope,
    VisitPriceType,
    VisitService,
    VisitServiceMaster,
)
from app.operational_report import build_operational_report, list_report_visits
from app.payroll_fund import money_q2
from app.ui_visit_display import visit_masters_fund_total


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


def test_list_report_visits_compares_ops_and_ledger_funds(memory_db) -> None:
    db = memory_db
    user, client = _seed_user_client(db)
    when = datetime(2026, 7, 10, 7, 0)
    visit = _add_visit(
        db,
        user=user,
        client=client,
        performed_date=when,
        created_at=when,
        amount=7900.0,
        masters_pool=4000.0,
        salon_profit=3000.0,
    )
    # В журнале меньше, чем в карточке (недопровели начисления).
    db.add(
        PayrollFundLedger(
            created_at=when,
            effective_at=when,
            entry_kind=PayrollFundEntryKind.ACCRUAL,
            side=PayrollFundSide.STUDIO,
            user_id=None,
            amount=3000.0,
            source_kind=PayrollFundSourceKind.VISIT,
            source_id=visit.id,
            created_by_user_id=user.id,
        )
    )
    db.commit()

    rows = list_report_visits(db, date(2026, 7, 1), date(2026, 7, 27))
    assert len(rows) == 1
    row = rows[0]
    assert row.entity_id == visit.id
    # salon 3000 + masters 4000 (100%) = 7000; studio_fund_amount=0
    assert row.amount_ops == 7000.0
    assert row.amount_ledger == 3000.0
    assert row.amount_mismatch is True
    assert row.client_amount_header == 7900.0
    assert row.client_amount_card == 0.0  # нет строк услуг
    assert row.client_amount_mismatch is True
    assert row.any_mismatch is True


def test_list_report_visits_client_header_vs_services(memory_db) -> None:
    db = memory_db
    user, client = _seed_user_client(db)
    when = datetime(2026, 7, 11, 7, 0)
    visit = _add_visit(
        db,
        user=user,
        client=client,
        performed_date=when,
        created_at=when,
        amount=10670.0,
        masters_pool=0.0,
        salon_profit=0.0,
    )
    db.add(
        VisitService(
            visit_id=visit.id,
            service_id=1,
            category_name="Cat",
            subcategory_name="Sub",
            service_name="Svc",
            sort_order=1,
            amount_from_client=7900.0,
            cost_total=0,
            profit_before_split=0,
            salon_profit=0,
            masters_pool=0,
        )
    )
    db.commit()

    rows = list_report_visits(db, date(2026, 7, 1), date(2026, 7, 27))
    assert len(rows) == 1
    row = rows[0]
    assert row.client_amount_header == 10670.0
    assert row.client_amount_card == 7900.0
    assert row.client_amount_mismatch is True
    assert row.amount_ops == 0.0
    assert row.amount_ledger == 0.0
    assert row.amount_mismatch is False
    assert row.any_mismatch is True


def test_visit_ops_funds_include_per_service_masters(memory_db) -> None:
    """Как визит 178: доли на услуге, visit_masters пустой → раньше ЗП выпадала из отчёта."""
    db = memory_db
    user, client = _seed_user_client(db)
    when = datetime(2026, 7, 6, 7, 0)
    visit = _add_visit(
        db,
        user=user,
        client=client,
        performed_date=when,
        created_at=when,
        amount=3800.0,
        masters_pool=1720.0,
        salon_profit=1720.0,
    )
    visit.studio_fund_amount = 200.0
    visit.masters_scope = VisitMastersScope.PER_SERVICE
    # Убираем доли уровня визита — остаются только VisitServiceMaster.
    for vm in list(db.scalars(select(VisitMaster).where(VisitMaster.visit_id == visit.id)).all()):
        db.delete(vm)
    db.flush()

    vs = VisitService(
        visit_id=visit.id,
        service_id=1,
        category_name="Cat",
        subcategory_name="Sub",
        service_name="Svc",
        sort_order=1,
        amount_from_client=3800.0,
        cost_total=360,
        profit_before_split=3440,
        salon_profit=1720,
        masters_pool=1720,
        studio_fund_amount=200,
        amortization_amount=200,
    )
    db.add(vs)
    db.flush()
    db.add(VisitServiceMaster(visit_service_id=vs.id, master_id=user.id, percent=100))
    db.commit()

    db.refresh(visit)
    assert visit_masters_fund_total(visit) == 1720.0

    rows = list_report_visits(db, date(2026, 7, 1), date(2026, 7, 27))
    assert len(rows) == 1
    assert rows[0].amount_ops == 3640.0  # 1720 + 200 + 1720

    report = build_operational_report(db, date(2026, 7, 1), date(2026, 7, 27))
    assert report.visit_studio_to_fund == 1920.0
    assert report.visit_masters_to_fund == 1720.0


def test_list_report_visits_includes_cancelled_with_ledger_orphan(memory_db) -> None:
    db = memory_db
    user, client = _seed_user_client(db)
    when = datetime(2026, 7, 12, 8, 0)
    visit = _add_visit(
        db,
        user=user,
        client=client,
        performed_date=when,
        created_at=when,
        amount=1000.0,
        masters_pool=500.0,
        salon_profit=400.0,
    )
    visit.is_cancelled = True
    db.add(
        PayrollFundLedger(
            created_at=when,
            effective_at=when,
            entry_kind=PayrollFundEntryKind.ACCRUAL,
            side=PayrollFundSide.MASTER,
            user_id=user.id,
            amount=500.0,
            source_kind=PayrollFundSourceKind.VISIT,
            source_id=visit.id,
            created_by_user_id=user.id,
        )
    )
    db.commit()

    rows = list_report_visits(db, date(2026, 7, 1), date(2026, 7, 27))
    assert len(rows) == 1
    row = rows[0]
    assert row.amount_ops == 0.0
    assert row.amount_ledger == 500.0
    assert row.amount_mismatch is True
    assert row.note and "отменён" in row.note


def test_orphan_visit_service_ledger_summary_and_storno(memory_db) -> None:
    """Сирота VISIT_SERVICE (услуги нет) — в сводке сирот и чинится сторно."""
    from app.operational_report import storno_orphan_visit_ledger, summarize_orphan_visit_ledger

    db = memory_db
    user, client = _seed_user_client(db)
    when = datetime(2026, 7, 23, 7, 0)
    # Проводки на несуществующий visit_service_id.
    db.add(
        PayrollFundLedger(
            created_at=when,
            effective_at=when,
            entry_kind=PayrollFundEntryKind.ACCRUAL,
            side=PayrollFundSide.STUDIO,
            user_id=None,
            amount=900.0,
            source_kind=PayrollFundSourceKind.VISIT_SERVICE,
            source_id=999001,
            created_by_user_id=user.id,
        )
    )
    db.add(
        PayrollFundLedger(
            created_at=when,
            effective_at=when,
            entry_kind=PayrollFundEntryKind.ACCRUAL,
            side=PayrollFundSide.MASTER,
            user_id=user.id,
            amount=800.0,
            source_kind=PayrollFundSourceKind.VISIT_SERVICE,
            source_id=999001,
            created_by_user_id=user.id,
        )
    )
    db.commit()

    orphan = summarize_orphan_visit_ledger(db, date(2026, 7, 1), date(2026, 7, 27))
    assert orphan.source_count == 1
    assert orphan.net_amount == 1700.0
    assert orphan.details[0] == ("VISIT_SERVICE", 999001, 1700.0)

    after = storno_orphan_visit_ledger(db, date(2026, 7, 1), date(2026, 7, 27), created_by_user_id=user.id)
    db.commit()
    assert after.source_count == 0
    assert after.net_amount == 0.0


def test_report_includes_consultations_hourly_and_manual(memory_db) -> None:
    db = memory_db
    user, client = _seed_user_client(db)
    when = datetime(2026, 6, 10, 12, 0)

    hourly = HourlyWorkEntry(
        performed_date=when,
        duration_minutes=60,
        amount=500.0,
        master_user_id=user.id,
        created_by_user_id=user.id,
        comment="hour",
    )
    db.add(hourly)
    db.flush()

    # Как в post_hourly_work: ACCRUAL мастеру + EXPENSE студии.
    db.add(
        PayrollFundLedger(
            created_at=when,
            effective_at=when,
            entry_kind=PayrollFundEntryKind.ACCRUAL,
            side=PayrollFundSide.MASTER,
            user_id=user.id,
            amount=500.0,
            source_kind=PayrollFundSourceKind.HOURLY_WORK,
            source_id=hourly.id,
            created_by_user_id=user.id,
        )
    )
    db.add(
        PayrollFundLedger(
            created_at=when,
            effective_at=when,
            entry_kind=PayrollFundEntryKind.EXPENSE,
            side=PayrollFundSide.STUDIO,
            user_id=None,
            amount=-500.0,
            source_kind=PayrollFundSourceKind.HOURLY_WORK,
            source_id=hourly.id,
            created_by_user_id=user.id,
        )
    )
    db.add(
        PayrollFundLedger(
            created_at=when,
            effective_at=when,
            entry_kind=PayrollFundEntryKind.ACCRUAL,
            side=PayrollFundSide.MASTER,
            user_id=user.id,
            amount=300.0,
            source_kind=PayrollFundSourceKind.CONSULTATION,
            source_id=42,
            created_by_user_id=user.id,
        )
    )
    db.add(
        PayrollFundLedger(
            created_at=when,
            effective_at=when,
            entry_kind=PayrollFundEntryKind.ACCRUAL,
            side=PayrollFundSide.MASTER,
            user_id=user.id,
            amount=150.0,
            source_kind=PayrollFundSourceKind.MANUAL,
            source_id=None,
            created_by_user_id=user.id,
            comment="ручная доплата",
        )
    )
    db.commit()

    june = build_operational_report(db, date(2026, 6, 1), date(2026, 6, 30))
    assert june.hourly_work_count == 1
    assert june.hourly_masters_to_fund == 500.0
    assert june.consultations_count == 1
    assert june.consultation_masters_to_fund == 300.0
    assert june.manual_to_fund == 150.0
    assert june.total_to_funds_without_manual == 800.0
    assert june.total_to_funds == 950.0
    # EXPENSE почасовой не входит в нетто начислений.
    assert june.ledger_net_accruals == 950.0
    assert june.reconciliation_delta == 0.0

    by_key = {b.key: b for b in june.reconcile_buckets}
    assert by_key["hourly"].delta == 0.0
    assert by_key["consultations"].delta == 0.0
    assert by_key["manual"].delta == 0.0
    assert june.employees[0].from_hourly == 500.0
    assert june.employees[0].from_consultations == 300.0
    assert june.employees[0].from_manual == 150.0


def test_report_hourly_expense_storno_does_not_inflate_ledger_net(memory_db) -> None:
    """Сторно EXPENSE почасовой не должно портить сверку начислений."""
    db = memory_db
    user, _client = _seed_user_client(db)
    when = datetime(2026, 6, 12, 9, 0)
    hourly = HourlyWorkEntry(
        performed_date=when,
        duration_minutes=30,
        amount=200.0,
        master_user_id=user.id,
        created_by_user_id=user.id,
    )
    db.add(hourly)
    db.flush()

    acc = PayrollFundLedger(
        created_at=when,
        effective_at=when,
        entry_kind=PayrollFundEntryKind.ACCRUAL,
        side=PayrollFundSide.MASTER,
        user_id=user.id,
        amount=200.0,
        source_kind=PayrollFundSourceKind.HOURLY_WORK,
        source_id=hourly.id,
        created_by_user_id=user.id,
    )
    exp = PayrollFundLedger(
        created_at=when,
        effective_at=when,
        entry_kind=PayrollFundEntryKind.EXPENSE,
        side=PayrollFundSide.STUDIO,
        user_id=None,
        amount=-200.0,
        source_kind=PayrollFundSourceKind.HOURLY_WORK,
        source_id=hourly.id,
        created_by_user_id=user.id,
    )
    db.add_all([acc, exp])
    db.flush()
    db.add(
        PayrollFundLedger(
            created_at=when,
            effective_at=when,
            entry_kind=PayrollFundEntryKind.STORNO,
            side=PayrollFundSide.MASTER,
            user_id=user.id,
            amount=-200.0,
            source_kind=PayrollFundSourceKind.HOURLY_WORK,
            source_id=hourly.id,
            storno_of_id=acc.id,
            created_by_user_id=user.id,
        )
    )
    db.add(
        PayrollFundLedger(
            created_at=when,
            effective_at=when,
            entry_kind=PayrollFundEntryKind.STORNO,
            side=PayrollFundSide.STUDIO,
            user_id=None,
            amount=200.0,
            source_kind=PayrollFundSourceKind.HOURLY_WORK,
            source_id=hourly.id,
            storno_of_id=exp.id,
            created_by_user_id=user.id,
        )
    )
    # Карточка осталась — ops всё ещё видит 200, журнал начислений нетто 0.
    db.commit()

    june = build_operational_report(db, date(2026, 6, 1), date(2026, 6, 30))
    assert june.hourly_masters_to_fund == 200.0
    assert june.ledger_net_accruals == 0.0
    assert june.reconciliation_delta == 200.0
    by_key = {b.key: b for b in june.reconcile_buckets}
    assert by_key["hourly"].ops_amount == 200.0
    assert by_key["hourly"].ledger_amount == 0.0
    assert by_key["hourly"].delta == 200.0
