from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.db import models as _orm_models  # noqa: F401
from app.db.base import Base
from app.db.models import CatalogProduct, Kit, KitAuditLog, KitBlanksCondition
from app.kit_composition_lines import BlankCondition, CompositionLine, lines_to_json
from app.kit_crud import (
    apply_kit_discount,
    apply_kit_stock_price_recalc,
    kit_display_price_breakdown,
    parse_kit_ids_csv,
    preview_kit_stock_price_recalc,
)


@pytest.fixture()
def memory_db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as db:
        yield db


def test_parse_kit_ids_csv_basic() -> None:
    assert parse_kit_ids_csv("12, 45,108") == [12, 45, 108]
    assert parse_kit_ids_csv("1;2, 2") == [1, 2]


def test_parse_kit_ids_csv_rejects_empty() -> None:
    with pytest.raises(ValueError, match="через запятую"):
        parse_kit_ids_csv("  ,  ")


def test_parse_kit_ids_csv_rejects_bad_token() -> None:
    with pytest.raises(ValueError, match="Некорректный id"):
        parse_kit_ids_csv("12, abc")


def test_kit_display_price_breakdown_proportional() -> None:
    kit = SimpleNamespace(
        stock_price_total=1000.0,
        pieces_total=10,
        pieces_available=4,
        discount_percent=20,
        reserves=[
            SimpleNamespace(pieces_reserved=3),
            SimpleNamespace(pieces_reserved=1),
        ],
    )
    b = kit_display_price_breakdown(kit)
    assert b["full"] == 1000.0
    assert b["remainder"] == 400.0
    assert b["reserved"] == 400.0
    assert b["pieces_reserved"] == 4
    assert b["has_discount"] is True
    assert b["full_discounted"] == 800.0
    assert b["remainder_discounted"] == 320.0
    assert b["reserved_discounted"] == 320.0


def test_kit_display_price_breakdown_no_discount() -> None:
    kit = SimpleNamespace(
        stock_price_total=500.0,
        pieces_total=5,
        pieces_available=5,
        discount_percent=0,
        reserves=[],
    )
    b = kit_display_price_breakdown(kit)
    assert b["remainder"] == 500.0
    assert b["reserved"] == 0.0
    assert b["full_discounted"] is None
    assert apply_kit_discount(500.0, 0) is None
    assert apply_kit_discount(500.0, 10) == 450.0


def test_apply_kit_stock_price_recalc_writes_full_price_and_audit(memory_db: Session) -> None:
    memory_db.add(
        CatalogProduct(
            category_name="Заказ",
            subcategory_name="Заготовки поштучно",
            name="SE Body",
            price=200.0,
            meta_json='{"kit_key": "SE_BODY"}',
            is_active=True,
        )
    )
    composition = lines_to_json(
        [CompositionLine(key="SE_BODY", condition=BlankCondition.NEW, by_staff={1: 3})]
    )
    kit = Kit(
        sku="PRICE-RECALC-1",
        title="Тест пересчёта",
        blank_type_de=False,
        blank_type_se=True,
        blanks_condition=KitBlanksCondition.NEW,
        pieces_total=3,
        pieces_available=3,
        stock_price_total=100.0,
        cost_total=50.0,
        discount_percent=0,
        composition_json=composition,
        is_active=True,
    )
    memory_db.add(kit)
    memory_db.commit()
    memory_db.refresh(kit)

    preview = preview_kit_stock_price_recalc(memory_db, [kit.id])
    assert len(preview) == 1
    assert preview[0]["ok"] is True
    assert preview[0]["old_price"] == 100.0
    assert preview[0]["new_price"] == 600.0

    applied = apply_kit_stock_price_recalc(memory_db, [kit.id], changed_by_user_id=1)
    memory_db.commit()
    memory_db.refresh(kit)
    assert kit.stock_price_total == 600.0
    assert applied[0]["ok"] is True

    audits = list(memory_db.query(KitAuditLog).filter(KitAuditLog.kit_id == kit.id).all())
    assert any(
        a.field_name in ("stock_price_total", "Складская цена") and (a.new_value or "").startswith("600")
        for a in audits
    )
