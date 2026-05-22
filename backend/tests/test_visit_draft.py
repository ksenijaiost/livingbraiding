"""Черновики визита."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.auth import AuthUser
from app.db.models import (
    Client,
    PayrollPeriod,
    Service,
    ServiceCategory,
    ServiceSubcategory,
    User,
    UserRole,
    UserRoleAssignment,
    Visit,
    VisitDraft,
    VisitDraftParticipant,
    VisitService,
    VisitClientType,
    VisitMastersScope,
)
from app.visit_draft import (
    acquire_draft_lock,
    compute_draft_preview,
    draft_counts_by_day,
    extract_participant_master_ids,
    finalize_visit_draft,
    save_visit_draft,
    user_can_view_draft,
)
from app.visit_multi_service import (
    MultiServiceVisitInput,
    VisitHeaderInput,
    VisitServiceLineInput,
    parse_multi_service_visit_form,
)
from app.time_utils import utcnow_naive


@pytest.fixture()
def memory_db():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.db import models as _orm_models  # noqa: F401
    from app.db.base import Base

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as db:
        yield db


def _seed_users_and_service(db) -> tuple[User, User, User, int]:
    def _master(username: str, name: str) -> User:
        u = User(
            username=username,
            password_hash="x",
            display_name=name,
            role=UserRole.MASTER,
            is_active=True,
        )
        db.add(u)
        db.flush()
        db.add(UserRoleAssignment(user_id=u.id, role=UserRole.MASTER))
        return u

    m1 = _master("m1", "Master One")
    m2 = _master("m2", "Master Two")
    m3 = _master("m3", "Master Three")
    admin = User(
        username="adm",
        password_hash="x",
        display_name="Admin",
        role=UserRole.ADMIN,
        is_active=True,
    )
    db.add(admin)
    db.flush()
    db.add(UserRoleAssignment(user_id=admin.id, role=UserRole.ADMIN))
    db.add(
        PayrollPeriod(
            date_from=datetime(2020, 1, 1),
            date_to=datetime(2030, 12, 31, 23, 59, 59),
            closed_at=None,
        )
    )
    client = Client(name="Клиент", phone="79001234567", is_confirmed=True)
    db.add(client)
    db.flush()
    cat = ServiceCategory(name="Вся голова", is_active=True)
    db.add(cat)
    db.flush()
    sub = ServiceSubcategory(category_id=cat.id, name="Плетение", is_active=True)
    db.add(sub)
    db.flush()
    svc = Service(subcategory_id=sub.id, name="Service A", is_active=True)
    db.add(svc)
    db.flush()
    db.commit()
    return m1, m2, m3, int(svc.id)


def _visit_inp(client_id: int, service_id: int, master_ids: list[tuple[int, int]]) -> MultiServiceVisitInput:
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
        performed_date=date(2026, 5, 15),
        duration_minutes=60,
        masters_scope=VisitMastersScope.VISIT,
        same_master_shares_all_services=True,
        visit_master_allocations=master_ids,
    )
    line = VisitServiceLineInput(
        service_id=service_id,
        amount_from_client=5000.0,
        client_discount_percent=0,
        kanekalon_grams=0.0,
        kudri_grams=0.0,
        mix_source=None,
        mix_complexity=None,
        mix_bonus_master_id=None,
        amortization_level=None,
        kit_kind="STOCK",
    )
    return MultiServiceVisitInput(header=header, lines=[line])


def test_save_draft_requires_date(memory_db):
    m1, _, _, sid = _seed_users_and_service(memory_db)
    client = memory_db.scalar(select(Client).limit(1))
    inp = _visit_inp(int(client.id), sid, [(m1.id, 100)])
    with pytest.raises(ValueError, match="дату"):
        save_visit_draft(memory_db, None, inp, m1.id, {}, created_by_label="test")


def test_participants_visibility(memory_db):
    m1, m2, m3, sid = _seed_users_and_service(memory_db)
    client = memory_db.scalar(select(Client).limit(1))
    inp = _visit_inp(int(client.id), sid, [(m1.id, 50), (m2.id, 50)])
    draft = save_visit_draft(
        memory_db,
        None,
        inp,
        m1.id,
        {"performed_date": "2026-05-15"},
        created_by_label="test",
    )
    memory_db.commit()
    pids = {p.master_id for p in draft.participants}
    assert m1.id in pids and m2.id in pids
    assert m3.id not in pids

    drafts_m1 = list(
        memory_db.scalars(
            select(VisitDraft)
            .join(VisitDraftParticipant)
            .where(VisitDraftParticipant.master_id == m1.id, VisitDraft.finalized_visit_id.is_(None))
        ).all()
    )
    drafts_m3 = list(
        memory_db.scalars(
            select(VisitDraft)
            .join(VisitDraftParticipant)
            .where(VisitDraftParticipant.master_id == m3.id, VisitDraft.finalized_visit_id.is_(None))
        ).all()
    )
    assert len(drafts_m1) == 1
    assert len(drafts_m3) == 0


def test_lock_readonly_for_other_master(memory_db):
    m1, m2, _, sid = _seed_users_and_service(memory_db)
    client = memory_db.scalar(select(Client).limit(1))
    inp = _visit_inp(int(client.id), sid, [(m1.id, 100)])
    draft = save_visit_draft(
        memory_db,
        None,
        inp,
        m1.id,
        {"performed_date": "2026-05-15"},
        created_by_label="test",
    )
    memory_db.commit()
    draft.locked_by_user_id = m1.id
    draft.locked_at = utcnow_naive()
    memory_db.commit()
    lock = acquire_draft_lock(memory_db, draft, m2.id)
    assert lock.readonly is True
    assert lock.lock_holder is not None
    assert lock.lock_holder.id == m1.id


def test_finalize_creates_visit_without_preview_stock(memory_db):
    m1, _, _, sid = _seed_users_and_service(memory_db)
    client = memory_db.scalar(select(Client).limit(1))
    inp = _visit_inp(int(client.id), sid, [(m1.id, 100)])
    apply_calls: list[int] = []

    def _spy_apply(db, **kwargs):
        apply_calls.append(1)
        return (1, 100.0, 10.0, {})

    with patch("app.visit_multi_service._apply_stock_kit_usage", side_effect=_spy_apply):
        preview = compute_draft_preview(memory_db, inp)
    assert apply_calls == []

    draft = save_visit_draft(
        memory_db,
        None,
        inp,
        m1.id,
        {"performed_date": "2026-05-15"},
        created_by_label="test",
    )
    memory_db.commit()
    apply_calls.clear()
    with patch("app.visit_multi_service._apply_stock_kit_usage", side_effect=_spy_apply):
        visit_id = finalize_visit_draft(memory_db, int(draft.id), inp, m1.id, created_by_label="test")
    memory_db.commit()
    draft = memory_db.get(VisitDraft, draft.id)
    assert draft.finalized_visit_id == visit_id
    visit = memory_db.scalar(select(Visit).where(Visit.id == visit_id).options(selectinload(Visit.services)))
    assert visit is not None
    assert len(visit.services) >= 1


def test_admin_sees_draft_count_on_calendar_day(memory_db):
    m1, m2, _, sid = _seed_users_and_service(memory_db)
    admin = memory_db.scalar(select(User).where(User.username == "adm"))
    client = memory_db.scalar(select(Client).limit(1))
    inp = _visit_inp(int(client.id), sid, [(m1.id, 100)])
    inp.header.performed_date = date(2026, 5, 20)
    save_visit_draft(
        memory_db,
        None,
        inp,
        m1.id,
        {"performed_date": "2026-05-20"},
        created_by_label="test",
    )
    memory_db.commit()
    auth_admin = AuthUser(
        id=admin.id,
        username=admin.username,
        display_name=admin.display_name,
        role=UserRole.ADMIN,
        roles=(UserRole.ADMIN,),
    )
    counts = draft_counts_by_day(
        memory_db,
        user=auth_admin,
        day_from=date(2026, 5, 1),
        day_to_excl=date(2026, 6, 1),
    )
    assert counts.get(date(2026, 5, 20)) == 1
