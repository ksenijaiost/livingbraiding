"""Fix 125: редактирование работы-комплекта — состав, мастера, ЗП."""

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
    Kit,
    KitAuthorStaff,
    KitBlankStock,
    KitReserve,
    MasterLevel,
    PayrollPeriod,
    User,
    UserRole,
    UserRoleAssignment,
    WorkForInventory,
    WorkForInventoryStaff,
    WorkKind,
    WorkScope,
)
from app.kit_blank_stock_core import blank_stock_qty_map
from app.work_kit_edit import (
    apply_kit_work_edit,
    details_lines_to_initial_lines,
    kit_master_ids_from_work,
    replace_work_staff_rows,
    work_kit_edit_template_extras,
)
from app.work_products import (
    _alloc_equal_shares_for_masters,
    _kit_cost_snapshot_text,
    _kit_stock_price_snapshot_text,
)


class _FakeForm(dict):
    def keys(self):
        return super().keys()

    def get(self, key, default=None):
        return super().get(key, default)

    def getlist(self, key):
        v = self.get(key)
        if v is None:
            return []
        if isinstance(v, list):
            return v
        return [v]


@pytest.fixture()
def memory_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as db:
        yield db


def _seed_two_masters_and_catalog(db) -> tuple[User, User]:
    db.add(
        PayrollPeriod(
            date_from=datetime(2020, 1, 1),
            date_to=datetime(2030, 12, 31, 23, 59, 59),
            closed_at=None,
        )
    )
    m1 = User(
        username="maria",
        password_hash="x",
        display_name="Мария",
        role=UserRole.MASTER,
        is_active=True,
    )
    m2 = User(
        username="yulya",
        password_hash="x",
        display_name="Юля",
        role=UserRole.MASTER,
        is_active=True,
    )
    db.add_all([m1, m2])
    db.flush()
    db.add_all(
        [
            UserRoleAssignment(user_id=m1.id, role=UserRole.MASTER),
            UserRoleAssignment(user_id=m2.id, role=UserRole.MASTER),
        ]
    )
    db.add(
        CatalogProduct(
            category_name="Заказ",
            subcategory_name="Заготовки поштучно",
            name="DE Thermo",
            price=80.0,
            meta_json='{"kit_key": "DE_THERMO_CURL", "master_pay": 25}',
            is_active=True,
        )
    )
    db.commit()
    return m1, m2


def _seed_kit_work(db, m1: User, m2: User) -> tuple[WorkForInventory, Kit]:
    now = datetime.utcnow()
    kit = Kit(
        sku="ORDER-99",
        title="Заказ — тест",
        blanks_condition="NEW",
        is_active=True,
        pieces_total=27,
        pieces_available=0,
        blank_type_se=False,
        blank_type_de=True,
        composition_json=json.dumps(
            [
                {
                    "key": "DE_THERMO_CURL",
                    "condition": "NEW",
                    "by_staff": {str(m1.id): 10, str(m2.id): 17},
                }
            ],
            ensure_ascii=False,
        ),
        stock_price_total=2160.0,
        cost_total=2000.0,
        is_in_stock=True,
        created_at=now,
    )
    db.add(kit)
    db.flush()
    db.add(KitBlankStock(kit_id=kit.id, kit_key="DE_THERMO_CURL", qty=27))
    db.add_all(
        [
            KitAuthorStaff(kit_id=kit.id, user_id=m1.id, sort_order=0),
            KitAuthorStaff(kit_id=kit.id, user_id=m2.id, sort_order=1),
        ]
    )
    details = {
        "kit": {
            "blank_type_se": False,
            "blank_type_de": True,
            "totals": {"DE_THERMO_CURL": 27},
            "by_staff": {str(m1.id): {"DE_THERMO_CURL": 10}, str(m2.id): {"DE_THERMO_CURL": 17}},
            "lines": [
                {
                    "key": "DE_THERMO_CURL",
                    "condition": "NEW",
                    "by_staff": {str(m1.id): 10, str(m2.id): 17},
                }
            ],
            "catalog_client_price": 2160.0,
        }
    }
    work = WorkForInventory(
        created_by_user_id=m1.id,
        performed_date=now.date(),
        kind=WorkKind.KIT,
        scope=WorkScope.CUSTOM_ORDER,
        client_id=None,
        kanekalon_grams=0.0,
        kudri_grams=160.0,
        materials_cost_total=1280.0,
        extra_costs_amount=0.0,
        cost_total_amount=1280.0,
        master_profit_amount=675.0,
        studio_profit_amount=0.0,
        profit_total_amount=675.0,
        details_json=json.dumps(details, ensure_ascii=False),
        created_kit_id=kit.id,
        created_at=now,
    )
    db.add(work)
    db.flush()
    db.add_all(
        [
            WorkForInventoryStaff(
                work_id=work.id,
                user_id=m1.id,
                share=0.5,
                master_profit_amount=250.0,
            ),
            WorkForInventoryStaff(
                work_id=work.id,
                user_id=m2.id,
                share=0.5,
                master_profit_amount=425.0,
            ),
        ]
    )
    db.commit()
    return work, kit


def test_details_lines_to_initial_lines() -> None:
    lines = details_lines_to_initial_lines(
        [{"key": "DE_THERMO_CURL", "condition": "NEW", "by_staff": {"3": 5, "7": 2}}]
    )
    assert lines[0]["key"] == "DE_THERMO_CURL"
    assert lines[0]["by_staff"] == {3: 5, 7: 2}


def test_kit_master_ids_from_work_staff_rows(memory_db) -> None:
    m1, m2 = _seed_two_masters_and_catalog(memory_db)
    work, _ = _seed_kit_work(memory_db, m1, m2)
    memory_db.refresh(work)
    work.staff_rows = list(
        memory_db.scalars(
            select(WorkForInventoryStaff).where(WorkForInventoryStaff.work_id == work.id)
        ).all()
    )
    ids = kit_master_ids_from_work(work, {})
    assert ids == [m1.id, m2.id]


def test_apply_kit_work_edit_updates_composition_and_stock(memory_db) -> None:
    m1, m2 = _seed_two_masters_and_catalog(memory_db)
    work, kit = _seed_kit_work(memory_db, m1, m2)

    form = _FakeForm(
        {
            "kit_type_se": "",
            "kit_type_de": "on",
            "kit_use_multi_masters": "on",
            "kit_master_on": [str(m1.id), str(m2.id)],
            "kit_line_0_key": "DE_THERMO_CURL",
            f"kit_line_0_qty_{m1.id}": "12",
            f"kit_line_0_qty_{m2.id}": "15",
            f"staff_profit_{m1.id}": "300",
            f"staff_profit_{m2.id}": "400",
        }
    )

    result = apply_kit_work_edit(
        memory_db,
        work,
        form,
        extra_costs_amount=0.0,
        cost_total_amount=1280.0,
        alloc_equal_shares_for_masters=_alloc_equal_shares_for_masters,
        kit_stock_price_snapshot_text=_kit_stock_price_snapshot_text,
        kit_cost_snapshot_text=_kit_cost_snapshot_text,
    )
    memory_db.commit()

    assert result.master_total == pytest.approx(700.0)
    assert result.staff_profits[m1.id] == pytest.approx(300.0)
    assert result.staff_profits[m2.id] == pytest.approx(400.0)

    memory_db.refresh(kit)
    assert kit.pieces_total == 27
    stock = blank_stock_qty_map(memory_db, int(kit.id))
    assert stock.get("DE_THERMO_CURL") == 27

    details = json.loads(work.details_json or "{}")
    line = details["kit"]["lines"][0]
    assert line["by_staff"][str(m1.id)] == 12
    assert line["by_staff"][str(m2.id)] == 15

    authors = list(
        memory_db.scalars(select(KitAuthorStaff).where(KitAuthorStaff.kit_id == kit.id)).all()
    )
    assert {a.user_id for a in authors} == {m1.id, m2.id}


def test_apply_kit_work_edit_custom_order_reserve_consumes_blank_stock(memory_db) -> None:
    m1, m2 = _seed_two_masters_and_catalog(memory_db)
    work, kit = _seed_kit_work(memory_db, m1, m2)
    client = Client(name="Оксана")
    memory_db.add(client)
    memory_db.flush()
    work.client_id = client.id
    memory_db.add(
        KitReserve(
            kit_id=kit.id,
            pieces_reserved=27,
            reserved_by_user_id=m1.id,
            reserved_for_client_id=client.id,
        )
    )
    memory_db.commit()

    form = _FakeForm(
        {
            "kit_type_se": "",
            "kit_type_de": "on",
            "kit_use_multi_masters": "on",
            "kit_master_on": [str(m1.id), str(m2.id)],
            "kit_line_0_key": "DE_THERMO_CURL",
            f"kit_line_0_qty_{m1.id}": "12",
            f"kit_line_0_qty_{m2.id}": "15",
            f"staff_profit_{m1.id}": "300",
            f"staff_profit_{m2.id}": "400",
        }
    )
    apply_kit_work_edit(
        memory_db,
        work,
        form,
        extra_costs_amount=0.0,
        cost_total_amount=1280.0,
        alloc_equal_shares_for_masters=_alloc_equal_shares_for_masters,
        kit_stock_price_snapshot_text=_kit_stock_price_snapshot_text,
        kit_cost_snapshot_text=_kit_cost_snapshot_text,
    )
    memory_db.commit()
    memory_db.refresh(kit)
    assert kit.pieces_total == 27
    assert int(kit.pieces_available) == 0
    assert blank_stock_qty_map(memory_db, int(kit.id)).get("DE_THERMO_CURL") == 0


def test_replace_work_staff_rows(memory_db) -> None:
    m1, m2 = _seed_two_masters_and_catalog(memory_db)
    work, _ = _seed_kit_work(memory_db, m1, m2)
    rows = replace_work_staff_rows(
        memory_db,
        work,
        [m1.id, m2.id],
        {m1.id: 100.0, m2.id: 200.0},
        _alloc_equal_shares_for_masters,
    )
    memory_db.commit()
    assert len(rows) == 2
    by_uid = {int(r.user_id): float(r.master_profit_amount) for r in rows}
    assert by_uid[m1.id] == pytest.approx(100.0)
    assert by_uid[m2.id] == pytest.approx(200.0)


def _auth_admin() -> "AuthUser":
    from app.auth import AuthUser

    return AuthUser(
        id=999,
        username="admin",
        display_name="Admin",
        role=UserRole.ADMIN_SUPER,
        roles=(UserRole.ADMIN_SUPER,),
        master_level=MasterLevel.MIDDLE,
    )


def test_work_kit_edit_template_extras_initial_lines(memory_db) -> None:
    m1, m2 = _seed_two_masters_and_catalog(memory_db)
    work, kit = _seed_kit_work(memory_db, m1, m2)
    memory_db.refresh(work)
    work.staff_rows = list(
        memory_db.scalars(
            select(WorkForInventoryStaff).where(WorkForInventoryStaff.work_id == work.id)
        ).all()
    )

    def _builder(*, masters, initial_lines):
        return json.dumps({"initialLines": initial_lines, "masters": [{"id": u.id, "name": u.display_name} for u in masters]})

    extras = work_kit_edit_template_extras(
        memory_db,
        work,
        kit_table_state_json_builder=_builder,
        list_masters=lambda db: [m1, m2],
    )
    state = json.loads(extras["kit_table_state_json"])
    assert len(state["initialLines"]) == 1
    assert state["initialLines"][0]["key"] == "DE_THERMO_CURL"
    by_staff = state["initialLines"][0]["by_staff"]
    assert by_staff.get(m1.id) == 10 or by_staff.get(str(m1.id)) == 10
    assert by_staff.get(m2.id) == 17 or by_staff.get(str(m2.id)) == 17
    assert {m["id"] for m in state["masters"]} == {m1.id, m2.id}


def test_work_kit_edit_template_extras_fallback_from_kit_composition(memory_db) -> None:
    m1, m2 = _seed_two_masters_and_catalog(memory_db)
    work, kit = _seed_kit_work(memory_db, m1, m2)
    work.details_json = json.dumps({"kit": {"blank_type_de": True, "lines": []}})
    memory_db.commit()

    def _builder(*, masters, initial_lines):
        return json.dumps({"initialLines": initial_lines})

    extras = work_kit_edit_template_extras(
        memory_db,
        work,
        kit_table_state_json_builder=_builder,
        list_masters=lambda db: [m1, m2],
    )
    state = json.loads(extras["kit_table_state_json"])
    by_staff = state["initialLines"][0]["by_staff"]
    assert by_staff.get(m1.id) == 10 or by_staff.get(str(m1.id)) == 10


def test_work_edit_save_kit_integration(memory_db) -> None:
    import asyncio
    from urllib.parse import urlencode

    from starlette.requests import Request

    from app.work_products import work_edit_save

    m1, m2 = _seed_two_masters_and_catalog(memory_db)
    work, kit = _seed_kit_work(memory_db, m1, m2)

    fields = {
        "amount_from_client": "",
        "comment": work.comment or "",
        "kanekalon_grams": "0",
        "kudri_grams": "160",
        "materials_cost_total": "1280",
        "extra_costs_amount": "0",
        "cost_total_amount": "1280",
        "studio_profit_amount": "0",
        "profit_total_amount": "710",
        "kit_type_de": "on",
        "kit_use_multi_masters": "on",
        "kit_master_on": [str(m1.id), str(m2.id)],
        "kit_line_0_key": "DE_THERMO_CURL",
        f"kit_line_0_qty_{m1.id}": "8",
        f"kit_line_0_qty_{m2.id}": "19",
        f"staff_profit_{m1.id}": "210",
        f"staff_profit_{m2.id}": "500",
    }
    body = urlencode(fields, doseq=True).encode("utf-8")

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    scope = {
        "type": "http",
        "method": "POST",
        "path": f"/sales/work/{work.id}/edit",
        "headers": [(b"content-type", b"application/x-www-form-urlencoded")],
        "query_string": b"",
    }
    request = Request(scope, receive)
    resp = asyncio.run(work_edit_save(request, work.id, _auth_admin(), memory_db))
    assert resp.status_code == 303

    memory_db.refresh(work)
    memory_db.refresh(kit)
    assert work.master_profit_amount == pytest.approx(710.0)
    staff = list(
        memory_db.scalars(
            select(WorkForInventoryStaff).where(WorkForInventoryStaff.work_id == work.id)
        ).all()
    )
    profits = {int(s.user_id): float(s.master_profit_amount) for s in staff}
    assert profits[m1.id] == pytest.approx(210.0)
    assert profits[m2.id] == pytest.approx(500.0)
    details = json.loads(work.details_json or "{}")
    assert details["kit"]["lines"][0]["by_staff"][str(m1.id)] == 8
    assert blank_stock_qty_map(memory_db, int(kit.id)).get("DE_THERMO_CURL") == 27
