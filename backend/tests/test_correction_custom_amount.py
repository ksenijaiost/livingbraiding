"""Fix 104: своя сумма при коррекции комплекта (работа и визит)."""

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
    Client,
    ClientPaymentKind,
    PayrollPeriod,
    Service,
    ServiceCategory,
    ServiceSubcategory,
    User,
    UserRole,
    UserRoleAssignment,
    VisitClientType,
    VisitMastersScope,
    WorkForInventory,
    WorkKind,
)
from app.visit_multi_service import VisitHeaderInput, VisitServiceLineInput, compute_visit_service_line, effective_amount_from_client
from app.work_products_compute import (
    CORR_SVC_TRIM,
    compute_correction_extra_costs,
    split_profit_from_client_amount,
)


@pytest.fixture()
def memory_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as db:
        yield db


def _seed_correction_catalog(db, *, fixed: float = 10.0) -> None:
    db.add(
        CatalogProduct(
            category_name="Заказ",
            subcategory_name="Коррекция комплекта",
            name=CORR_SVC_TRIM,
            price=100.0,
            meta_json=json.dumps({"master_pay": 50.0, "studio_pay": 20.0, "fixed_expense": fixed}),
            is_active=True,
        )
    )
    db.commit()


def test_compute_correction_extra_costs(memory_db) -> None:
    _seed_correction_catalog(memory_db, fixed=15.0)
    fx = compute_correction_extra_costs(
        memory_db,
        corr_trim_qty=2,
        corr_hourly_hours=0.0,
        corr_hourly_avg=False,
        corr_wash=False,
        corr_circle=False,
        corr_steam=False,
    )
    assert fx == pytest.approx(30.0)


def test_split_profit_from_client_amount() -> None:
    profit, masters, studio = split_profit_from_client_amount(5000, 1000, 0.5)
    assert profit == pytest.approx(4000.0)
    assert masters == pytest.approx(2000.0)
    assert studio == pytest.approx(2000.0)


def test_post_work_kit_correction_custom_amount(memory_db) -> None:
    import asyncio
    from urllib.parse import urlencode

    from starlette.requests import Request

    from app.auth import AuthUser
    from app.db.models import MasterLevel
    from app.work_products import work_new_post

    db = memory_db
    db.add(
        PayrollPeriod(
            date_from=datetime(2020, 1, 1),
            date_to=datetime(2030, 12, 31, 23, 59, 59),
            closed_at=None,
        )
    )
    master = User(
        username="m1",
        password_hash="x",
        display_name="Master",
        role=UserRole.MASTER,
        is_active=True,
    )
    db.add(master)
    db.flush()
    db.add(UserRoleAssignment(user_id=master.id, role=UserRole.MASTER))
    _seed_correction_catalog(db, fixed=10.0)
    db.commit()

    fields = {
        "performed_date": "2025-06-01",
        "scope": "IN_STOCK",
        "kind": "KIT_CORRECTION",
        "corr_trim_qty": "1",
        "corr_use_custom_amount": "1",
        "corr_custom_amount": "5000",
        "corr_client_payment_kind": "NON_CASH",
        "kanekalon_grams": "0",
        "kudri_grams": "0",
        "mix_source": "NO_MIX",
    }
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
    user = AuthUser(
        id=master.id,
        username="m1",
        display_name="Master",
        role=UserRole.MASTER,
        roles=(UserRole.MASTER,),
        master_level=MasterLevel.MIDDLE,
    )
    resp = asyncio.run(work_new_post(request, user, db))
    assert resp.status_code == 303
    work = db.scalars(select(WorkForInventory).order_by(WorkForInventory.id.desc())).first()
    assert work is not None
    assert work.kind == WorkKind.KIT_CORRECTION
    assert work.amount_from_client == 5000
    assert work.client_payment_kind == ClientPaymentKind.NON_CASH
    assert work.cost_total_amount == pytest.approx(10.0)
    assert work.master_profit_amount + work.studio_profit_amount == pytest.approx(4990.0)


def test_visit_correction_custom_amount(memory_db) -> None:
    db = memory_db
    cat = ServiceCategory(name="Cat")
    sub = ServiceSubcategory(name="Sub", category_id=None, show_kit_section=True)
    db.add(cat)
    db.flush()
    sub.category_id = cat.id
    db.add(sub)
    db.flush()
    svc = Service(
        subcategory_id=sub.id,
        name="Вплетение",
        estimated_duration_minutes=60,
        is_active=True,
    )
    client = Client(name="Клиент", is_confirmed=True)
    db.add_all([svc, client])
    db.flush()
    _seed_correction_catalog(db, fixed=20.0)

    line = VisitServiceLineInput(
        service_id=int(svc.id),
        amount_from_client=0,
        client_discount_percent=0,
        kanekalon_grams=0,
        kudri_grams=0,
        mix_source=None,
        mix_complexity=None,
        mix_bonus_master_id=None,
        amortization_level=None,
        own_correction=True,
        own_corr_trim_qty=1,
        own_corr_use_custom_amount=True,
        own_corr_custom_amount=3000.0,
        own_corr_client_payment_kind=ClientPaymentKind.CASH,
        kit_kind="OWN",
        own_origin="FOREIGN",
    )
    header = VisitHeaderInput(
        client_mode="existing",
        existing_client_id=int(client.id),
        draft_name="",
        draft_phone="",
        draft_telegram="",
        draft_vk="",
        draft_instagram="",
        draft_other_contact="",
        client_type=VisitClientType.RETURNING,
        performed_date=datetime(2025, 6, 1).date(),
        duration_minutes=60,
        masters_scope=VisitMastersScope.VISIT,
        same_master_shares_all_services=False,
        visit_master_allocations=[],
    )
    computed = compute_visit_service_line(db, line, header, default_mix_bonus_master_id=None, apply_kit_stock=False)
    assert computed.amount_from_client == 3000.0
    assert computed.client_payment_kind == ClientPaymentKind.CASH
    assert computed.cost_total >= 20.0
    assert computed.masters_pool + computed.salon_profit == pytest.approx(computed.profit_before_split)


def test_effective_amount_from_client_custom_correction() -> None:
    line = VisitServiceLineInput(
        service_id=1,
        amount_from_client=5000,
        client_discount_percent=0,
        kanekalon_grams=0,
        kudri_grams=0,
        mix_source=None,
        mix_complexity=None,
        mix_bonus_master_id=None,
        amortization_level=None,
        own_correction=True,
        own_corr_use_custom_amount=True,
        own_corr_custom_amount=4500.0,
        kit_kind="OWN",
    )
    assert effective_amount_from_client(line) == 9500.0


def test_parse_visit_line_custom_amount_without_flag() -> None:
    from app.visit_multi_service import _parse_line_from_form

    class _Form:
        def keys(self):
            return self._data.keys()

        def get(self, key, default=None):
            return self._data.get(key, default)

        def getlist(self, key):
            v = self._data.get(key)
            if v is None:
                return []
            return [v]

        def __init__(self, data):
            self._data = data

    form = _Form(
        {
            "service_id": "1",
            "amount_from_client": "",
            "own_correction": "on",
            "own_corr_custom_amount": "3200",
            "own_corr_client_payment_kind": "CASH",
            "kit_kind": "OWN",
            "kanekalon_grams": "0",
            "kudri_grams": "0",
            "mix_source": "NO_MIX",
        }
    )
    line = _parse_line_from_form(form, 0)
    assert line.own_corr_use_custom_amount is True
    assert line.amount_from_client == 0.0
    assert effective_amount_from_client(line) == 3200.0


def test_visit_correction_custom_amount_adds_to_service_sum(memory_db) -> None:
    db = memory_db
    cat = ServiceCategory(name="Cat2")
    sub = ServiceSubcategory(name="Sub2", category_id=None, show_kit_section=True)
    db.add(cat)
    db.flush()
    sub.category_id = cat.id
    db.add(sub)
    db.flush()
    svc = Service(subcategory_id=sub.id, name="Вплетение 2", estimated_duration_minutes=60, is_active=True)
    client = Client(name="Клиент 2", is_confirmed=True)
    db.add_all([svc, client])
    db.flush()
    _seed_correction_catalog(db, fixed=20.0)

    line = VisitServiceLineInput(
        service_id=int(svc.id),
        amount_from_client=5000.0,
        client_discount_percent=0,
        kanekalon_grams=0,
        kudri_grams=0,
        mix_source=None,
        mix_complexity=None,
        mix_bonus_master_id=None,
        amortization_level=None,
        own_correction=True,
        own_corr_trim_qty=1,
        own_corr_use_custom_amount=True,
        own_corr_custom_amount=3000.0,
        own_corr_client_payment_kind=ClientPaymentKind.CASH,
        kit_kind="OWN",
        own_origin="FOREIGN",
    )
    header = VisitHeaderInput(
        client_mode="existing",
        existing_client_id=int(client.id),
        draft_name="",
        draft_phone="",
        draft_telegram="",
        draft_vk="",
        draft_instagram="",
        draft_other_contact="",
        client_type=VisitClientType.RETURNING,
        performed_date=datetime(2025, 6, 1).date(),
        duration_minutes=60,
        masters_scope=VisitMastersScope.VISIT,
        same_master_shares_all_services=False,
        visit_master_allocations=[],
    )
    computed = compute_visit_service_line(db, line, header, default_mix_bonus_master_id=None, apply_kit_stock=False)
    assert computed.amount_from_client == 8000.0
    assert computed.cost_total >= 20.0
