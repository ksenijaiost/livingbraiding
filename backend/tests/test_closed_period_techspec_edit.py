"""1.12.1: техспец может править закрытый период ЗП (с ack)."""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.auth import AuthUser
from app.db import models as _orm_models  # noqa: F401
from app.db.base import Base
from app.db.models import (
    Client,
    PayrollPeriod,
    User,
    UserRole,
    UserRoleAssignment,
    Visit,
    VisitClientType,
    VisitPriceType,
)
from app.visit_edit_policy import (
    CLOSED_PERIOD_ACK_VALUE,
    ensure_event_date_in_open_payroll_period,
    is_in_closed_payroll_period,
    require_closed_period_ack,
    user_may_edit_closed_payroll_period,
    visit_edit_policy,
)


@pytest.fixture()
def memory_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as db:
        yield db


def _auth(user: User, *roles: UserRole) -> AuthUser:
    rs = list(roles) if roles else [user.role]
    return AuthUser(
        id=user.id,
        username=user.username,
        display_name=user.display_name,
        role=rs[0],
        roles=tuple(rs),
        master_level=None,
    )


def test_only_techspec_may_edit_closed_period(memory_db) -> None:
    db = memory_db
    tech = User(username="t", password_hash="x", display_name="T", role=UserRole.TECHSPEC, is_active=True)
    super_a = User(username="s", password_hash="x", display_name="S", role=UserRole.ADMIN_SUPER, is_active=True)
    db.add_all([tech, super_a])
    db.flush()
    db.add(UserRoleAssignment(user_id=tech.id, role=UserRole.TECHSPEC))
    db.add(UserRoleAssignment(user_id=super_a.id, role=UserRole.ADMIN_SUPER))
    db.add(
        PayrollPeriod(
            date_from=datetime(2026, 6, 1),
            date_to=datetime(2026, 6, 30, 23, 59, 59),
            closed_at=datetime(2026, 7, 1),
        )
    )
    client = Client(name="C", phone="+79990001111", is_confirmed=True)
    db.add(client)
    db.flush()
    visit = Visit(
        created_by_user_id=tech.id,
        performed_date=datetime(2026, 6, 4),
        duration_minutes=60,
        client_id=client.id,
        client_type=VisitClientType.RETURNING,
        price_type=VisitPriceType.CLIENT,
        client_discount_percent=0,
        amount_from_client=100,
        cost_total=0,
        profit_before_split=100,
        salon_profit=50,
        masters_pool=50,
        studio_fund_amount=0,
        materials_cost_total=0,
        mix_cost_amount=0,
        mix_bonus_amount=0,
        kanekalon_grams=0,
        kudri_grams=0,
    )
    db.add(visit)
    db.commit()
    db.refresh(visit)

    assert user_may_edit_closed_payroll_period(_auth(tech, UserRole.TECHSPEC))
    assert not user_may_edit_closed_payroll_period(_auth(super_a, UserRole.ADMIN_SUPER))
    assert visit_edit_policy(visit, _auth(tech, UserRole.TECHSPEC), db).can_edit
    assert not visit_edit_policy(visit, _auth(super_a, UserRole.ADMIN_SUPER), db).can_edit


def test_techspec_payout_date_allow_closed(memory_db) -> None:
    """ensure(..., allow_closed=True) пропускает дату в закрытом периоде — как выплата техспеца."""
    db = memory_db
    db.add(
        PayrollPeriod(
            date_from=datetime(2026, 6, 1),
            date_to=datetime(2026, 6, 30, 23, 59, 59),
            closed_at=datetime(2026, 7, 1),
        )
    )
    db.add(
        PayrollPeriod(
            date_from=datetime(2026, 7, 1),
            date_to=datetime(2026, 7, 31, 23, 59, 59),
            closed_at=None,
        )
    )
    db.commit()
    event = datetime(2026, 6, 12)
    assert is_in_closed_payroll_period(db, event)
    with pytest.raises(ValueError):
        ensure_event_date_in_open_payroll_period(db, event, allow_closed=False)
    ensure_event_date_in_open_payroll_period(db, event, allow_closed=True)


def test_closed_period_ack_and_ensure(memory_db) -> None:
    db = memory_db
    db.add(
        PayrollPeriod(
            date_from=datetime(2026, 6, 1),
            date_to=datetime(2026, 6, 30, 23, 59, 59),
            closed_at=datetime(2026, 7, 1),
        )
    )
    db.add(
        PayrollPeriod(
            date_from=datetime(2026, 7, 1),
            date_to=datetime(2026, 7, 31, 23, 59, 59),
            closed_at=None,
        )
    )
    db.commit()
    event = datetime(2026, 6, 15)
    with pytest.raises(ValueError):
        ensure_event_date_in_open_payroll_period(db, event)
    ensure_event_date_in_open_payroll_period(db, event, allow_closed=True)

    with pytest.raises(ValueError):
        require_closed_period_ack(needed=True, form_ack="")
    require_closed_period_ack(needed=True, form_ack=CLOSED_PERIOD_ACK_VALUE)
    require_closed_period_ack(needed=False, form_ack="")
