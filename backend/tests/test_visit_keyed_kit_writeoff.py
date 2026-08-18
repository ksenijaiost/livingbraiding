"""Списание всего остатка комплекта по видам при сохранении/правке визита (1.56)."""

from __future__ import annotations

import json
from datetime import date, datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db import models as _orm_models  # noqa: F401
from app.db.base import Base
from app.db.models import (
    CatalogProduct,
    Client,
    Kit,
    KitBlankStock,
    MixSource,
    PayrollPeriod,
    Service,
    ServiceCategory,
    ServiceSubcategory,
    User,
    UserRole,
    UserRoleAssignment,
    VisitClientType,
    VisitKitUsage,
    VisitMastersScope,
    VisitService,
)
from app.kit_blank_stock_core import blank_stock_qty_map
from app.kit_inlay_visit import StockKitLineInput
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


def _seed(db, *, blank_qty: int = 91):
    master = User(
        username="techspec",
        password_hash="x",
        display_name="Техспец",
        role=UserRole.ADMIN_SUPER,
        is_active=True,
    )
    db.add(master)
    db.flush()
    db.add(UserRoleAssignment(user_id=master.id, role=UserRole.ADMIN_SUPER))
    db.add(UserRoleAssignment(user_id=master.id, role=UserRole.MASTER))
    db.add(
        PayrollPeriod(
            date_from=datetime(2020, 1, 1),
            date_to=datetime(2030, 12, 31, 23, 59, 59),
            closed_at=None,
        )
    )
    cat = ServiceCategory(name="Вся голова", is_active=True)
    db.add(cat)
    db.flush()
    sub = ServiceSubcategory(category_id=cat.id, name="Плетение", is_active=True, show_kit_section=True)
    db.add(sub)
    db.flush()
    svc = Service(subcategory_id=sub.id, name="Наращивание", is_active=True)
    db.add(svc)
    client = Client(name="Оксана", phone="+79990001122", is_confirmed=True)
    db.add(client)
    db.add(
        CatalogProduct(
            is_active=True,
            category_name="Заказ",
            subcategory_name="Заготовки поштучно",
            name="D.E. коса",
            price=170.0,
            meta_json=json.dumps({"kit_key": "DE_BRAID_LONG"}),
        )
    )
    kit = Kit(
        sku="ORDER-26",
        title="Заказ — комплект",
        pieces_total=91,
        pieces_available=blank_qty,
        stock_price_total=15470.0,
        discount_percent=0,
        cost_total=4575.0,
        composition_json=json.dumps(
            [{"key": "DE_BRAID_LONG", "condition": "NEW", "by_staff": {"2": 4, "6": 87}}],
            ensure_ascii=False,
        ),
        created_at=datetime(2026, 8, 14, 16, 1, 58),
        is_in_stock=blank_qty > 0,
        is_active=True,
    )
    db.add(kit)
    db.flush()
    db.add(KitBlankStock(kit_id=kit.id, kit_key="DE_BRAID_LONG", qty=blank_qty))
    db.commit()
    return master, int(svc.id), int(client.id), kit


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
        performed_date=date(2026, 8, 4),
        duration_minutes=420,
        masters_scope=VisitMastersScope.VISIT,
        same_master_shares_all_services=False,
        visit_master_allocations=[(master_id, 100)],
    )


def _stock_line(
    kit_id: int,
    *,
    use_entire: bool,
    blanks_used: int = 0,
    usage_by_key: dict[str, int] | None = None,
) -> VisitServiceLineInput:
    return VisitServiceLineInput(
        service_id=0,
        amount_from_client=5000,
        client_discount_percent=0,
        kanekalon_grams=0,
        kudri_grams=0,
        mix_source=MixSource.NO_MIX,
        mix_complexity=None,
        mix_bonus_master_id=None,
        amortization_level=None,
        kit_kind="STOCK",
        stock_kit_lines=[
            StockKitLineInput(
                kit_id=kit_id,
                use_entire=use_entire,
                blanks_used=blanks_used,
                usage_by_key=usage_by_key,
                amount_from_client=12900,
            )
        ],
    )


def test_save_visit_writes_off_entire_keyed_kit(memory_db) -> None:
    db = memory_db
    master, svc_id, client_id, kit = _seed(db)
    line = _stock_line(int(kit.id), use_entire=True)
    line.service_id = svc_id
    visit = save_visit_with_services(
        db,
        master.id,
        MultiServiceVisitInput(header=_header(client_id, master.id), lines=[line]),
    )
    db.commit()
    db.refresh(kit)
    assert int(kit.pieces_available or 0) == 0
    assert blank_stock_qty_map(db, int(kit.id)).get("DE_BRAID_LONG") == 0
    usages = list(db.scalars(select(VisitKitUsage).where(VisitKitUsage.visit_id == visit.id)).all())
    assert len(usages) == 1
    assert int(usages[0].pieces_used or 0) == 91


def test_save_visit_writes_off_all_keyed_pieces_by_qty(memory_db) -> None:
    db = memory_db
    master, svc_id, client_id, kit = _seed(db)
    line = _stock_line(
        int(kit.id),
        use_entire=False,
        blanks_used=91,
        usage_by_key={"DE_BRAID_LONG": 91},
    )
    line.service_id = svc_id
    save_visit_with_services(
        db,
        master.id,
        MultiServiceVisitInput(header=_header(client_id, master.id), lines=[line]),
    )
    db.commit()
    db.refresh(kit)
    assert int(kit.pieces_available or 0) == 0
    assert blank_stock_qty_map(db, int(kit.id)).get("DE_BRAID_LONG") == 0


def test_edit_visit_rewrites_entire_keyed_kit(memory_db) -> None:
    db = memory_db
    master, svc_id, client_id, kit = _seed(db)
    line = _stock_line(int(kit.id), use_entire=True)
    line.service_id = svc_id
    header = _header(client_id, master.id)
    visit = save_visit_with_services(db, master.id, MultiServiceVisitInput(header=header, lines=[line]))
    db.commit()
    vs = db.scalar(select(VisitService).where(VisitService.visit_id == visit.id))
    assert vs is not None
    line.visit_service_id = vs.id
    line.amount_from_client = 5100
    update_visit_with_services(db, visit.id, master.id, MultiServiceVisitInput(header=header, lines=[line]))
    db.commit()
    db.refresh(kit)
    assert int(kit.pieces_available or 0) == 0
    assert blank_stock_qty_map(db, int(kit.id)).get("DE_BRAID_LONG") == 0


def test_empty_keyed_stock_still_blocked(memory_db) -> None:
    db = memory_db
    master, svc_id, client_id, kit = _seed(db, blank_qty=0)
    line = _stock_line(int(kit.id), use_entire=True)
    line.service_id = svc_id
    with pytest.raises(ValueError, match="остатки по видам"):
        save_visit_with_services(
            db,
            master.id,
            MultiServiceVisitInput(header=_header(client_id, master.id), lines=[line]),
        )
