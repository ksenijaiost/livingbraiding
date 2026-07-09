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
    VisitAuditLog,
    VisitClientType,
    VisitMaster,
    VisitMastersScope,
    VisitService,
    VisitServiceMaster,
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
    for name in ("Услуга A", "Услуга B"):
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


def test_update_visit_writes_audit_rows(memory_db):
    db = memory_db
    master_a, _master_b, _admin, svc_ids = _seed_users_and_services(db)
    client = db.scalar(select(Client).limit(1))
    visit = _make_visit(db, master_a, client.id, svc_ids[0])
    vs = db.scalar(select(VisitService).where(VisitService.visit_id == visit.id))
    assert vs is not None

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
    rows = db.scalars(select(VisitAuditLog).where(VisitAuditLog.visit_id == visit.id)).all()
    assert rows
    assert any(r.field_name == "amount_from_client" for r in rows)


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


class _FakeForm:
    def __init__(self, data: dict):
        self._data = data

    def keys(self):
        return self._data.keys()

    def get(self, key):
        val = self._data.get(key)
        if isinstance(val, list):
            return val[0] if val else None
        return val

    def getlist(self, key):
        val = self._data.get(key)
        if val is None:
            return []
        return val if isinstance(val, list) else [val]


def test_parse_line_kit_kind_uses_last_radio_value(memory_db):
    from app.visit_multi_service import _parse_line_from_form

    form = _FakeForm(
        {
            "line_1_service_id": "1",
            "line_1_kit_kind": ["STOCK", "OWN"],
            "line_1_mix_source": ["NO_MIX", "NO_MIX"],
            "line_1_amortization_level": ["MIN"],
            "line_1_amount_from_client": "5000",
        }
    )
    line = _parse_line_from_form(form, 1)
    assert line.kit_kind == "OWN"
    assert line.amount_from_client == 5000.0


def test_parse_line_questionnaire_from_prefixed_fields():
    from app.questionnaire.answer_validate import extract_line_questionnaire_raw_from_form
    from app.visit_multi_service import _parse_line_from_form

    form = _FakeForm(
        {
            "line_1_service_id": "1",
            "line_1_q_bases_count": "38",
            "line_1_q_blanks_count": "12",
            "line_1_kit_kind": "STOCK",
            "line_1_mix_source": "NO_MIX",
            "line_1_amortization_level": "MIN",
            "line_1_amount_from_client": "5000",
        }
    )
    assert extract_line_questionnaire_raw_from_form(form, 1) == {
        "bases_count": "38",
        "blanks_count": "12",
    }
    line = _parse_line_from_form(form, 1)
    assert line.questionnaire_raw == {"bases_count": "38", "blanks_count": "12"}


def test_parse_line0_questionnaire_merges_legacy_q_fields():
    from app.visit_multi_service import _parse_line_from_form

    form = _FakeForm(
        {
            "service_id": "1",
            "q_bases_count": "40",
            "q_blanks_count": "15",
            "kit_kind": "STOCK",
            "mix_source": "NO_MIX",
            "amortization_level": "MIN",
            "amount_from_client": "6000",
        }
    )
    line = _parse_line_from_form(form, 0)
    assert line.questionnaire_raw == {"bases_count": "40", "blanks_count": "15"}


def test_own_kit_line_without_stock_succeeds(memory_db):
    from app.db.models import AmortizationLevel, MixSource, ServiceSubcategory
    from app.visit_multi_service import VisitHeaderInput, VisitServiceLineInput, compute_visit_service_line

    db = memory_db
    master_a, _master_b, _admin, svc_ids = _seed_users_and_services(db)
    svc = db.get(Service, svc_ids[0])
    sub = db.get(ServiceSubcategory, svc.subcategory_id)
    sub.show_kit_section = True
    db.commit()

    client = db.scalar(select(Client).limit(1))
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
        performed_date=datetime.utcnow().date(),
        duration_minutes=60,
        masters_scope=VisitMastersScope.VISIT,
        same_master_shares_all_services=False,
        visit_master_allocations=[(master_a.id, 100)],
    )
    line = VisitServiceLineInput(
        service_id=svc_ids[0],
        amount_from_client=5000.0,
        client_discount_percent=0,
        kanekalon_grams=100.0,
        kudri_grams=0.0,
        mix_source=MixSource.NO_MIX,
        mix_complexity=None,
        mix_bonus_master_id=None,
        amortization_level=AmortizationLevel.MIN,
        kit_kind="OWN",
        stock_kit_lines=[],
        own_origin="STUDIO",
        own_correction=False,
        own_extra_blanks=False,
    )
    computed = compute_visit_service_line(db, line, header, default_mix_bonus_master_id=master_a.id, apply_kit_stock=False)
    assert computed.amount_from_client == 5000.0
    assert computed.amortization_amount > 0


def test_update_visit_adds_second_service_without_dropping_first(memory_db):
    db = memory_db
    master_a, _master_b, _admin, svc_ids = _seed_users_and_services(db)
    client = db.scalar(select(Client).limit(1))
    visit = _make_visit(db, master_a, client.id, svc_ids[0], amount=3000.0)
    vs0 = db.scalar(
        select(VisitService).where(VisitService.visit_id == visit.id, VisitService.is_cancelled.is_(False))
    )
    assert vs0 is not None

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
        performed_date=visit.performed_date.date() if visit.performed_date else datetime.utcnow().date(),
        duration_minutes=60,
        masters_scope=VisitMastersScope.VISIT,
        same_master_shares_all_services=False,
        visit_master_allocations=[(master_a.id, 100)],
    )
    multi = MultiServiceVisitInput(
        header=header,
        lines=[
            VisitServiceLineInput(
                service_id=svc_ids[0],
                amount_from_client=3000.0,
                client_discount_percent=0,
                kanekalon_grams=0,
                kudri_grams=0,
                mix_source=MixSource.NO_MIX,
                mix_complexity=None,
                mix_bonus_master_id=None,
                amortization_level=None,
                visit_service_id=vs0.id,
                kit_kind="STOCK",
            ),
            VisitServiceLineInput(
                service_id=svc_ids[1],
                amount_from_client=2000.0,
                client_discount_percent=0,
                kanekalon_grams=0,
                kudri_grams=0,
                mix_source=MixSource.NO_MIX,
                mix_complexity=None,
                mix_bonus_master_id=None,
                amortization_level=None,
                kit_kind="STOCK",
            ),
        ],
    )
    update_visit_with_services(db, visit.id, master_a.id, multi)

    active = db.scalars(
        select(VisitService)
        .where(VisitService.visit_id == visit.id, VisitService.is_cancelled.is_(False))
        .order_by(VisitService.sort_order.asc(), VisitService.id.asc())
    ).all()
    assert len(active) == 2
    assert {int(vs.service_id) for vs in active} == {svc_ids[0], svc_ids[1]}
    assert active[0].id == vs0.id
    assert float(active[0].amount_from_client or 0) == 3000.0
    assert float(active[1].amount_from_client or 0) == 2000.0


def test_update_per_service_masters_replaces_existing_rows(memory_db):
    """Повторное сохранение PER_SERVICE не должно дублировать visit_service_masters."""
    db = memory_db
    master_a, master_b, _admin, svc_ids = _seed_users_and_services(db)
    client = db.scalar(select(Client).limit(1))

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
        performed_date=datetime.utcnow().date(),
        duration_minutes=60,
        masters_scope=VisitMastersScope.PER_SERVICE,
        same_master_shares_all_services=False,
        visit_master_allocations=[],
    )
    line = VisitServiceLineInput(
        service_id=svc_ids[0],
        amount_from_client=3000.0,
        client_discount_percent=0,
        kanekalon_grams=0,
        kudri_grams=0,
        mix_source=MixSource.NO_MIX,
        mix_complexity=None,
        mix_bonus_master_id=None,
        amortization_level=None,
        kit_kind="STOCK",
        service_master_allocations=[(master_a.id, 100)],
    )
    visit = save_visit_with_services(db, master_a.id, MultiServiceVisitInput(header=header, lines=[line]))
    vs = db.scalar(select(VisitService).where(VisitService.visit_id == visit.id))
    assert vs is not None

    update_visit_with_services(
        db,
        visit.id,
        master_a.id,
        MultiServiceVisitInput(
            header=header,
            lines=[
                VisitServiceLineInput(
                    service_id=svc_ids[0],
                    amount_from_client=3000.0,
                    client_discount_percent=0,
                    kanekalon_grams=0,
                    kudri_grams=0,
                    mix_source=MixSource.NO_MIX,
                    mix_complexity=None,
                    mix_bonus_master_id=None,
                    amortization_level=None,
                    kit_kind="STOCK",
                    visit_service_id=vs.id,
                    service_master_allocations=[(master_a.id, 50), (master_b.id, 50)],
                ),
            ],
        ),
    )

    masters = db.scalars(
        select(VisitServiceMaster)
        .where(VisitServiceMaster.visit_service_id == vs.id)
        .order_by(VisitServiceMaster.master_id.asc())
    ).all()
    assert len(masters) == 2
    assert [(int(m.master_id), int(m.percent or 0)) for m in masters] == [
        (master_a.id, 50),
        (master_b.id, 50),
    ]
