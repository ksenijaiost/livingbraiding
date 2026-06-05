from __future__ import annotations

from datetime import datetime

import pytest

from app.consultation_types import (
    consultation_kind_for_category_name,
    filter_consultation_catalog_by_types,
    format_types_display,
    parse_types_from_form,
    types_json_dumps,
    validate_types_selected,
)
from app.db.models import ConsultationKind
from app.db.models import (
    Booking,
    BookingKind,
    BookingStatus,
    Client,
    Consultation,
    PayrollFundEntryKind,
    PayrollFundLedger,
    PayrollFundSourceKind,
    User,
    UserRole,
    Visit,
    VisitClientType,
    VisitPriceType,
    WorkForInventory,
    WorkKind,
    WorkScope,
)


def test_types_json_roundtrip() -> None:
    data = parse_types_from_form(["BRAIDING", "OTHER"], "кастом")
    assert validate_types_selected(data) is None
    assert "BRAIDING" in data
    assert data["other_text"] == "кастом"
    disp = format_types_display(types_json_dumps(data))
    assert "Плетение" in disp
    assert "кастом" in disp


def test_validate_types_requires_selection() -> None:
    assert validate_types_selected({}) is not None


def test_consultation_kind_for_category_name() -> None:
    assert consultation_kind_for_category_name("Наращивание") == ConsultationKind.EXTENSION
    assert consultation_kind_for_category_name("Снятие") == ConsultationKind.OTHER
    assert consultation_kind_for_category_name("Уход") == ConsultationKind.OTHER
    assert consultation_kind_for_category_name("Обучение") == ConsultationKind.OTHER
    assert consultation_kind_for_category_name("Вся голова") == ConsultationKind.BRAIDING
    assert consultation_kind_for_category_name("Миниатюра") == ConsultationKind.BRAIDING


def test_filter_consultation_catalog_by_types() -> None:
    catalog = [
        {"id": 1, "name": "Вся голова", "consultation_kind": "BRAIDING", "subcategories": []},
        {"id": 2, "name": "Наращивание", "consultation_kind": "EXTENSION", "subcategories": []},
        {"id": 3, "name": "Уход", "consultation_kind": "OTHER", "subcategories": []},
    ]
    assert [c["name"] for c in filter_consultation_catalog_by_types(catalog, ["BRAIDING"])] == ["Вся голова"]
    assert [c["name"] for c in filter_consultation_catalog_by_types(catalog, ["EXTENSION"])] == ["Наращивание"]
    assert [c["name"] for c in filter_consultation_catalog_by_types(catalog, ["BRAIDING", "OTHER"])] == [
        "Вся голова",
        "Уход",
    ]
    assert filter_consultation_catalog_by_types(catalog, []) == []


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


def _seed_user_client(db) -> tuple[User, Client]:
    u = User(
        username="m1",
        password_hash="x",
        display_name="Master",
        role=UserRole.MASTER,
        is_active=True,
    )
    db.add(u)
    db.flush()
    c = Client(name="Клиент", is_confirmed=True)
    db.add(c)
    db.commit()
    db.refresh(u)
    db.refresh(c)
    return u, c


def test_sum_booking_fulfillment_visit_plus_work(memory_db) -> None:
    from app.payroll_fund import sum_booking_fulfillment_amount

    u, c = _seed_user_client(memory_db)
    cons = Consultation(
        created_at=datetime(2026, 1, 1),
        created_by_user_id=u.id,
        client_id=c.id,
        consultation_date=datetime(2026, 1, 2),
        types_json='{"BRAIDING": true}',
    )
    memory_db.add(cons)
    memory_db.flush()
    b = Booking(
        created_at=datetime(2026, 1, 1),
        created_by_user_id=u.id,
        client_id=c.id,
        planned_date=datetime(2026, 2, 1),
        kind=BookingKind.VISIT,
        status=BookingStatus.DONE,
        consultation_id=cons.id,
    )
    memory_db.add(b)
    memory_db.flush()
    memory_db.add(
        WorkForInventory(
            created_at=datetime(2026, 1, 1),
            created_by_user_id=u.id,
            performed_date=datetime(2026, 2, 1),
            kind=WorkKind.KIT,
            scope=WorkScope.IN_STOCK,
            booking_id=b.id,
            amount_from_client=1000,
            studio_share_snapshot=0.5,
        )
    )
    memory_db.add(
        Visit(
                created_at=datetime(2026, 1, 1),
                client_id=c.id,
                performed_date=datetime(2026, 2, 1),
                booking_id=b.id,
                amount_from_client=4500,
                duration_minutes=60,
                client_type=VisitClientType.NEW,
                price_type=VisitPriceType.CLIENT,
            )
    )
    memory_db.commit()
    assert sum_booking_fulfillment_amount(memory_db, b.id) == 5500


def test_consultation_pay_tiers(memory_db) -> None:
    from sqlalchemy import select

    from app.payroll_fund import post_consultation_accrual

    u, c = _seed_user_client(memory_db)

    def pay_for(amount: int) -> float:
        cons = Consultation(
            created_at=datetime(2026, 1, 1),
            created_by_user_id=u.id,
            client_id=c.id,
            consultation_date=datetime(2026, 1, 2),
            types_json='{"BRAIDING": true}',
        )
        memory_db.add(cons)
        memory_db.flush()
        b = Booking(
            created_at=datetime(2026, 1, 1),
            created_by_user_id=u.id,
            client_id=c.id,
            planned_date=datetime(2026, 2, 1),
            kind=BookingKind.VISIT,
            status=BookingStatus.DONE,
            consultation_id=cons.id,
        )
        memory_db.add(b)
        memory_db.flush()
        memory_db.add(
            Visit(
                created_at=datetime(2026, 1, 1),
                client_id=c.id,
                performed_date=datetime(2026, 2, 1),
                booking_id=b.id,
                amount_from_client=amount,
                duration_minutes=60,
                client_type=VisitClientType.NEW,
                price_type=VisitPriceType.CLIENT,
            )
        )
        memory_db.commit()
        post_consultation_accrual(memory_db, cons.id, u.id)
        memory_db.commit()
        acc = memory_db.scalars(
            select(PayrollFundLedger).where(
                PayrollFundLedger.source_kind == PayrollFundSourceKind.CONSULTATION,
                PayrollFundLedger.source_id == cons.id,
                PayrollFundLedger.entry_kind == PayrollFundEntryKind.ACCRUAL,
            )
        ).first()
        return float(acc.amount) if acc else 0.0

    assert pay_for(4999) == 200.0
    assert pay_for(5000) == 300.0


def test_no_pay_without_visit(memory_db) -> None:
    from app.payroll_fund import post_consultation_accrual
    from sqlalchemy import select

    u, c = _seed_user_client(memory_db)
    cons = Consultation(
        created_at=datetime(2026, 1, 1),
        created_by_user_id=u.id,
        client_id=c.id,
        consultation_date=datetime(2026, 1, 2),
        types_json='{"BRAIDING": true}',
    )
    memory_db.add(cons)
    memory_db.flush()
    b = Booking(
        created_at=datetime(2026, 1, 1),
        created_by_user_id=u.id,
        client_id=c.id,
        planned_date=datetime(2026, 2, 1),
        kind=BookingKind.VISIT,
        status=BookingStatus.ACTIVE,
        consultation_id=cons.id,
    )
    memory_db.add(b)
    memory_db.commit()
    post_consultation_accrual(memory_db, cons.id, u.id)
    memory_db.commit()
    acc = memory_db.scalars(
        select(PayrollFundLedger).where(
            PayrollFundLedger.source_kind == PayrollFundSourceKind.CONSULTATION,
            PayrollFundLedger.source_id == cons.id,
        )
    ).first()
    assert acc is None


def test_prefill_booking_from_consultation(memory_db) -> None:
    from app.routes.bookings import _prefill_booking_fp_from_consultation

    u, c = _seed_user_client(memory_db)
    cons = Consultation(
        created_at=datetime(2026, 1, 1),
        created_by_user_id=u.id,
        client_id=c.id,
        consultation_date=datetime(2026, 1, 2),
        types_json='{"EXTENSION": true}',
        preliminary_cost_text="от 8000",
        comment="хочет в пятницу",
        photo_1="/media/x1.jpg",
    )
    memory_db.add(cons)
    memory_db.commit()
    fp: dict[str, str] = {}
    _prefill_booking_fp_from_consultation(memory_db, cons, fp)
    assert fp["client_id"] == str(c.id)
    assert fp["quoted_price_text"] == "от 8000"
    assert fp["prefill_photo_1"] == "/media/x1.jpg"
