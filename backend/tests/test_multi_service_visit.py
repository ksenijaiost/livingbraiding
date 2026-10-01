"""Визит с несколькими услугами."""

from __future__ import annotations

from datetime import date, datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import selectinload, sessionmaker

from app.db import models as _orm_models  # noqa: F401
from app.db.base import Base
from app.db.models import (
    Client,
    MixSource,
    PayrollFundLedger,
    PayrollFundSide,
    PayrollFundSourceKind,
    Service,
    ServiceCategory,
    ServiceSubcategory,
    User,
    UserRole,
    UserRoleAssignment,
    Visit,
    VisitClientType,
    VisitMastersScope,
    VisitServiceMaster,
    PayrollPeriod,
)
from app.visit_multi_service import (
    MultiServiceVisitInput,
    VisitHeaderInput,
    VisitServiceLineInput,
    recalc_visit_totals,
    save_visit_with_services,
)
from app.payroll_fund import storno_source_accruals
from app.time_utils import utcnow_naive


@pytest.fixture()
def memory_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as db:
        yield db


def _seed_master_and_services(db) -> tuple[User, list[int]]:
    u = User(
        username="m1",
        password_hash="x",
        display_name="Master",
        role=UserRole.MASTER,
        is_active=True,
    )
    db.add(u)
    db.flush()
    db.add(UserRoleAssignment(user_id=u.id, role=UserRole.MASTER))
    db.add(
        PayrollPeriod(
            date_from=datetime(2020, 1, 1),
            date_to=datetime(2030, 12, 31, 23, 59, 59),
            closed_at=None,
        )
    )
    db.flush()
    cat = ServiceCategory(name="Вся голова", is_active=True)
    db.add(cat)
    db.flush()
    sub = ServiceSubcategory(category_id=cat.id, name="Плетение", is_active=True)
    db.add(sub)
    db.flush()
    ids: list[int] = []
    for name in ("Услуга A", "Услуга B"):
        s = Service(subcategory_id=sub.id, name=name, is_active=True)
        db.add(s)
        db.flush()
        ids.append(int(s.id))
    db.commit()
    db.refresh(u)
    return u, ids


def _header(client_id: int, master_id: int) -> VisitHeaderInput:
    return VisitHeaderInput(
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
        visit_master_allocations=[(master_id, 100)],
    )


def _line(service_id: int, amount: float) -> VisitServiceLineInput:
    return VisitServiceLineInput(
        service_id=service_id,
        amount_from_client=amount,
        client_discount_percent=0,
        kanekalon_grams=0,
        kudri_grams=0,
        mix_source=MixSource.NO_MIX,
        mix_complexity=None,
        mix_bonus_master_id=None,
        amortization_level=None,
        kit_kind="STOCK",
    )


def test_two_services_sum_visit_total(memory_db) -> None:
    db = memory_db
    master, svc_ids = _seed_master_and_services(db)
    client = Client(name="Multi", phone="+79990001122", is_confirmed=True)
    db.add(client)
    db.commit()
    visit = save_visit_with_services(
        db,
        master.id,
        MultiServiceVisitInput(
            header=_header(client.id, master.id),
            lines=[_line(svc_ids[0], 3000), _line(svc_ids[1], 2000)],
        ),
    )
    assert len(visit.services) == 2
    assert visit.amount_from_client == pytest.approx(5000.0)


def test_visit_service_payroll_accruals(memory_db) -> None:
    db = memory_db
    master, svc_ids = _seed_master_and_services(db)
    client = Client(name="Pay", phone="+79990003344", is_confirmed=True)
    db.add(client)
    db.commit()
    visit = save_visit_with_services(
        db,
        master.id,
        MultiServiceVisitInput(header=_header(client.id, master.id), lines=[_line(svc_ids[0], 4000)]),
    )
    vs = visit.services[0]
    rows = list(
        db.scalars(
            select(PayrollFundLedger).where(
                PayrollFundLedger.source_kind == PayrollFundSourceKind.VISIT_SERVICE,
                PayrollFundLedger.source_id == vs.id,
            )
        ).all()
    )
    assert rows


def test_recalc_after_line_cancel(memory_db) -> None:
    db = memory_db
    master, svc_ids = _seed_master_and_services(db)
    client = Client(name="Cancel", phone="+79990005566", is_confirmed=True)
    db.add(client)
    db.commit()
    visit = save_visit_with_services(
        db,
        master.id,
        MultiServiceVisitInput(
            header=_header(client.id, master.id),
            lines=[_line(svc_ids[0], 1000), _line(svc_ids[1], 2000)],
        ),
    )
    visit = db.scalar(select(Visit).options(selectinload(Visit.services)).where(Visit.id == visit.id))
    assert visit is not None
    vs_cancel = visit.services[1]
    storno_source_accruals(db, PayrollFundSourceKind.VISIT_SERVICE, vs_cancel.id, master.id)
    vs_cancel.is_cancelled = True
    vs_cancel.cancelled_at = utcnow_naive()
    recalc_visit_totals(visit)
    db.commit()
    db.refresh(visit)
    assert visit.amount_from_client == pytest.approx(1000.0)
    assert not visit.is_cancelled


def test_per_service_last_line_master_accrual_with_autoflush_off() -> None:
    """Регресс 1.92: при autoflush=False masters последней услуги должны попасть в журнал."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, future=True)
    with SessionLocal() as db:
        master, svc_ids = _seed_master_and_services(db)
        master_b = User(
            username="m2",
            password_hash="x",
            display_name="Master B",
            role=UserRole.MASTER,
            is_active=True,
        )
        db.add(master_b)
        db.flush()
        db.add(UserRoleAssignment(user_id=master_b.id, role=UserRole.MASTER))
        client = Client(name="Flush", phone="+79990007788", is_confirmed=True)
        db.add(client)
        db.commit()

        header = VisitHeaderInput(
            client_mode="existing",
            existing_client_id=client.id,
            draft_name="",
            draft_phone="",
            draft_telegram="",
            draft_vk="",
            draft_instagram="",
            draft_other_contact="",
            client_type=VisitClientType.RETURNING,
            performed_date=date.today(),
            duration_minutes=60,
            masters_scope=VisitMastersScope.PER_SERVICE,
            same_master_shares_all_services=False,
            visit_master_allocations=[],
        )
        line1 = _line(svc_ids[0], 4000)
        line1.service_master_allocations = [(master.id, 50), (master_b.id, 50)]
        line2 = _line(svc_ids[1], 300)
        line2.service_master_allocations = [(master.id, 100)]

        visit = save_visit_with_services(
            db,
            master.id,
            MultiServiceVisitInput(header=header, lines=[line1, line2]),
        )
        services = sorted(visit.services, key=lambda s: int(s.sort_order or 0))
        assert len(services) == 2
        last = services[-1]
        masters_last = list(
            db.scalars(
                select(VisitServiceMaster).where(VisitServiceMaster.visit_service_id == last.id)
            ).all()
        )
        assert len(masters_last) == 1
        assert float(last.masters_pool or 0) > 0

        master_rows = list(
            db.scalars(
                select(PayrollFundLedger).where(
                    PayrollFundLedger.source_kind == PayrollFundSourceKind.VISIT_SERVICE,
                    PayrollFundLedger.source_id == last.id,
                    PayrollFundLedger.side == PayrollFundSide.MASTER,
                    PayrollFundLedger.entry_kind == "ACCRUAL",
                )
            ).all()
        )
        assert master_rows, "начисление мастеру по последней услуге должно быть в журнале"
        assert sum(float(r.amount or 0) for r in master_rows) == pytest.approx(float(last.masters_pool))
