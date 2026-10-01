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
    apply_kit_admin_stock_remainder,
    blank_stock_edit_rows_for_kit,
    blank_stock_qty_map,
    build_usage_breakdown_keyed,
    decrement_blank_stock_keys,
    distribute_scalar_to_keys,
    ensure_blank_stock_from_composition,
    infer_kit_blanks_condition_from_totals,
    infer_stock_remainder_mode,
    inventory_qty_by_key_from_kit,
    keyed_cost_selected,
    kit_reserve_free_rows,
    load_catalog_kit_maps,
    max_take_by_key_for_client,
    merge_keyed_kit_reserve_rows_by_batch,
    release_client_kit_reserves_into_free_pool,
    repair_all_kits_pieces_available_from_blank_stock,
    repair_kit_blank_stock_reserve_desync,
    require_composition_stock_rows_or_scalar_ok,
    reserve_kit_stock_for_client,
    reserve_row_per_key_map,
    split_unkeyed_kit_reserves_by_composition,
    sum_reserved_by_key_for_client,
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


def test_keyed_stock_price_allocates_by_catalog_weights(memory_db: Session) -> None:
    """Цена склада 6460 раскладывается как 72*x+2*y при x:y = 85:170."""
    from app.kit_blank_stock_core import (
        keyed_stock_price_selected,
        keyed_stock_unit_prices_from_catalog_weights,
    )

    db = memory_db
    _catalog_blank(db, "DE_BRAID_LONG")
    # override prices: SE=85, DE=170
    for r in db.scalars(select(CatalogProduct)).all():
        meta = json.loads(r.meta_json or "{}")
        if meta.get("kit_key") == "DE_BRAID_LONG":
            r.price = 170.0
            r.name = "D.E. коса"
    db.add(
        CatalogProduct(
            is_active=True,
            category_name="Заказ",
            subcategory_name="Заготовки поштучно",
            name="S.E. коса",
            price=85.0,
            meta_json=json.dumps({"kit_key": "SE_BRAID_LONG"}),
        )
    )
    kit = Kit(
        sku="C1",
        title="Kit",
        pieces_total=74,
        pieces_available=74,
        stock_price_total=6460.0,
        cost_total=1.0,
        composition_json=json.dumps(
            [
                {"key": "SE_BRAID_LONG", "qty": 72},
                {"key": "DE_BRAID_LONG", "qty": 2},
            ],
            ensure_ascii=False,
        ),
        created_at=datetime(2026, 9, 3, 12, 0, 0),
        is_in_stock=True,
    )
    db.add(kit)
    db.flush()
    price_map, meta, _ = load_catalog_kit_maps(db)
    assert price_map["SE_BRAID_LONG"] == 85.0
    assert price_map["DE_BRAID_LONG"] == 170.0
    units = keyed_stock_unit_prices_from_catalog_weights(
        db,
        kit,
        stock_price_total=6460.0,
        composition_qty={"SE_BRAID_LONG": 72, "DE_BRAID_LONG": 2},
        price_map=price_map,
        meta_by_key=meta,
    )
    assert units["SE_BRAID_LONG"] == pytest.approx(85.0)
    assert units["DE_BRAID_LONG"] == pytest.approx(170.0)
    assert keyed_stock_price_selected(
        {"SE_BRAID_LONG": 10}, unit_stock_by_key=units
    ) == pytest.approx(850.0)
    # если цена склада другая — пропорция прайса сохраняется
    units2 = keyed_stock_unit_prices_from_catalog_weights(
        db,
        kit,
        stock_price_total=7000.0,
        composition_qty={"SE_BRAID_LONG": 72, "DE_BRAID_LONG": 2},
        price_map=price_map,
        meta_by_key=meta,
    )
    assert units2["SE_BRAID_LONG"] / units2["DE_BRAID_LONG"] == pytest.approx(85.0 / 170.0)
    assert keyed_stock_price_selected(
        {"SE_BRAID_LONG": 10}, unit_stock_by_key=units2
    ) == pytest.approx(7000.0 * 85.0 / 6460.0 * 10)


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


def _mixed_kit(db: Session, *, se_qty: int, de_qty: int, stock: dict[str, int] | None) -> Kit:
    total = se_qty + de_qty
    kit = Kit(
        sku="INV-74",
        title="Инвентаризация",
        pieces_total=total,
        pieces_available=total if stock is None else sum(stock.values()),
        blank_type_se=True,
        blank_type_de=True,
        stock_price_total=1000.0,
        cost_total=400.0,
        composition_json=json.dumps(
            [
                {"key": "SE_BRAID_LONG", "qty": se_qty},
                {"key": "DE_BRAID_LONG", "qty": de_qty},
                {"key": "SE_TRIM_SHORT", "qty": 3},
            ],
            ensure_ascii=False,
        ),
        created_at=datetime(2026, 8, 20, 12, 0, 0),
        is_in_stock=True,
    )
    db.add(kit)
    db.flush()
    if stock is not None:
        for k, q in stock.items():
            if int(q) > 0:
                db.add(KitBlankStock(kit_id=kit.id, kit_key=k, qty=int(q)))
        db.flush()
    return kit


def _mixed_condition_kit(db: Session, *, stock: dict[str, int] | None = None) -> Kit:
    kit = Kit(
        sku="INV-COND",
        title="Инвентаризация с б/у",
        pieces_total=80,
        pieces_available=80 if stock is None else sum(stock.values()),
        blank_type_se=True,
        stock_price_total=1000.0,
        cost_total=400.0,
        composition_json=json.dumps(
            [
                {"key": "SE_CURL", "condition": "NEW", "qty": 28},
                {"key": "SE_CURL", "condition": "USED", "used_price_pct": 60, "qty": 52},
            ],
            ensure_ascii=False,
        ),
        created_at=datetime(2026, 8, 20, 12, 0, 0),
        is_in_stock=True,
    )
    db.add(kit)
    db.flush()
    if stock is not None:
        for k, q in stock.items():
            if int(q) > 0:
                db.add(KitBlankStock(kit_id=kit.id, kit_key=k, qty=int(q)))
        db.flush()
    return kit


def test_inventory_qty_by_key_excludes_trims(memory_db: Session) -> None:
    kit = _mixed_kit(memory_db, se_qty=72, de_qty=2, stock=None)
    assert inventory_qty_by_key_from_kit(kit) == {"SE_BRAID_LONG": 72, "DE_BRAID_LONG": 2}


def test_inventory_qty_by_key_splits_new_and_used_same_kind(memory_db: Session) -> None:
    kit = _mixed_condition_kit(memory_db, stock=None)
    assert inventory_qty_by_key_from_kit(kit) == {"SE_CURL": 28, "SE_CURL__USED__": 52}


def test_infer_stock_remainder_mode_all_when_stock_matches_composition(memory_db: Session) -> None:
    db = memory_db
    kit = _mixed_kit(db, se_qty=72, de_qty=2, stock={"SE_BRAID_LONG": 72, "DE_BRAID_LONG": 2})
    db.commit()
    assert infer_stock_remainder_mode(db, kit) == "all"


def test_infer_stock_remainder_mode_choose_when_stock_differs(memory_db: Session) -> None:
    db = memory_db
    kit = _mixed_kit(db, se_qty=72, de_qty=2, stock={"SE_BRAID_LONG": 67, "DE_BRAID_LONG": 2})
    db.commit()
    assert infer_stock_remainder_mode(db, kit) == "choose"


def test_apply_admin_remainder_all_copies_composition(memory_db: Session) -> None:
    db = memory_db
    kit = _mixed_kit(db, se_qty=72, de_qty=2, stock={"SE_BRAID_LONG": 67, "DE_BRAID_LONG": 2})
    db.commit()
    apply_kit_admin_stock_remainder(db, kit, mode="all", blank_qty={"SE_BRAID_LONG": 1})
    db.commit()
    db.refresh(kit)
    assert blank_stock_qty_map(db, kit.id) == {"SE_BRAID_LONG": 72, "DE_BRAID_LONG": 2}
    assert int(kit.pieces_available) == 74
    assert infer_stock_remainder_mode(db, kit) == "all"


def test_apply_admin_remainder_choose_uses_posted_qty(memory_db: Session) -> None:
    db = memory_db
    kit = _mixed_kit(db, se_qty=72, de_qty=2, stock={"SE_BRAID_LONG": 72, "DE_BRAID_LONG": 2})
    db.commit()
    apply_kit_admin_stock_remainder(
        db, kit, mode="choose", blank_qty={"SE_BRAID_LONG": 70, "DE_BRAID_LONG": 1}
    )
    db.commit()
    db.refresh(kit)
    assert blank_stock_qty_map(db, kit.id) == {"SE_BRAID_LONG": 70, "DE_BRAID_LONG": 1}
    assert int(kit.pieces_available) == 71
    assert infer_stock_remainder_mode(db, kit) == "choose"


def test_blank_stock_edit_rows_skip_trims(memory_db: Session) -> None:
    db = memory_db
    kit = _mixed_kit(db, se_qty=72, de_qty=2, stock={"SE_BRAID_LONG": 70, "DE_BRAID_LONG": 1})
    db.commit()
    rows = blank_stock_edit_rows_for_kit(db, kit)
    assert [r["key"] for r in rows] == ["DE_BRAID_LONG", "SE_BRAID_LONG"]
    by_key = {r["key"]: r["qty"] for r in rows}
    assert by_key == {"DE_BRAID_LONG": 1, "SE_BRAID_LONG": 70}


def test_blank_stock_edit_rows_split_new_and_used_same_kind(memory_db: Session) -> None:
    db = memory_db
    _catalog_blank(db, "SE_CURL")
    kit = _mixed_condition_kit(db, stock={"SE_CURL": 28, "SE_CURL__USED__": 52})
    db.commit()
    rows = blank_stock_edit_rows_for_kit(db, kit)
    assert [r["key"] for r in rows] == ["SE_CURL", "SE_CURL"]
    assert [r["condition_label"] for r in rows] == ["нов", "б/у"]
    by_raw_key = {r["raw_key"]: r["qty"] for r in rows}
    assert by_raw_key == {"SE_CURL": 28, "SE_CURL__USED__": 52}


def test_stock_price_alloc_respects_used_pct_per_stock_key(memory_db: Session) -> None:
    """NEW и б/у одного вида — разные веса; 60% б/у не усредняется с новыми."""
    from app.kit_blank_stock_core import (
        catalog_unit_weight_for_kit_key,
        keyed_stock_price_selected,
        keyed_stock_unit_prices_from_catalog_weights,
    )
    from app.kit_inlay_visit import kit_suggest_dict_for_kit

    db = memory_db
    db.add(
        CatalogProduct(
            is_active=True,
            category_name="Заказ",
            subcategory_name="Заготовки поштучно",
            name="SE Curl",
            price=100.0,
            meta_json=json.dumps({"kit_key": "SE_CURL"}),
        )
    )
    # 28*100 + 52*60 = 5920
    kit = Kit(
        sku="BU-MIX",
        title="Смесь",
        pieces_total=80,
        pieces_available=80,
        stock_price_total=5920.0,
        cost_total=1.0,
        composition_json=json.dumps(
            [
                {"key": "SE_CURL", "condition": "NEW", "qty": 28},
                {"key": "SE_CURL", "condition": "USED", "used_price_pct": 60, "qty": 52},
            ],
            ensure_ascii=False,
        ),
        created_at=datetime(2026, 9, 4, 12, 0, 0),
        is_in_stock=True,
    )
    db.add(kit)
    db.flush()
    db.add(KitBlankStock(kit_id=kit.id, kit_key="SE_CURL", qty=28))
    db.add(KitBlankStock(kit_id=kit.id, kit_key="SE_CURL__USED__", qty=52))
    db.commit()
    db.refresh(kit)

    price_map, meta, _ = load_catalog_kit_maps(db)
    assert catalog_unit_weight_for_kit_key(
        db, kit, "SE_CURL", price_map=price_map, meta_by_key=meta
    ) == pytest.approx(100.0)
    assert catalog_unit_weight_for_kit_key(
        db, kit, "SE_CURL__USED__", price_map=price_map, meta_by_key=meta
    ) == pytest.approx(60.0)

    inv = {"SE_CURL": 28, "SE_CURL__USED__": 52}
    units = keyed_stock_unit_prices_from_catalog_weights(
        db,
        kit,
        stock_price_total=5920.0,
        composition_qty=inv,
        price_map=price_map,
        meta_by_key=meta,
    )
    assert units["SE_CURL"] == pytest.approx(100.0)
    assert units["SE_CURL__USED__"] == pytest.approx(60.0)
    assert keyed_stock_price_selected(
        {"SE_CURL__USED__": 10}, unit_stock_by_key=units
    ) == pytest.approx(600.0)

    preview = kit_suggest_dict_for_kit(db, kit, for_client_id=None)
    by_key = {r["key"]: r for r in preview["per_key"]}
    assert by_key["SE_CURL"]["price_per_piece"] == pytest.approx(100.0)
    assert by_key["SE_CURL__USED__"]["price_per_piece"] == pytest.approx(60.0)
    assert by_key["SE_CURL__USED__"]["condition"] == "USED"
    assert by_key["SE_CURL__USED__"]["used_price_pct"] == 60


def test_max_take_by_key_null_reserve_uses_composition(memory_db: Session) -> None:
    db = memory_db
    _catalog_blank(db, "DE_BRAID_LONG")
    _catalog_blank(db, "SE_BRAID_LONG")
    user, client = _user_and_client(db)
    kit = _mixed_kit(db, se_qty=40, de_qty=20, stock={"DE_BRAID_LONG": 0, "SE_BRAID_LONG": 0})
    db.add(
        KitReserve(
            kit_id=kit.id,
            pieces_reserved=60,
            kit_key=None,
            reserved_by_user_id=user.id,
            reserved_for_client_id=client.id,
        )
    )
    db.commit()
    db.refresh(kit)

    out = max_take_by_key_for_client(
        db,
        kit=kit,
        client_id=int(client.id),
        stock_map={"DE_BRAID_LONG": 0, "SE_BRAID_LONG": 0},
    )
    assert out == {"DE_BRAID_LONG": 20, "SE_BRAID_LONG": 40}


def test_split_unkeyed_reserves_by_composition(memory_db: Session) -> None:
    db = memory_db
    _catalog_blank(db, "DE_BRAID_LONG")
    _catalog_blank(db, "SE_BRAID_LONG")
    user, client = _user_and_client(db)
    kit = _mixed_kit(db, se_qty=40, de_qty=20, stock={"DE_BRAID_LONG": 0, "SE_BRAID_LONG": 0})
    db.add(
        KitReserve(
            kit_id=kit.id,
            pieces_reserved=60,
            kit_key=None,
            reserved_by_user_id=user.id,
            reserved_for_client_id=client.id,
        )
    )
    db.commit()

    n = split_unkeyed_kit_reserves_by_composition(db, kit)
    db.commit()
    assert n == 1
    rows = list(db.scalars(select(KitReserve).where(KitReserve.kit_id == kit.id)).all())
    assert len(rows) == 1
    r = rows[0]
    assert int(r.pieces_reserved) == 60
    assert json.loads(r.reserve_breakdown_json or "{}") == {"DE_BRAID_LONG": 20, "SE_BRAID_LONG": 40}


def test_merge_keyed_kit_reserve_rows_by_batch(memory_db: Session) -> None:
    db = memory_db
    _catalog_blank(db, "DE_BRAID_LONG")
    _catalog_blank(db, "SE_BRAID_LONG")
    user, client = _user_and_client(db)
    kit = _mixed_kit(db, se_qty=40, de_qty=20, stock={"DE_BRAID_LONG": 0, "SE_BRAID_LONG": 0})
    when = datetime(2026, 9, 2, 10, 0, 0)
    db.add_all(
        [
            KitReserve(
                kit_id=kit.id,
                kit_key="DE_BRAID_LONG",
                pieces_reserved=20,
                reserved_at=when,
                reserved_by_user_id=user.id,
                reserved_for_client_id=client.id,
            ),
            KitReserve(
                kit_id=kit.id,
                kit_key="SE_BRAID_LONG",
                pieces_reserved=40,
                reserved_at=when,
                reserved_by_user_id=user.id,
                reserved_for_client_id=client.id,
            ),
        ]
    )
    db.commit()

    n = merge_keyed_kit_reserve_rows_by_batch(db, kit)
    db.commit()
    assert n == 1
    rows = list(db.scalars(select(KitReserve).where(KitReserve.kit_id == kit.id)).all())
    assert len(rows) == 1
    assert json.loads(rows[0].reserve_breakdown_json or "{}") == {"DE_BRAID_LONG": 20, "SE_BRAID_LONG": 40}
    assert sum_reserved_by_key_for_client(db, kit_id=int(kit.id), client_id=int(client.id)) == {
        "DE_BRAID_LONG": 20,
        "SE_BRAID_LONG": 40,
    }


def test_kit_reserve_free_rows_labels(memory_db: Session) -> None:
    db = memory_db
    _catalog_blank(db, "DE_BRAID_LONG")
    _catalog_blank(db, "SE_BRAID_LONG")
    kit = _mixed_kit(db, se_qty=40, de_qty=20, stock={"DE_BRAID_LONG": 20, "SE_BRAID_LONG": 40})
    db.commit()
    keyed, rows = kit_reserve_free_rows(db, kit)
    assert keyed is True
    by_key = {r["key"]: r for r in rows}
    assert by_key["DE_BRAID_LONG"]["qty_free"] == 20
    assert by_key["SE_BRAID_LONG"]["qty_free"] == 40
    assert by_key["DE_BRAID_LONG"]["label"] == "DE_BRAID_LONG"

