"""Черновики работы с товарами."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.auth import AuthUser
from app.db.models import (
    Client,
    PayrollPeriod,
    User,
    UserRole,
    UserRoleAssignment,
    WorkDraft,
    WorkDraftParticipant,
    WorkKind,
    WorkScope,
)
from app.work_draft import (
    acquire_draft_lock,
    draft_counts_by_day,
    extract_participant_master_ids,
    link_finalized_work,
    list_open_drafts_for_master,
    save_work_draft,
    user_can_view_draft,
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


def _seed_masters(db) -> tuple[User, User, User]:
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
    db.add(
        PayrollPeriod(
            date_from=datetime(2020, 1, 1),
            date_to=datetime(2030, 12, 31, 23, 59, 59),
            closed_at=None,
        )
    )
    client = Client(name="Клиент", phone="79001234567", is_confirmed=True)
    db.add(client)
    db.commit()
    return m1, m2, m3


def test_save_draft_requires_date(memory_db):
    m1, _, _ = _seed_masters(memory_db)
    with pytest.raises(ValueError, match="дату"):
        save_work_draft(
            memory_db,
            None,
            {"kind": WorkKind.MIX.value, "scope": WorkScope.IN_STOCK.value},
            m1.id,
        )


def test_participants_visibility_kit_multi(memory_db):
    m1, m2, m3 = _seed_masters(memory_db)
    form = {
        "performed_date": "2026-05-15",
        "kind": WorkKind.KIT.value,
        "scope": WorkScope.IN_STOCK.value,
        "kit_use_multi_masters": "on",
        "kit_master_on": f"{m1.id},{m2.id}",
    }
    draft = save_work_draft(memory_db, None, form, m1.id)
    memory_db.commit()
    pids = {p.master_id for p in draft.participants}
    assert m1.id in pids and m2.id in pids
    assert m3.id not in pids

    assert len(list_open_drafts_for_master(memory_db, m1.id)) == 1
    assert len(list_open_drafts_for_master(memory_db, m2.id)) == 1
    assert len(list_open_drafts_for_master(memory_db, m3.id)) == 0

    auth_m3 = AuthUser(
        id=m3.id, username="m3", role=UserRole.MASTER, display_name="M3", roles=(UserRole.MASTER,)
    )
    assert not user_can_view_draft(auth_m3, draft, memory_db)
    auth_m1 = AuthUser(
        id=m1.id, username="m1", role=UserRole.MASTER, display_name="M1", roles=(UserRole.MASTER,)
    )
    assert user_can_view_draft(auth_m1, draft, memory_db)


def test_extract_participants_single_master(memory_db):
    m1, _, _ = _seed_masters(memory_db)
    form = {
        "kind": WorkKind.MIX.value,
        "scope": WorkScope.IN_STOCK.value,
    }
    assert extract_participant_master_ids(form, current_user_id=m1.id, kind=WorkKind.MIX) == [m1.id]


def test_lock_blocks_other_master(memory_db):
    m1, m2, _ = _seed_masters(memory_db)
    form = {
        "performed_date": "2026-05-15",
        "kind": WorkKind.MIX.value,
        "scope": WorkScope.IN_STOCK.value,
    }
    draft = save_work_draft(memory_db, None, form, m1.id)
    # Make m2 also a participant
    memory_db.add(WorkDraftParticipant(work_draft_id=draft.id, master_id=m2.id))
    memory_db.commit()

    lock1 = acquire_draft_lock(memory_db, draft, m1.id)
    assert not lock1.readonly
    lock2 = acquire_draft_lock(memory_db, draft, m2.id)
    assert lock2.readonly
    assert lock2.lock_holder and lock2.lock_holder.id == m1.id

    draft.locked_at = utcnow_naive() - timedelta(minutes=31)
    memory_db.flush()
    lock3 = acquire_draft_lock(memory_db, draft, m2.id)
    assert not lock3.readonly


def test_link_finalized_hides_from_list(memory_db):
    from app.db.models import WorkForInventory

    m1, _, _ = _seed_masters(memory_db)
    form = {
        "performed_date": "2026-05-15",
        "kind": WorkKind.MIX.value,
        "scope": WorkScope.IN_STOCK.value,
        "kanekalon_grams": "10",
        "mix_complexity": "STANDARD",
    }
    draft = save_work_draft(memory_db, None, form, m1.id)
    work = WorkForInventory(
        created_at=utcnow_naive(),
        created_by_user_id=m1.id,
        performed_date=datetime(2026, 5, 15),
        kind=WorkKind.MIX,
        scope=WorkScope.IN_STOCK,
        kanekalon_grams=10.0,
        kudri_grams=0.0,
        materials_cost_total=0.0,
        extra_costs_amount=0.0,
        cost_total_amount=0.0,
        master_profit_amount=0.0,
        studio_profit_amount=0.0,
        profit_total_amount=0.0,
        studio_share_snapshot=0,
        is_voided=False,
    )
    memory_db.add(work)
    memory_db.flush()
    link_finalized_work(memory_db, draft.id, work.id, m1.id)
    memory_db.commit()
    assert draft.finalized_work_id == work.id
    assert list_open_drafts_for_master(memory_db, m1.id) == []


def test_draft_counts_by_day(memory_db):
    m1, _, _ = _seed_masters(memory_db)
    form = {
        "performed_date": "2026-05-15",
        "kind": WorkKind.MIX.value,
        "scope": WorkScope.IN_STOCK.value,
    }
    save_work_draft(memory_db, None, form, m1.id)
    memory_db.commit()
    auth = AuthUser(
        id=m1.id, username="m1", role=UserRole.MASTER, display_name="M1", roles=(UserRole.MASTER,)
    )
    counts = draft_counts_by_day(
        memory_db,
        user=auth,
        day_from=date(2026, 5, 1),
        day_to_excl=date(2026, 6, 1),
    )
    assert counts.get(date(2026, 5, 15), 0) == 1
