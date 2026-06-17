from __future__ import annotations

import json
from datetime import datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db import models as _orm_models  # noqa: F401
from app.db.base import Base
from app.db.models import (
    PayrollPeriod,
    User,
    UserRole,
    UserRoleAssignment,
    WorkForInventory,
    WorkKind,
)
from app.work_products import (
    _resolve_rubber_type_from_form,
    _rubber_family_size_from_type,
)


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


def test_rubber_family_size_split_and_resolve() -> None:
    assert _rubber_family_size_from_type("TAIL_CRAB_MINI") == ("TAIL_CRAB", "MINI")
    assert _rubber_family_size_from_type("TAIL_ELASTIC") == ("TAIL_ELASTIC", "")

    form = _FakeForm({"rubber_family": "TAIL_NET", "rubber_size": "STANDARD"})
    assert _resolve_rubber_type_from_form(form) == "TAIL_NET_STANDARD"

    with pytest.raises(ValueError, match="размер"):
        _resolve_rubber_type_from_form(_FakeForm({"rubber_family": "TAIL_BUN"}))


def _seed_master(db) -> User:
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
    db.commit()
    return u


def _auth_user(master: User):
    from app.auth import AuthUser
    from app.db.models import MasterLevel

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


def test_post_work_other_kind(memory_db) -> None:
    from app.db.models import CatalogProduct

    master = _seed_master(memory_db)
    memory_db.add(
        CatalogProduct(
            category_name="Заказ",
            subcategory_name="Другое",
            name="Другое — простое",
            price=100.0,
            meta_json=json.dumps({"master_pay": 0.0, "studio_pay": 0.0, "fixed_expense": 0.0}),
            sort_order=1,
            is_active=True,
        )
    )
    memory_db.commit()
    other_id = int(memory_db.scalar(select(CatalogProduct.id)).__int__())
    resp = _post_work_form(
        memory_db,
        master,
        {
            "performed_date": "2025-05-21",
            "scope": "IN_STOCK",
            "kind": "OTHER",
            "other_product_id": str(other_id),
            "comment": "Прочие работы",
            "kanekalon_grams": "10",
        },
    )
    assert resp.status_code == 303
    work = memory_db.scalar(select(WorkForInventory).order_by(WorkForInventory.id.desc()))
    assert work is not None
    assert work.kind == WorkKind.OTHER
    assert work.comment == "Прочие работы"


def test_post_work_other_self_mixed(memory_db) -> None:
    from app.db.models import MixComplexity, WorkForInventoryStaff, WorkRate
    from app.mix_rates import mix_complexity_rate_map
    from app.work_rate_keys import MIX_STANDARD

    master = _seed_master(memory_db)
    memory_db.add(
        WorkRate(
            key=MIX_STANDARD,
            value_json="2.5",
            is_active=True,
        )
    )
    memory_db.commit()
    assert mix_complexity_rate_map(memory_db).get(MixComplexity.STANDARD) == pytest.approx(2.5)

    from app.db.models import CatalogProduct

    memory_db.add(
        CatalogProduct(
            category_name="Заказ",
            subcategory_name="Другое",
            name="Другое — тест",
            price=1000.0,
            meta_json=json.dumps({"master_pay": 100.0, "studio_pay": 0.0, "fixed_expense": 10.0}),
            sort_order=1,
            is_active=True,
        )
    )
    memory_db.commit()
    other_id = int(memory_db.scalar(select(CatalogProduct.id)).__int__())

    resp = _post_work_form(
        memory_db,
        master,
        {
            "performed_date": "2025-05-21",
            "scope": "IN_STOCK",
            "kind": "OTHER",
            "other_product_id": str(other_id),
            "kanekalon_grams": "100",
            "mix_source": "SELF_MIXED",
            "mix_complexity": "STANDARD",
        },
    )
    assert resp.status_code == 303
    work = memory_db.scalar(select(WorkForInventory).order_by(WorkForInventory.id.desc()))
    assert work is not None
    assert work.kind == WorkKind.OTHER
    details = json.loads(work.details_json or "{}")
    assert details.get("mix_complexity") == "STANDARD"

    staff = memory_db.scalar(
        select(WorkForInventoryStaff).where(WorkForInventoryStaff.work_id == work.id)
    )
    assert staff is not None
    # 100 (работа из прайса) + 100*2.5 (смешка) = 350
    assert float(staff.master_profit_amount or 0) == pytest.approx(350.0)


def test_post_work_rubber_family_size(memory_db) -> None:
    from app.db.models import CatalogProduct

    master = _seed_master(memory_db)
    memory_db.add(
        CatalogProduct(
            category_name="Заказ",
            subcategory_name="Хвосты/резинки",
            name="Хвост на крабе — mini",
            price=500.0,
            meta_json=json.dumps({"master_pay": 100.0, "studio_pay": 50.0, "fixed_expense": 10.0}),
            is_active=True,
        )
    )
    memory_db.commit()

    resp = _post_work_form(
        memory_db,
        master,
        {
            "performed_date": "2025-05-21",
            "scope": "IN_STOCK",
            "kind": "RUBBER",
            "rubber_family": "TAIL_CRAB",
            "rubber_size": "MINI",
        },
    )
    assert resp.status_code == 303
    work = memory_db.scalar(select(WorkForInventory).order_by(WorkForInventory.id.desc()))
    assert work is not None
    assert work.kind == WorkKind.RUBBER
    details = json.loads(work.details_json or "{}")
    assert details["rubber"]["type"] == "TAIL_CRAB_MINI"
