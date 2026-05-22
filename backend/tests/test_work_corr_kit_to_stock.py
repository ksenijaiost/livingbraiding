"""Б/У комплект на склад из работы «Коррекция комплекта» (в наличие)."""

from __future__ import annotations

import json
from datetime import datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db import models as _orm_models  # noqa: F401
from app.db.base import Base
from app.db.models import (
    CatalogProduct,
    Kit,
    KitBlanksCondition,
    KitBlankStock,
    MasterLevel,
    PayrollPeriod,
    User,
    UserRole,
    UserRoleAssignment,
    WorkForInventory,
    WorkKind,
)
from app.kit_composition_lines import (
    BlankCondition,
    CompositionLine,
    apply_global_used_discount,
    lines_from_form,
    stock_price_for_used_kit,
    work_pay_for_lines,
)
from app.work_products_compute import CORR_SVC_TRIM


class _FakeForm(dict):
    def keys(self):
        return super().keys()

    def get(self, key, default=None):
        return super().get(key, default)


@pytest.fixture()
def memory_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as db:
        yield db


def _seed_master_and_catalog(db, *, blank_price: float = 200.0, trim_pay: float = 50.0) -> User:
    db.add(
        PayrollPeriod(
            date_from=datetime(2020, 1, 1),
            date_to=datetime(2030, 12, 31, 23, 59, 59),
            closed_at=None,
        )
    )
    u = User(
        username="master1",
        password_hash="x",
        display_name="Master",
        role=UserRole.MASTER,
        is_active=True,
    )
    db.add(u)
    db.flush()
    db.add(UserRoleAssignment(user_id=u.id, role=UserRole.MASTER))
    db.add(
        CatalogProduct(
            category_name="Заказ",
            subcategory_name="Заготовки поштучно",
            name="SE Braid",
            price=blank_price,
            meta_json='{"kit_key": "SE_BRAID_LONG", "master_pay": 30}',
            is_active=True,
        )
    )
    db.add(
        CatalogProduct(
            category_name="Заказ",
            subcategory_name="Коррекция комплекта",
            name=CORR_SVC_TRIM,
            price=100.0,
            meta_json=json.dumps({"master_pay": trim_pay, "fixed_expense": 10.0}),
            is_active=True,
        )
    )
    db.commit()
    return u


def test_corr_kit_line_prefix_from_form():
    form = _FakeForm(
        {
            "corr_kit_line_0_key": "SE_BRAID_LONG",
            "corr_kit_line_0_qty_3": "2",
            "corr_kit_line_1_key": "",
        }
    )
    lines = lines_from_form(form, prefix="corr_kit_line")
    assert len(lines) == 1
    assert lines[0].key == "SE_BRAID_LONG"
    assert lines[0].by_staff[3] == 2


def test_stock_price_for_used_kit_40pct(memory_db):
    _seed_master_and_catalog(memory_db, blank_price=200.0)
    lines = [CompositionLine(key="SE_BRAID_LONG", by_staff={1: 2})]
    new_total, stock_total, missing = stock_price_for_used_kit(memory_db, lines, 40)
    assert missing == []
    assert new_total == pytest.approx(400.0)
    assert stock_total == pytest.approx(160.0)
    saved = apply_global_used_discount(lines, 40)
    assert saved[0].condition == BlankCondition.USED
    assert saved[0].used_price_pct == 40


def test_work_pay_zero_for_used_only_lines(memory_db):
    _seed_master_and_catalog(memory_db)
    lines = apply_global_used_discount(
        [CompositionLine(key="SE_BRAID_LONG", by_staff={1: 3})], 50
    )
    assert work_pay_for_lines(memory_db, lines) == {}


def _auth_user(master: User) -> "AuthUser":
    from app.auth import AuthUser

    return AuthUser(
        id=master.id,
        username="master1",
        display_name="Master",
        role=UserRole.MASTER,
        roles=(UserRole.MASTER,),
        master_level=MasterLevel.MIDDLE,
    )


def _post_work_form(memory_db, master: User, fields: dict[str, str]):
    import asyncio
    from urllib.parse import urlencode

    from starlette.requests import Request

    from app.work_products import work_new_post

    body = urlencode(fields).encode("utf-8")

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/sales/work/new",
        "headers": [(b"content-type", b"application/x-www-form-urlencoded")],
        "query_string": b"",
    }
    request = Request(scope, receive)
    return asyncio.run(work_new_post(request, _auth_user(master), memory_db))


def test_post_work_creates_used_kit(memory_db):
    master = _seed_master_and_catalog(memory_db, blank_price=100.0, trim_pay=40.0)
    mid = master.id
    resp = _post_work_form(
        memory_db,
        master,
        {
            "performed_date": "2025-05-21",
            "scope": "IN_STOCK",
            "kind": "KIT_CORRECTION",
            "corr_trim_qty": "1",
            "corr_add_kit_to_stock": "1",
            "corr_kit_type_se": "on",
            "corr_kit_sku": "BU-CORR-001",
            "corr_kit_title": "Комплект б/у",
            "corr_kit_used_discount_pct": "40",
            "corr_kit_line_0_key": "SE_BRAID_LONG",
            f"corr_kit_line_0_qty_{mid}": "2",
            "kanekalon_grams": "0",
            "kudri_grams": "0",
            "mix_source": "NO_MIX",
        },
    )
    assert resp.status_code == 303, getattr(resp, "body", resp)
    work = memory_db.scalars(select(WorkForInventory).order_by(WorkForInventory.id.desc())).first()
    assert work is not None
    assert work.kind == WorkKind.KIT_CORRECTION
    assert work.created_kit_id is not None
    kit = memory_db.get(Kit, int(work.created_kit_id))
    assert kit is not None
    assert kit.blanks_condition == KitBlanksCondition.USED
    assert kit.stock_price_total == pytest.approx(80.0)
    assert kit.discount_percent == 40
    assert kit.cost_total == pytest.approx(work.cost_total_amount + work.master_profit_amount)
    comp = json.loads(kit.composition_json or "[]")
    assert comp[0]["condition"] == "USED"
    assert comp[0]["used_price_pct"] == 40
    stock_rows = list(
        memory_db.scalars(select(KitBlankStock).where(KitBlankStock.kit_id == kit.id)).all()
    )
    assert len(stock_rows) == 1
    assert stock_rows[0].kit_key == "SE_BRAID_LONG"
    assert stock_rows[0].qty == 2


def test_post_without_flag_no_kit(memory_db):
    master = _seed_master_and_catalog(memory_db)
    _post_work_form(
        memory_db,
        master,
        {
            "performed_date": "2025-05-21",
            "scope": "IN_STOCK",
            "kind": "KIT_CORRECTION",
            "corr_trim_qty": "1",
            "kanekalon_grams": "0",
            "kudri_grams": "0",
            "mix_source": "NO_MIX",
        },
    )
    work = memory_db.scalars(select(WorkForInventory).order_by(WorkForInventory.id.desc())).first()
    assert work.created_kit_id is None


def test_corr_kit_on_custom_order_rejected(memory_db):
    master = _seed_master_and_catalog(memory_db)
    mid = master.id
    resp = _post_work_form(
        memory_db,
        master,
        {
            "performed_date": "2025-05-21",
            "scope": "CUSTOM_ORDER",
            "kind": "KIT_CORRECTION",
            "corr_trim_qty": "1",
            "corr_add_kit_to_stock": "1",
            "corr_kit_type_se": "on",
            "corr_kit_sku": "X",
            "corr_kit_title": "Y",
            "corr_kit_used_discount_pct": "50",
            "corr_kit_line_0_key": "SE_BRAID_LONG",
            f"corr_kit_line_0_qty_{mid}": "1",
            "kanekalon_grams": "0",
            "kudri_grams": "0",
        },
    )
    assert resp.status_code == 400
    assert "наличие" in resp.body.decode("utf-8")
