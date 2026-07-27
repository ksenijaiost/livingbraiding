from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import models as _orm_models  # noqa: F401
from app.db.base import Base
from app.db.models import (
    Client,
    MixComplexity,
    MixSource,
    Service,
    ServiceCategory,
    ServiceSubcategory,
    User,
    UserRole,
    UserRoleAssignment,
    PayrollPeriod,
    VisitClientType,
    VisitMastersScope,
    WorkKind,
    WorkScope,
)
from app.visit_multi_service import (
    MultiServiceVisitInput,
    VisitHeaderInput,
    VisitServiceLineInput,
    save_visit_with_services,
)
from app.work_products_compute import compute_work_financials


def _memory_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    return SessionLocal()


def test_visit_self_mixed_does_not_add_cost_or_bonus() -> None:
    db = _memory_db()
    master = User(
        username="mixmaster",
        password_hash="x",
        display_name="Master",
        role=UserRole.MASTER,
        is_active=True,
    )
    client = Client(name="Client", phone="+79990000000", is_confirmed=True)
    db.add_all([master, client])
    db.flush()
    db.add(UserRoleAssignment(user_id=master.id, role=UserRole.MASTER))
    db.add(
        PayrollPeriod(
            date_from=datetime(2020, 1, 1),
            date_to=datetime(2030, 12, 31, 23, 59, 59),
            closed_at=None,
        )
    )
    cat = ServiceCategory(name="Категория", is_active=True)
    db.add(cat)
    db.flush()
    sub = ServiceSubcategory(category_id=cat.id, name="Подкатегория", is_active=True)
    db.add(sub)
    db.flush()
    svc = Service(subcategory_id=sub.id, name="Услуга", is_active=True)
    db.add(svc)
    db.commit()

    visit = save_visit_with_services(
        db,
        master.id,
        MultiServiceVisitInput(
            header=VisitHeaderInput(
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
                masters_scope=VisitMastersScope.VISIT,
                same_master_shares_all_services=False,
                visit_master_allocations=[(master.id, 100)],
            ),
            lines=[
                VisitServiceLineInput(
                    service_id=svc.id,
                    amount_from_client=5000.0,
                    client_discount_percent=0,
                    kanekalon_grams=100.0,
                    kudri_grams=50.0,
                    mix_source=MixSource.SELF_MIXED,
                    mix_complexity=MixComplexity.STANDARD,
                    mix_bonus_master_id=master.id,
                    amortization_level=None,
                    kit_kind="STOCK",
                )
            ],
        ),
    )

    line = visit.services[0]
    assert line.mix_cost_amount == 0.0
    assert line.mix_bonus_amount == 0.0
    assert visit.mix_cost_amount == 0.0
    assert visit.mix_bonus_amount == 0.0


def test_work_kit_self_mixed_does_not_add_master_pay() -> None:
    db = _memory_db()

    fin = compute_work_financials(
        db,
        kind=WorkKind.KIT,
        scope=WorkScope.IN_STOCK,
        alloc=[(1, 1.0)],
        current_user_id=1,
        mat_cost=120.0,
        kit_totals={},
        kit_staff_ids=[1],
        kit_by_staff={},
        mix_source=MixSource.SELF_MIXED,
        mix_complexity=MixComplexity.STANDARD,
        grams_total=200.0,
        rubber_type="",
        rubber_qty=1,
        corr_trim_qty=0,
        corr_hourly_hours=0.0,
        corr_hourly_avg=False,
        corr_wash=False,
        corr_circle=False,
        corr_steam=False,
        composition_lines=None,
        kit_client_price=0.0,
        amount_from_client=None,
    )

    assert fin.master_total == 0.0
    assert fin.staff_master_profit[1] == 0.0
    assert fin.cost_total_amount == 120.0
    assert fin.studio_total == -0.0
