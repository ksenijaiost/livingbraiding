"""Полное редактирование визита: политика, сторно, сохранение."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.auth import AuthUser
from app.db import models as _orm_models  # noqa: F401
from app.db.base import Base
from app.db.models import (
    Client,
    MixSource,
    PayrollFundEntryKind,
    PayrollFundLedger,
    PayrollFundSide,
    PayrollFundSourceKind,
    PayrollPeriod,
    Service,
    ServiceCategory,
    ServiceSubcategory,
    Setting,
    User,
    UserRole,
    UserRoleAssignment,
    Visit,
    VisitClientType,
    VisitMaster,
    VisitMastersScope,
    VisitService,
)
from app.payroll_fund import sum_visit_ledger_by_visit_id
from app.setting_keys import EDIT_WINDOW_DAYS
from app.time_utils import utcnow_naive
from app.visit_edit_policy import visit_edit_policy, user_participates_in_visit
from app.visit_multi_service import (
    MultiServiceVisitInput,
    VisitHeaderInput,
    VisitServiceLineInput,
    save_visit_with_services,
    update_visit_with_services,
)


@pytest.fixture()
def memory_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as db:
        yield db


def _seed_users_and_services(db) -> tuple[User, User, User, list[int]]:
    master_a = User(username="ma", password_hash="x", display_name="Master A", role=UserRole.MASTER, is_active=True)
    master_b = User(username="mb", password_hash="x", display_name="Master B", role=UserRole.MASTER, is_active=True)
    admin = User(username="adm", password_hash="x", display_name="Admin", role=UserRole.ADMIN, is_active=True)
    db.add_all([master_a, master_b, admin])
    db.flush()
    for u, role in [(master_a, UserRole.MASTER), (master_b, UserRole.MASTER), (admin, UserRole.ADMIN)]:
        db.add(UserRoleAssignment(user_id=u.id, role=role))
    db.add(
        PayrollPeriod(
            date_from=datetime(2020, 1, 1),
            date_to=datetime(2030, 12, 31, 23, 59, 59),
            closed_at=None,
        )
    )
    db.add(Setting(key=EDIT_WINDOW_DAYS, value="7"))
    client = Client(name="Клиент", phone="+79990001122", is_confirmed=True)
    db.add(client)
    db.flush()
    cat = ServiceCategory(name="Вся голова", is_active=True)
    db.add(cat)
    db.flush()
    sub = ServiceSubcategory(category_id=cat.id, name="Плетение", is_active=True)
    db.add(sub)
    db.flush()
    svc_ids: list[int] = []
    for name in ("Услуга A",):
        s = Service(subcategory_id=sub.id, name=name, is_active=True)
        db.add(s)
        db.flush()
        svc_ids.append(s.id)
    db.commit()
    return master_a, master_b, admin, svc_ids


def _auth(user: User) -> AuthUser:
    return AuthUser(
        id=user.id,
        username=user.username,
        display_name=user.display_name,
        role=user.role,
        roles=(user.role,),
        master_level=user.master_level,
    )


def _make_visit(db, master: User, client_id: int, service_id: int, amount: float = 1000.0) -> Visit:
    header = VisitHeaderInput(
        client_mode="existing",
        existing_client_id=client_id,
        draft_name="",
        draft_phone="",
        draft_telegram="",
        draft_vk="",
        draft_instagram="",
        draft_other_contact="",
        client_type=VisitClientType.RETURNING,
        performed_date=datetime.utcnow().date(),
        duration_minutes=60,
        masters_scope=VisitMastersScope.VISIT,
        same_master_shares_all_services=False,
        visit_master_allocations=[(master.id, 100)],
    )
    line = VisitServiceLineInput(
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
    return save_visit_with_services(db, master.id, MultiServiceVisitInput(header=header, lines=[line]))


def test_master_participant_can_edit_within_window(memory_db):
    db = memory_db
    master_a, master_b, admin, svc_ids = _seed_users_and_services(db)
    client = db.scalar(select(Client).limit(1))
    visit = _make_visit(db, master_a, client.id, svc_ids[0])
    assert user_participates_in_visit(db, visit.id, master_a.id)
    assert not user_participates_in_visit(db, visit.id, master_b.id)
    pol_a = visit_edit_policy(visit, _auth(master_a), db)
    pol_b = visit_edit_policy(visit, _auth(master_b), db)
    assert pol_a.can_edit
    assert not pol_b.can_edit


def test_admin_edit_window(memory_db):
    db = memory_db
    master_a, _master_b, admin, svc_ids = _seed_users_and_services(db)
    client = db.scalar(select(Client).limit(1))
    visit = _make_visit(db, master_a, client.id, svc_ids[0])
    assert visit_edit_policy(visit, _auth(admin), db).can_edit
    visit.created_at = utcnow_naive() - timedelta(days=30)
    assert not visit_edit_policy(visit, _auth(admin), db).can_edit


def test_update_amount_triggers_storno(memory_db):
    db = memory_db
    master_a, _master_b, _admin, svc_ids = _seed_users_and_services(db)
    client = db.scalar(select(Client).limit(1))
    visit = _make_visit(db, master_a, client.id, svc_ids[0], amount=1000.0)
    vs = db.scalar(select(VisitService).where(VisitService.visit_id == visit.id))
    assert vs is not None
    before_count = len(
        db.scalars(
            select(PayrollFundLedger).where(
                PayrollFundLedger.source_kind == PayrollFundSourceKind.VISIT_SERVICE,
                PayrollFundLedger.source_id == vs.id,
            )
        ).all()
    )
    assert before_count > 0

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
        performed_date=visit.performed_date.date(),
        duration_minutes=visit.duration_minutes,
        masters_scope=VisitMastersScope.VISIT,
        same_master_shares_all_services=False,
        visit_master_allocations=[(master_a.id, 100)],
    )
    line = VisitServiceLineInput(
        service_id=svc_ids[0],
        amount_from_client=1500.0,
        client_discount_percent=0,
        kanekalon_grams=0,
        kudri_grams=0,
        mix_source=MixSource.NO_MIX,
        mix_complexity=None,
        mix_bonus_master_id=None,
        amortization_level=None,
        kit_kind="STOCK",
        visit_service_id=vs.id,
    )
    update_visit_with_services(db, visit.id, master_a.id, MultiServiceVisitInput(header=header, lines=[line]))

    rows = list(
        db.scalars(
            select(PayrollFundLedger).where(
                PayrollFundLedger.source_kind == PayrollFundSourceKind.VISIT_SERVICE,
                PayrollFundLedger.source_id == vs.id,
            )
        ).all()
    )
    assert any(r.entry_kind == PayrollFundEntryKind.STORNO for r in rows)
    accruals = [r for r in rows if r.entry_kind == PayrollFundEntryKind.ACCRUAL and r.storno_of_id is None]
    assert len(accruals) >= 1


def test_update_comment_only_no_storno(memory_db):
    db = memory_db
    master_a, _master_b, _admin, svc_ids = _seed_users_and_services(db)
    client = db.scalar(select(Client).limit(1))
    visit = _make_visit(db, master_a, client.id, svc_ids[0])
    vs = db.scalar(select(VisitService).where(VisitService.visit_id == visit.id))
    before = list(
        db.scalars(
            select(PayrollFundLedger).where(
                PayrollFundLedger.source_kind == PayrollFundSourceKind.VISIT_SERVICE,
                PayrollFundLedger.source_id == vs.id,
            )
        ).all()
    )
    before_storno = sum(1 for r in before if r.entry_kind == PayrollFundEntryKind.STORNO)

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
        performed_date=visit.performed_date.date(),
        duration_minutes=90,
        masters_scope=VisitMastersScope.VISIT,
        same_master_shares_all_services=False,
        visit_master_allocations=[(master_a.id, 100)],
    )
    line = VisitServiceLineInput(
        service_id=svc_ids[0],
        amount_from_client=float(vs.amount_from_client),
        client_discount_percent=0,
        kanekalon_grams=0,
        kudri_grams=0,
        mix_source=MixSource.NO_MIX,
        mix_complexity=None,
        mix_bonus_master_id=None,
        amortization_level=None,
        kit_kind="STOCK",
        visit_service_id=vs.id,
        comment="Новый комментарий",
    )
    update_visit_with_services(db, visit.id, master_a.id, MultiServiceVisitInput(header=header, lines=[line]))
    db.refresh(vs)
    assert vs.comment == "Новый комментарий"

    after = list(
        db.scalars(
            select(PayrollFundLedger).where(
                PayrollFundLedger.source_kind == PayrollFundSourceKind.VISIT_SERVICE,
                PayrollFundLedger.source_id == vs.id,
            )
        ).all()
    )
    after_storno = sum(1 for r in after if r.entry_kind == PayrollFundEntryKind.STORNO)
    assert after_storno == before_storno


def test_visit_ledger_net_reflects_amount_edit_for_calendar(memory_db):
    """Календарь на главной суммирует нетто по журналу — после правки суммы визита."""
    db = memory_db
    master_a, _master_b, _admin, svc_ids = _seed_users_and_services(db)
    client = db.scalar(select(Client).limit(1))
    visit = _make_visit(db, master_a, client.id, svc_ids[0], amount=1000.0)
    vs = db.scalar(select(VisitService).where(VisitService.visit_id == visit.id))
    assert vs is not None

    before_master = sum_visit_ledger_by_visit_id(
        db,
        side=PayrollFundSide.MASTER,
        visit_ids=[visit.id],
        user_id=master_a.id,
    )[visit.id]
    before_studio = sum_visit_ledger_by_visit_id(
        db,
        side=PayrollFundSide.STUDIO,
        visit_ids=[visit.id],
        user_id=None,
    )[visit.id]

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
        performed_date=visit.performed_date.date(),
        duration_minutes=visit.duration_minutes,
        masters_scope=VisitMastersScope.VISIT,
        same_master_shares_all_services=False,
        visit_master_allocations=[(master_a.id, 100)],
    )
    line = VisitServiceLineInput(
        service_id=svc_ids[0],
        amount_from_client=2000.0,
        client_discount_percent=0,
        kanekalon_grams=0,
        kudri_grams=0,
        mix_source=MixSource.NO_MIX,
        mix_complexity=None,
        mix_bonus_master_id=None,
        amortization_level=None,
        kit_kind="STOCK",
        visit_service_id=vs.id,
    )
    update_visit_with_services(db, visit.id, master_a.id, MultiServiceVisitInput(header=header, lines=[line]))

    after_master = sum_visit_ledger_by_visit_id(
        db,
        side=PayrollFundSide.MASTER,
        visit_ids=[visit.id],
        user_id=master_a.id,
    )[visit.id]
    after_studio = sum_visit_ledger_by_visit_id(
        db,
        side=PayrollFundSide.STUDIO,
        visit_ids=[visit.id],
        user_id=None,
    )[visit.id]

    assert after_master != before_master
    assert after_studio != before_studio
    assert after_master > before_master
    assert after_studio > before_studio
