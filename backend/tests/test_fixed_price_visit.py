"""1.53: «Работа по фикс цене» — прайс товаров, зеркало услуг, расчёт визита."""

from __future__ import annotations

import json
from datetime import date, datetime

import pytest

from app.db.models import (
    CatalogProduct,
    Client,
    MixSource,
    PayrollPeriod,
    Service,
    User,
    UserRole,
    UserRoleAssignment,
    VisitClientType,
    VisitMastersScope,
)
from app.fixed_price_visit import (
    FIXED_PRICE_VISIT_CATEGORY,
    catalog_product_for_visit_service,
    deactivate_fixed_price_mirror_service,
    ensure_fixed_price_visit_nodes,
    is_fixed_price_visit_service,
    sync_fixed_price_catalog_product,
)
from app.kit_inlay_visit import list_master_visit_services_catalog
from app.questionnaire.schemas import parse_visit_service_details
from app.visit_multi_service import (
    MultiServiceVisitInput,
    VisitHeaderInput,
    VisitServiceLineInput,
    compute_visit_service_line,
    save_visit_with_services,
)


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


def _seed_master(db) -> User:
    u = User(
        username="m1",
        password_hash="x",
        display_name="Master",
        role=UserRole.MASTER,
        is_active=True,
    )
    db.add(u)
    db.flush()
    db.add(UserRoleAssignment(user_id=u.id, role=UserRole.MASTER))
    db.add(
        PayrollPeriod(
            date_from=datetime(2020, 1, 1),
            date_to=datetime(2030, 12, 31, 23, 59, 59),
            closed_at=None,
        )
    )
    db.flush()
    return u


def _add_feathers(db, *, name: str = "Перья", price: float = 200, work: float = 80, expense: float = 20) -> CatalogProduct:
    row = CatalogProduct(
        category_name=FIXED_PRICE_VISIT_CATEGORY,
        subcategory_name=FIXED_PRICE_VISIT_CATEGORY,
        name=name,
        price=price,
        meta_json=json.dumps({"master_pay": work, "fixed_expense": expense}, ensure_ascii=False),
        sort_order=1,
        is_active=True,
    )
    db.add(row)
    db.flush()
    sync_fixed_price_catalog_product(db, row)
    db.flush()
    return row


def _header(client_id: int, master_id: int, *, self_client: bool = False) -> VisitHeaderInput:
    return VisitHeaderInput(
        client_mode="existing",
        existing_client_id=client_id,
        draft_name="",
        draft_phone="",
        draft_telegram="",
        draft_vk="",
        draft_instagram="",
        draft_other_contact="",
        client_type=VisitClientType.SELF if self_client else VisitClientType.RETURNING,
        performed_date=date.today(),
        duration_minutes=60,
        masters_scope=VisitMastersScope.VISIT,
        same_master_shares_all_services=False,
        visit_master_allocations=[(master_id, 100)],
    )


def _line(service_id: int, amount: float, qty: int | None = 5) -> VisitServiceLineInput:
    return VisitServiceLineInput(
        service_id=service_id,
        amount_from_client=amount,
        client_discount_percent=0,
        kanekalon_grams=0,
        kudri_grams=0,
        mix_source=MixSource.NO_MIX,
        mix_complexity=None,
        mix_bonus_master_id=None,
        amortization_level=None,
        kit_kind="STOCK",
        fixed_price_qty=qty,
    )


def test_ensure_category_and_sync_mirror_service(memory_db) -> None:
    db = memory_db
    assert ensure_fixed_price_visit_nodes(db) is True
    assert ensure_fixed_price_visit_nodes(db) is False
    row = _add_feathers(db)
    meta = json.loads(row.meta_json or "{}")
    sid = int(meta["mirror_service_id"])
    svc = db.get(Service, sid)
    assert svc is not None
    assert svc.name == "Перья"
    assert svc.is_active is True
    assert svc.price_middle_from == pytest.approx(200)
    db.refresh(svc, ["subcategory"])
    db.refresh(svc.subcategory, ["category"])
    assert is_fixed_price_visit_service(svc)
    assert catalog_product_for_visit_service(db, svc).id == row.id


def test_sync_rename_and_deactivate(memory_db) -> None:
    db = memory_db
    row = _add_feathers(db)
    sid = json.loads(row.meta_json)["mirror_service_id"]
    row.name = "Перья XL"
    sync_fixed_price_catalog_product(db, row)
    db.flush()
    svc = db.get(Service, sid)
    assert svc.name == "Перья XL"
    row.is_active = False
    sync_fixed_price_catalog_product(db, row)
    db.flush()
    assert db.get(Service, sid).is_active is False
    deactivate_fixed_price_mirror_service(db, row)
    assert db.get(Service, sid).is_active is False


def test_compute_fixed_price_split_not_salon_cut(memory_db) -> None:
    db = memory_db
    master = _seed_master(db)
    row = _add_feathers(db)
    sid = json.loads(row.meta_json)["mirror_service_id"]
    client = Client(name="C", phone="+79990000001", is_confirmed=True)
    db.add(client)
    db.commit()
    computed = compute_visit_service_line(
        db,
        _line(sid, 1000, qty=5),
        _header(client.id, master.id),
        default_mix_bonus_master_id=master.id,
        apply_kit_stock=False,
    )
    # Расход 20×5=100; ЗП 80×5=400; студия = 1000 − 100 − 400 = 500.
    # Обычный salon_cut 50% дал бы 450/450 — так быть не должно.
    assert computed.amount_from_client == pytest.approx(1000)
    assert computed.cost_total == pytest.approx(100)
    assert computed.masters_pool == pytest.approx(400)
    assert computed.salon_profit == pytest.approx(500)


def test_compute_empty_amount_uses_catalog_price(memory_db) -> None:
    db = memory_db
    master = _seed_master(db)
    row = _add_feathers(db)
    sid = json.loads(row.meta_json)["mirror_service_id"]
    client = Client(name="C2", phone="+79990000002", is_confirmed=True)
    db.add(client)
    db.commit()
    computed = compute_visit_service_line(
        db,
        _line(sid, 0, qty=5),
        _header(client.id, master.id),
        default_mix_bonus_master_id=master.id,
        apply_kit_stock=False,
    )
    assert computed.amount_from_client == pytest.approx(1000)
    assert computed.masters_pool == pytest.approx(400)
    assert computed.salon_profit == pytest.approx(500)


def test_compute_self_client_zero_still_pays_master(memory_db) -> None:
    db = memory_db
    master = _seed_master(db)
    row = _add_feathers(db)
    sid = json.loads(row.meta_json)["mirror_service_id"]
    client = Client(name="Self", phone="+79990000003", is_confirmed=True)
    db.add(client)
    db.commit()
    computed = compute_visit_service_line(
        db,
        _line(sid, 0, qty=5),
        _header(client.id, master.id, self_client=True),
        default_mix_bonus_master_id=master.id,
        apply_kit_stock=False,
    )
    assert computed.amount_from_client == pytest.approx(0)
    assert computed.cost_total == pytest.approx(100)
    assert computed.masters_pool == pytest.approx(400)
    assert computed.salon_profit == pytest.approx(-500)


def test_save_visit_persists_qty(memory_db) -> None:
    db = memory_db
    master = _seed_master(db)
    row = _add_feathers(db)
    sid = json.loads(row.meta_json)["mirror_service_id"]
    client = Client(name="V", phone="+79990000004", is_confirmed=True)
    db.add(client)
    db.commit()
    visit = save_visit_with_services(
        db,
        master.id,
        MultiServiceVisitInput(
            header=_header(client.id, master.id),
            lines=[_line(sid, 1000, qty=5)],
        ),
    )
    vs = visit.services[0]
    payload = parse_visit_service_details(json.loads(vs.details_json or "{}"))
    assert payload.fixed_price_qty == 5
    assert vs.masters_pool == pytest.approx(400)
    assert vs.salon_profit == pytest.approx(500)
    assert vs.cost_total == pytest.approx(100)


def test_visit_catalog_flags(memory_db) -> None:
    db = memory_db
    _add_feathers(db)
    catalog = list_master_visit_services_catalog(db)
    fp = next(c for c in catalog if c["name"] == FIXED_PRICE_VISIT_CATEGORY)
    assert fp["hide_subcategory"] is True
    assert fp["is_fixed_price_work"] is True
    svc = fp["subcategories"][0]["services"][0]
    assert svc["is_fixed_price_work"] is True
    assert svc["client_price"] == pytest.approx(200)
    assert svc["master_pay"] == pytest.approx(80)
    assert svc["fixed_expense"] == pytest.approx(20)
    assert svc["requires_kit_block"] is False
    assert svc["questionnaire_fields"] == []


def test_legacy_details_json_without_qty() -> None:
    payload = parse_visit_service_details(
        {"service_fields": {}, "kit": None, "answers": {}, "answer_labels": {}, "answer_display": {}}
    )
    assert payload.fixed_price_qty is None


def test_admin_catalog_excludes_fixed_price_category() -> None:
    from app.admin_service_catalog import _PRODUCT_CATALOG_ONLY_CATEGORIES

    assert "Работа по фикс цене" in _PRODUCT_CATALOG_ONLY_CATEGORIES
