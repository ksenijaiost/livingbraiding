from __future__ import annotations

import json
from datetime import datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.db import models as _orm_models  # noqa: F401 — register models on Base
from app.db.base import Base
from app.db.models import CatalogProduct, Client, Kit, KitBlankStock, KitBlanksCondition, KitReserve, User, UserRole
from app.kit_blank_stock_core import (
    blank_stock_qty_map,
    build_usage_breakdown_keyed,
    decrement_blank_stock_keys,
    distribute_scalar_to_keys,
    ensure_blank_stock_from_composition,
    infer_kit_blanks_condition_from_totals,
    keyed_cost_selected,
    release_client_kit_reserves_into_free_pool,
    repair_all_kits_pieces_available_from_blank_stock,
    repair_kit_blank_stock_reserve_desync,
    require_composition_stock_rows_or_scalar_ok,
    reserve_kit_stock_for_client,
    sync_kit_pieces_available_from_blank_lines,
)


def _catalog_blank(db: Session, kit_key: str, *, is_bu: bool = False) -> None:
    db.add(
        CatalogProduct(
            is_active=True,
            category_name="Заказ",
            subcategory_name="Заготовки поштучно",
            name=kit_key,
            price=10.0,
            meta_json=json.dumps({"kit_key": kit_key, "is_bu": is_bu}),
        )
    )


def test_distribute_scalar_to_keys_matches_weights() -> None:
    comp = {"DE": 2, "SE": 1}
    got = distribute_scalar_to_keys(comp, 5)
    assert got == {"DE": 3, "SE": 2}


def test_keyed_cost_selected_linear_in_piece_count() -> None:
    comp = {"DE": 2, "SE": 1}
    per_piece_share = 60.0 / 3.0  # cost_total / sum(comp)
    assert keyed_cost_selected({"DE": 2}, comp=comp, kit_cost_total=60.0) == pytest.approx(2 * per_piece_share)
    assert keyed_cost_selected({"DE": 1, "SE": 2}, comp=comp, kit_cost_total=60.0) == pytest.approx(3 * per_piece_share)


def test_build_usage_breakdown_keyed_explicit_usage() -> None:
    max_by = {"DE": 5, "SE": 3}
    bd = build_usage_breakdown_keyed(
        use_entire=False,
        blanks_used=0,
        usage_by_key={"DE": 2, "SE": 1},
        max_by_key=max_by,
    )
    assert bd == {"DE": 2, "SE": 1}


@pytest.fixture()
def memory_db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as db:
        yield db


def test_infer_kit_blanks_condition_only_new_only_used_mixed(memory_db: Session) -> None:
    db = memory_db
    assert infer_kit_blanks_condition_from_totals(db, {"A": 1}) == KitBlanksCondition.NEW
    _catalog_blank(db, "N1", is_bu=False)
    _catalog_blank(db, "U1", is_bu=True)
    db.commit()
    assert infer_kit_blanks_condition_from_totals(db, {"N1": 2}) == KitBlanksCondition.NEW
    assert infer_kit_blanks_condition_from_totals(db, {"U1": 1}) == KitBlanksCondition.USED
    assert infer_kit_blanks_condition_from_totals(db, {"N1": 1, "U1": 1}) == KitBlanksCondition.MIXED


def test_decrement_two_keys_and_sync_pieces_available(memory_db: Session) -> None:
    db = memory_db
    kit = Kit(
        sku="T-KIT-1",
        title="Test kit",
        pieces_total=10,
        pieces_available=0,
        stock_price_total=100.0,
        discount_percent=0,
        cost_total=30.0,
        composition_json=json.dumps({"DE": 1, "SE": 1}),
        created_at=datetime(2024, 1, 1, 12, 0, 0),
    )
    db.add(kit)
    db.flush()
    db.add_all(
        [
            KitBlankStock(kit_id=kit.id, kit_key="DE", qty=4),
            KitBlankStock(kit_id=kit.id, kit_key="SE", qty=2),
        ]
    )
    db.commit()
    db.refresh(kit)

    decrement_blank_stock_keys(db, kit.id, {"DE": 2, "SE": 1})
    sync_kit_pieces_available_from_blank_lines(db, kit)
    db.commit()

    rows = {
        r.kit_key: int(r.qty)
        for r in db.scalars(select(KitBlankStock).where(KitBlankStock.kit_id == kit.id)).all()
    }
    assert rows == {"DE": 2, "SE": 1}
    db.refresh(kit)
    assert int(kit.pieces_available) == 3


def test_ensure_blank_stock_from_composition_on_create(memory_db: Session) -> None:
    db = memory_db
    _catalog_blank(db, "DE")
    _catalog_blank(db, "SE")
    kit = Kit(
        sku="NEW-KIT",
        title="Новый",
        pieces_total=3,
        pieces_available=3,
        stock_price_total=30.0,
        discount_percent=0,
        cost_total=10.0,
        composition_json=json.dumps({"DE": 2, "SE": 1}),
        created_at=datetime(2024, 1, 1, 12, 0, 0),
    )
    db.add(kit)
    db.flush()
    assert ensure_blank_stock_from_composition(db, kit) is True
    db.commit()
    assert blank_stock_qty_map(db, kit.id) == {"DE": 2, "SE": 1}
    db.refresh(kit)
    assert int(kit.pieces_available) == 3


def test_require_composition_stock_auto_heals_missing_blank_rows(memory_db: Session) -> None:
    db = memory_db
    _catalog_blank(db, "DE")
    kit = Kit(
        sku="HEAL-KIT",
        title="Heal",
        pieces_total=2,
        pieces_available=2,
        stock_price_total=20.0,
        discount_percent=0,
        cost_total=5.0,
        composition_json=json.dumps({"DE": 2}),
        created_at=datetime(2024, 1, 1, 12, 0, 0),
    )
    db.add(kit)
    db.commit()
    require_composition_stock_rows_or_scalar_ok(db, kit)
    assert blank_stock_qty_map(db, kit.id) == {"DE": 2}


def _user_and_client(db: Session) -> tuple[User, Client]:
    u = User(
        username="master1",
        password_hash="x",
        display_name="Мастер",
        role=UserRole.MASTER,
        is_active=True,
    )
    c = Client(name="Оксана")
    db.add_all([u, c])
    db.flush()
    return u, c


def _order_kit(db: Session, *, pieces: int = 91, available: int = 91, stock_qty: int | None = None) -> Kit:
    kit = Kit(
        sku="ORDER-26",
        title="Заказ — комплект",
        pieces_total=pieces,
        pieces_available=available,
        stock_price_total=15470.0,
        discount_percent=0,
        cost_total=4575.0,
        composition_json=json.dumps(
            [{"key": "DE_BRAID_LONG", "condition": "NEW", "by_staff": {"2": 4, "6": 87}}],
            ensure_ascii=False,
        ),
        created_at=datetime(2026, 8, 14, 16, 1, 58),
        is_in_stock=True,
    )
    db.add(kit)
    db.flush()
    db.add(KitBlankStock(kit_id=kit.id, kit_key="DE_BRAID_LONG", qty=stock_qty if stock_qty is not None else pieces))
    db.flush()
    return kit


def test_reserve_from_work_consumes_blank_stock_and_release_does_not_double(memory_db: Session) -> None:
    db = memory_db
    user, client = _user_and_client(db)
    kit = _order_kit(db)
    db.commit()
    db.refresh(kit)

    reserve_kit_stock_for_client(
        db,
        kit,
        client_id=int(client.id),
        reserved_by_user_id=int(user.id),
        qty=int(kit.pieces_total),
    )
    db.commit()
    db.refresh(kit)
    assert blank_stock_qty_map(db, kit.id) == {"DE_BRAID_LONG": 0}
    assert int(kit.pieces_available) == 0

    release_client_kit_reserves_into_free_pool(db, kit=kit, client_id=int(client.id))
    db.commit()
    db.refresh(kit)
    assert blank_stock_qty_map(db, kit.id) == {"DE_BRAID_LONG": 91}
    assert int(kit.pieces_available) == 91


def test_repair_pieces_available_desync_keeps_blank_stock(memory_db: Session) -> None:
    db = memory_db
    kit = _order_kit(db, available=182, stock_qty=91)
    db.commit()
    db.refresh(kit)

    assert repair_kit_blank_stock_reserve_desync(db, kit) is True
    db.commit()
    db.refresh(kit)
    assert blank_stock_qty_map(db, kit.id) == {"DE_BRAID_LONG": 91}
    assert int(kit.pieces_available) == 91


def test_repair_unkeyed_reserve_consumes_blank_stock(memory_db: Session) -> None:
    db = memory_db
    user, client = _user_and_client(db)
    kit = _order_kit(db, available=0, stock_qty=91)
    db.add(
        KitReserve(
            kit_id=kit.id,
            pieces_reserved=91,
            reserved_by_user_id=user.id,
            reserved_for_client_id=client.id,
            kit_key=None,
        )
    )
    db.commit()
    db.refresh(kit)

    assert repair_kit_blank_stock_reserve_desync(db, kit) is True
    db.commit()
    db.refresh(kit)
    assert blank_stock_qty_map(db, kit.id) == {"DE_BRAID_LONG": 0}
    assert int(kit.pieces_available) == 0

    release_client_kit_reserves_into_free_pool(db, kit=kit, client_id=int(client.id))
    db.commit()
    db.refresh(kit)
    assert blank_stock_qty_map(db, kit.id) == {"DE_BRAID_LONG": 91}
    assert int(kit.pieces_available) == 91


def test_repair_doubled_blank_stock_resets_to_composition(memory_db: Session) -> None:
    db = memory_db
    kit = _order_kit(db, available=182, stock_qty=182)
    db.commit()
    db.refresh(kit)

    assert repair_kit_blank_stock_reserve_desync(db, kit) is True
    db.commit()
    db.refresh(kit)
    assert blank_stock_qty_map(db, kit.id) == {"DE_BRAID_LONG": 91}
    assert int(kit.pieces_available) == 91


def test_sync_after_decrement_without_autoflush() -> None:
    """Прод: sessionmaker(..., autoflush=False) — SUM без flush оставлял pieces_available."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, future=True)
    with SessionLocal() as db:
        kit = Kit(
            sku="NO-FLUSH",
            title="Kit",
            pieces_total=91,
            pieces_available=91,
            stock_price_total=100.0,
            is_in_stock=True,
            created_at=datetime(2026, 8, 14, 16, 0, 0),
        )
        db.add(kit)
        db.flush()
        db.add(KitBlankStock(kit_id=kit.id, kit_key="DE_BRAID_TRACERY_FULL_HARD", qty=91))
        db.commit()
        db.refresh(kit)

        decrement_blank_stock_keys(db, int(kit.id), {"DE_BRAID_TRACERY_FULL_HARD": 91})
        sync_kit_pieces_available_from_blank_lines(db, kit)
        db.commit()
        db.refresh(kit)
        assert blank_stock_qty_map(db, int(kit.id)) == {"DE_BRAID_TRACERY_FULL_HARD": 0}
        assert int(kit.pieces_available) == 0
        assert kit.is_in_stock is False


def test_repair_all_aligns_available_to_blank_stock(memory_db: Session) -> None:
    db = memory_db
    kit = _order_kit(db, available=91, stock_qty=0)
    kit.is_in_stock = True
    db.commit()
    n = repair_all_kits_pieces_available_from_blank_stock(db)
    db.commit()
    db.refresh(kit)
    assert n >= 1
    assert int(kit.pieces_available) == 0
    assert kit.is_in_stock is False
