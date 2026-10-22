"""3.45: пустой suggest комплектов не сканирует склад поштучно."""

from __future__ import annotations

import time
from datetime import datetime

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.db import models as _orm_models  # noqa: F401
from app.db.base import Base
from app.db.models import Client, Kit, KitBlankStock, KitReserve, User, UserRole
from app.kit_blank_stock_core import blank_stock_qty_map, kit_inventory_is_keyed, load_catalog_kit_maps
from app.kit_inlay_visit import EMPTY_SUGGEST_LIMIT, suggest_kits_for_stock


def _db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _seed(db):
    user = User(
        username="yulya",
        password_hash="x",
        display_name="Юля",
        role=UserRole.MASTER,
        is_active=True,
    )
    client = Client(name="Клиентка", is_confirmed=True)
    other = Client(name="Другая", is_confirmed=True)
    db.add_all([user, client, other])
    db.flush()
    now = datetime(2026, 10, 21, 12, 0, 0)
    kits = []
    for i in range(80):
        kit = Kit(
            sku=f"K{i:04d}",
            title=f"Комплект {i}",
            pieces_total=10,
            pieces_available=0 if i == 0 else 4,
            stock_price_total=1000.0,
            composition_json='[{"key":"SE_BRAID_LONG","qty":4}]',
            created_at=now,
            is_in_stock=i != 0,
            is_active=True,
            is_archived=False,
        )
        db.add(kit)
        kits.append(kit)
    db.flush()
    db.add(KitBlankStock(kit_id=kits[0].id, kit_key="SE_BRAID_LONG", qty=0))
    for kit in kits[1:21]:
        db.add(KitBlankStock(kit_id=kit.id, kit_key="SE_BRAID_LONG", qty=4))
    db.add(
        KitReserve(
            kit_id=kits[0].id,
            pieces_reserved=3,
            reserved_at=now,
            reserved_by_user_id=user.id,
            reserved_for_client_id=client.id,
        )
    )
    for n in range(300):
        db.add(
            KitReserve(
                kit_id=kits[(n % 70) + 1].id,
                pieces_reserved=1,
                reserved_at=now,
                reserved_by_user_id=user.id,
                reserved_for_client_id=other.id,
            )
        )
    db.commit()
    return client, kits


def test_empty_suggest_for_client_with_history_is_short_and_fast() -> None:
    db = _db()
    client, kits = _seed(db)
    statements: list[str] = []

    def _count(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(db.get_bind(), "before_cursor_execute", _count)
    started = time.perf_counter()
    rows = suggest_kits_for_stock(db, "", for_client_id=int(client.id))
    elapsed = time.perf_counter() - started
    event.remove(db.get_bind(), "before_cursor_execute", _count)

    assert len(rows) <= EMPTY_SUGGEST_LIMIT
    assert rows[0]["id"] == kits[0].id
    assert rows[0]["reserved_for_selected_client"] == 3
    assert elapsed < 1.0
    suggest_queries = len(statements)

    naive_statements: list[str] = []

    def _count_naive(conn, cursor, statement, parameters, context, executemany):
        naive_statements.append(statement)

    event.listen(db.get_bind(), "before_cursor_execute", _count_naive)
    naive_started = time.perf_counter()
    for kit in kits[:60]:
        kit_inventory_is_keyed(db, int(kit.id))
        blank_stock_qty_map(db, int(kit.id))
        load_catalog_kit_maps(db)
    naive_elapsed = time.perf_counter() - naive_started
    event.remove(db.get_bind(), "before_cursor_execute", _count_naive)

    assert suggest_queries < 15
    assert len(naive_statements) > suggest_queries * 5
    print(f"suggest {elapsed:.4f}s queries={suggest_queries}; naive {naive_elapsed:.4f}s queries={len(naive_statements)}")


def test_repeat_empty_suggest_does_not_grow_work() -> None:
    db = _db()
    client, _kits = _seed(db)
    first = suggest_kits_for_stock(db, "", for_client_id=int(client.id))
    statements: list[str] = []

    def _count(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(db.get_bind(), "before_cursor_execute", _count)
    second = suggest_kits_for_stock(db, "", for_client_id=int(client.id))
    event.remove(db.get_bind(), "before_cursor_execute", _count)
    assert [row["id"] for row in second] == [row["id"] for row in first]
    assert len(statements) < 15
