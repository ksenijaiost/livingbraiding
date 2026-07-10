"""Fix 128: откат списания комплекта при редактировании визита."""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db import models as _orm_models  # noqa: F401
from app.db.base import Base
from app.db.models import (
    Client,
    Kit,
    KitBlankStock,
    PayrollPeriod,
    User,
    UserRole,
    UserRoleAssignment,
    Visit,
    VisitClientType,
    VisitKitUsage,
    VisitPriceType,
    VisitService,
)
from app.kit_blank_stock_core import blank_stock_qty_map, planned_kit_stock_revert_pieces
from app.visit_stock import visit_service_revert_stock


@pytest.fixture()
def memory_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as db:
        yield db


def _seed(db) -> tuple[User, Visit, VisitService]:
    db.add(
        PayrollPeriod(
            date_from=datetime(2020, 1, 1),
            date_to=datetime(2030, 12, 31, 23, 59, 59),
            closed_at=None,
        )
    )
    u = User(
        username="admin",
        password_hash="x",
        display_name="Admin",
        role=UserRole.ADMIN_SUPER,
        is_active=True,
    )
    db.add(u)
    db.flush()
    db.add(UserRoleAssignment(user_id=u.id, role=UserRole.ADMIN_SUPER))
    client = Client(name="Клиент", phone="+79990001122", is_confirmed=True)
    db.add(client)
    db.flush()
    kit = Kit(
        sku="212",
        title="Test kit",
        is_active=True,
        pieces_total=37,
        pieces_available=37,
        composition_json='[{"key":"DE_THERMO_CURL","condition":"NEW","by_staff":{"1":37}}]',
        stock_price_total=1000.0,
        cost_total=500.0,
        is_in_stock=True,
        created_at=datetime.utcnow(),
    )
    db.add(kit)
    db.flush()
    db.add(KitBlankStock(kit_id=kit.id, kit_key="DE_THERMO_CURL", qty=37))
    visit = Visit(
        created_by_user_id=u.id,
        client_id=client.id,
        client_type=VisitClientType.RETURNING,
        price_type=VisitPriceType.CLIENT,
        performed_date=datetime(2026, 7, 7),
        duration_minutes=150,
        amount_from_client=5000,
        cost_total=0,
        profit_before_split=0,
        salon_profit=0,
        masters_pool=0,
        created_at=datetime.utcnow(),
    )
    db.add(visit)
    db.flush()
    vs = VisitService(
        visit_id=visit.id,
        service_id=1,
        category_name="Cat",
        subcategory_name="Sub",
        service_name="Svc",
        sort_order=1,
        amount_from_client=300,
        cost_total=0,
        profit_before_split=0,
        salon_profit=0,
        masters_pool=0,
    )
    db.add(vs)
    db.flush()
    db.add(
        VisitKitUsage(
            visit_id=visit.id,
            visit_service_id=vs.id,
            kit_id=kit.id,
            pieces_used=10,
            cost_amount=100.0,
            usage_breakdown_json=None,
        )
    )
    db.commit()
    return u, visit, vs


def test_planned_revert_zero_when_stock_already_full(memory_db) -> None:
    _, _, _ = _seed(memory_db)
    kit = memory_db.scalars(select(Kit)).first()
    assert kit is not None
    pieces, bd = planned_kit_stock_revert_pieces(memory_db, kit, 10, None)
    assert pieces == 0
    assert bd is None


def test_visit_service_revert_stock_clears_orphan_usage(memory_db) -> None:
    _, _, vs = _seed(memory_db)
    kit = memory_db.scalars(select(Kit)).first()
    assert kit is not None
    assert int(kit.pieces_available or 0) == 37

    ok, err = visit_service_revert_stock(memory_db, vs.id)
    memory_db.commit()
    assert ok, err
    assert int(kit.pieces_available or 0) == 37
    assert blank_stock_qty_map(memory_db, int(kit.id)).get("DE_THERMO_CURL") == 37
    usages = list(memory_db.scalars(select(VisitKitUsage).where(VisitKitUsage.visit_service_id == vs.id)).all())
    assert usages == []


def test_visit_service_revert_stock_returns_decremented_stock(memory_db) -> None:
    _, _, vs = _seed(memory_db)
    kit = memory_db.scalars(select(Kit)).first()
    assert kit is not None
    kit.pieces_available = 27
    row = memory_db.scalar(
        select(KitBlankStock).where(KitBlankStock.kit_id == kit.id, KitBlankStock.kit_key == "DE_THERMO_CURL")
    )
    assert row is not None
    row.qty = 27
    memory_db.commit()

    ok, err = visit_service_revert_stock(memory_db, vs.id)
    memory_db.commit()
    assert ok, err
    assert int(kit.pieces_available or 0) == 37
    assert blank_stock_qty_map(memory_db, int(kit.id)).get("DE_THERMO_CURL") == 37
