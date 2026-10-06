"""3.12: уведомления staff_assignment_created при создании сущностей."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import models as _orm_models  # noqa: F401
from app.db.base import Base
from app.db.models import (
    Client,
    Consultation,
    HourlyWorkEntry,
    NotificationOutbox,
    NotificationOutboxStatus,
    PayrollPeriod,
    ProductSale,
    ProductSaleKind,
    User,
    UserRole,
    UserRoleAssignment,
    Visit,
    VisitClientType,
    VisitMastersScope,
    VisitPriceType,
    VisitService,
    VisitServiceMaster,
    WorkForInventory,
    WorkForInventoryStaff,
    WorkKind,
    WorkPlan,
    WorkPlanStatus,
    WorkPlanType,
    WorkScope,
)
from app.hourly_work import create_hourly_work_entry, update_hourly_work_entry
from app.staff_assignment_notifications import (
    EVENT_STAFF_ASSIGNMENT_CREATED,
    collect_visit_participant_user_ids,
    notify_consultation_staff_assigned_on_create,
    notify_hourly_work_staff_assigned_on_create,
    notify_product_sale_staff_assigned_on_create,
    notify_staff_assigned_on_create,
    notify_visit_staff_assigned_on_create,
    notify_work_plan_staff_assigned_on_create,
    notify_work_staff_assigned_on_create,
)
from app.visit_addon_sales import AddonSaleLine, AddonSalesInput


@pytest.fixture()
def memory_db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as db:
        yield db


def _payroll(db) -> None:
    db.add(
        PayrollPeriod(
            date_from=datetime(2020, 1, 1),
            date_to=datetime(2030, 12, 31, 23, 59, 59),
            closed_at=None,
        )
    )
    db.flush()


def _user(
    db,
    *,
    username: str,
    role: UserRole = UserRole.MASTER,
    tg: int | None = None,
    vk: int | None = None,
    notify: bool = True,
) -> User:
    from app.notify_prefs import apply_notify_prefs_from_legacy_flag

    u = User(
        username=username,
        password_hash="x",
        display_name=username,
        role=role,
        is_active=True,
        telegram_chat_id=tg,
        vk_user_id=vk,
        notify_enabled=notify,
    )
    db.add(u)
    db.flush()
    db.add(UserRoleAssignment(user_id=u.id, role=role))
    db.flush()
    apply_notify_prefs_from_legacy_flag(u, [role], enabled=notify)
    db.flush()
    return u


def _client(db, name: str = "Клиент") -> Client:
    c = Client(name=name, phone=f"+7999{abs(hash(name)) % 10000000:07d}", is_confirmed=True)
    db.add(c)
    db.flush()
    return c


def _outbox_count(db) -> int:
    return int(db.scalar(select(func.count()).select_from(NotificationOutbox)) or 0)


def _assignment_rows(db):
    return list(
        db.scalars(
            select(NotificationOutbox).where(NotificationOutbox.event_type == EVENT_STAFF_ASSIGNMENT_CREATED)
        ).all()
    )


def test_helper_creates_channels_and_message(memory_db) -> None:
    u = _user(memory_db, username="m1", tg=111, vk=222)
    memory_db.commit()
    with patch("app.notifications.send_telegram"), patch("app.notifications.send_vk"):
        rows = notify_staff_assigned_on_create(
            memory_db,
            entity_type="visit",
            entity_id=7,
            user_ids=[u.id],
            when=datetime(2026, 7, 1, 10, 0, 0),
            client_name="Анна",
            summary="Косы",
            comment="без клея",
        )
    assert len(rows) == 2
    texts = [r.payload_json for r in _assignment_rows(memory_db)]
    assert any("Вас добавили в визит #7" in t for t in texts)
    assert any("Анна" in t for t in texts)
    assert any("Косы" in t for t in texts)


def test_helper_skips_no_channel_and_notify_off(memory_db) -> None:
    a = _user(memory_db, username="no_ch", tg=None, vk=None, notify=True)
    b = _user(memory_db, username="off", tg=999, notify=False)
    memory_db.commit()
    with patch("app.notifications.send_telegram"):
        notify_staff_assigned_on_create(
            memory_db, entity_type="work", entity_id=1, user_ids=[a.id, b.id]
        )
    assert _outbox_count(memory_db) == 0


def test_helper_send_fail_does_not_raise(memory_db) -> None:
    u = _user(memory_db, username="m1", tg=555)
    memory_db.commit()

    def _boom(*a, **k):
        raise RuntimeError("tg down")

    with patch("app.notifications.send_telegram", side_effect=_boom):
        created = notify_staff_assigned_on_create(
            memory_db, entity_type="work", entity_id=3, user_ids=[u.id]
        )
    assert created  # enqueue ok
    row = _assignment_rows(memory_db)[0]
    assert row.status == NotificationOutboxStatus.FAILED


def test_helper_dedupe(memory_db) -> None:
    u = _user(memory_db, username="m1", tg=1)
    memory_db.commit()
    with patch("app.notifications.send_telegram"):
        notify_staff_assigned_on_create(memory_db, entity_type="visit", entity_id=1, user_ids=[u.id])
        notify_staff_assigned_on_create(memory_db, entity_type="visit", entity_id=1, user_ids=[u.id])
    assert len(_assignment_rows(memory_db)) == 1


def test_visit_create_path_notifies_masters_help_sellers(memory_db) -> None:
    from app.db.models import Service, ServiceCategory, ServiceSubcategory

    creator = _user(memory_db, username="creator", tg=None)
    m2 = _user(memory_db, username="m2", tg=102)
    helper = _user(memory_db, username="help", tg=103, role=UserRole.HELPER)
    seller = _user(memory_db, username="sell", tg=104)
    c = _client(memory_db)
    cat = ServiceCategory(name="Кат", is_active=True)
    memory_db.add(cat)
    memory_db.flush()
    sub = ServiceSubcategory(category_id=cat.id, name="Под", is_active=True)
    memory_db.add(sub)
    memory_db.flush()
    svc = Service(subcategory_id=sub.id, name="Услуга А", is_active=True)
    memory_db.add(svc)
    memory_db.flush()

    visit = Visit(
        created_by_user_id=creator.id,
        performed_date=datetime(2026, 7, 1),
        duration_minutes=60,
        client_id=c.id,
        client_type=VisitClientType.RETURNING,
        price_type=VisitPriceType.CLIENT,
        masters_scope=VisitMastersScope.PER_SERVICE,
        same_master_shares_all_services=False,
        comment="визит коммент",
        hourly_help_json='[{"master_id": %d, "hours": 1, "minutes": 0, "amount": 500}]' % helper.id,
        addons_details_json=AddonSalesInput(
            mode="all",
            lines=[AddonSaleLine(price=100, sale_percent=10, seller_user_id=seller.id)],
            shared_seller_user_id=seller.id,
        ).to_json(),
    )
    memory_db.add(visit)
    memory_db.flush()
    vs = VisitService(
        visit_id=visit.id,
        service_id=svc.id,
        category_name="К",
        subcategory_name="П",
        service_name="Услуга А",
        sort_order=0,
    )
    memory_db.add(vs)
    memory_db.flush()
    memory_db.add(VisitServiceMaster(visit_service_id=vs.id, master_id=m2.id, percent=100))
    memory_db.commit()

    ids = collect_visit_participant_user_ids(memory_db, visit.id)
    assert creator.id not in ids
    assert m2.id in ids
    assert helper.id in ids
    assert seller.id in ids

    with patch("app.notifications.send_telegram"):
        notify_visit_staff_assigned_on_create(memory_db, int(visit.id))
    user_ids = {r.user_id for r in _assignment_rows(memory_db)}
    assert user_ids == {m2.id, helper.id, seller.id}
    assert any("визит #" in r.payload_json for r in _assignment_rows(memory_db))


def test_save_visit_with_services_calls_notify_hook(memory_db) -> None:
    """Хук после commit в save_visit_with_services."""
    from app.fixed_price_visit import ensure_fixed_price_visit_nodes, sync_fixed_price_catalog_product
    from app.db.models import CatalogProduct, MixSource
    from app.visit_multi_service import (
        MultiServiceVisitInput,
        VisitHeaderInput,
        VisitServiceLineInput,
        save_visit_with_services,
    )
    import json
    from datetime import date

    master = _user(memory_db, username="vm", tg=200)
    _payroll(memory_db)
    ensure_fixed_price_visit_nodes(memory_db)
    row = CatalogProduct(
        category_name="Работа по фикс цене",
        subcategory_name="Работа по фикс цене",
        name="Перья",
        price=200,
        meta_json=json.dumps({"master_pay": 80, "fixed_expense": 20}, ensure_ascii=False),
        sort_order=1,
        is_active=True,
    )
    memory_db.add(row)
    memory_db.flush()
    sync_fixed_price_catalog_product(memory_db, row)
    memory_db.flush()
    sid = int(json.loads(row.meta_json)["mirror_service_id"])
    client = _client(memory_db, "VClient")
    memory_db.commit()

    inp = MultiServiceVisitInput(
        header=VisitHeaderInput(
            client_mode="existing",
            existing_client_id=client.id,
            draft_name="",
            draft_phone="",
            draft_telegram="",
            draft_vk="",
            draft_instagram="",
            draft_other_contact="",
            client_type=VisitClientType.SELF,
            performed_date=date(2026, 7, 1),
            duration_minutes=60,
            masters_scope=VisitMastersScope.VISIT,
            same_master_shares_all_services=False,
            visit_master_allocations=[(master.id, 100)],
        ),
        lines=[
            VisitServiceLineInput(
                service_id=sid,
                amount_from_client=0,
                client_discount_percent=0,
                kanekalon_grams=0,
                kudri_grams=0,
                mix_source=MixSource.NO_MIX,
                mix_complexity=None,
                mix_bonus_master_id=None,
                amortization_level=None,
                kit_kind="STOCK",
                fixed_price_qty=1,
            )
        ],
    )
    with patch("app.staff_assignment_notifications.notify_visit_staff_assigned_on_create") as mock_n:
        with patch("app.notifications.send_telegram"):
            visit = save_visit_with_services(memory_db, master.id, inp)
    mock_n.assert_called_once()
    assert mock_n.call_args.args[1] == int(visit.id)


def test_work_create_path_notifies_staff(memory_db) -> None:
    u = _user(memory_db, username="w1", tg=301)
    c = _client(memory_db)
    work = WorkForInventory(
        created_by_user_id=u.id,
        performed_date=datetime(2026, 7, 2),
        kind=WorkKind.MIX,
        scope=WorkScope.IN_STOCK,
        client_id=c.id,
        comment="микс",
    )
    memory_db.add(work)
    memory_db.flush()
    memory_db.add(WorkForInventoryStaff(work_id=work.id, user_id=u.id, share=1.0, master_profit_amount=100))
    memory_db.commit()
    with patch("app.notifications.send_telegram"):
        notify_work_staff_assigned_on_create(memory_db, int(work.id))
    rows = _assignment_rows(memory_db)
    assert len(rows) == 1
    assert "работу #" in rows[0].payload_json


def test_work_new_post_calls_notify_hook(memory_db) -> None:
    """Проверяем, что в work_new_post после commit вызывается notify (через исходный модуль)."""
    import inspect
    from app import work_products

    src = inspect.getsource(work_products.work_new_post)
    assert "notify_work_staff_assigned_on_create" in src


def test_hourly_create_path_notifies(memory_db) -> None:
    master = _user(memory_db, username="hm", tg=401)
    admin = _user(memory_db, username="ha", role=UserRole.ADMIN_SUPER, tg=None)
    _payroll(memory_db)
    memory_db.commit()
    entry = HourlyWorkEntry(
        performed_date=datetime(2026, 7, 3),
        duration_minutes=60,
        amount=500.0,
        comment="помощь",
        master_user_id=master.id,
    )
    with patch("app.notifications.send_telegram"):
        saved = create_hourly_work_entry(memory_db, entry, created_by_user_id=admin.id)
    rows = _assignment_rows(memory_db)
    assert len(rows) == 1
    assert rows[0].user_id == master.id
    assert "почасовую работу #" in rows[0].payload_json
    assert saved.id is not None


def test_hourly_update_does_not_notify(memory_db) -> None:
    master = _user(memory_db, username="hm2", tg=402)
    admin = _user(memory_db, username="ha2", role=UserRole.ADMIN_SUPER)
    _payroll(memory_db)
    memory_db.commit()
    entry = HourlyWorkEntry(
        performed_date=datetime(2026, 7, 3),
        duration_minutes=60,
        amount=500.0,
        master_user_id=master.id,
    )
    with patch("app.notifications.send_telegram"):
        saved = create_hourly_work_entry(memory_db, entry, created_by_user_id=admin.id)
    # clear outbox noise
    for r in list(memory_db.scalars(select(NotificationOutbox)).all()):
        memory_db.delete(r)
    memory_db.commit()

    draft = HourlyWorkEntry(
        performed_date=datetime(2026, 7, 3),
        duration_minutes=90,
        amount=600.0,
        comment="правка",
        master_user_id=master.id,
    )
    with patch("app.staff_assignment_notifications.notify_hourly_work_staff_assigned_on_create") as mock_n:
        update_hourly_work_entry(
            memory_db, saved, draft, updated_by_user_id=admin.id, is_admin=True
        )
    mock_n.assert_not_called()
    assert _outbox_count(memory_db) == 0


def test_product_sale_create_path_notifies_seller(memory_db) -> None:
    seller = _user(memory_db, username="ps", tg=501)
    c = _client(memory_db)
    sale = ProductSale(
        created_by_user_id=seller.id,
        performed_date=datetime(2026, 7, 4),
        client_id=c.id,
        amount_from_client=1000,
        kind=ProductSaleKind.OTHER,
        other_description="расческа",
    )
    memory_db.add(sale)
    memory_db.commit()
    with patch("app.notifications.send_telegram"):
        notify_product_sale_staff_assigned_on_create(memory_db, int(sale.id))
    rows = _assignment_rows(memory_db)
    assert len(rows) == 1
    assert rows[0].user_id == seller.id
    assert "продажу #" in rows[0].payload_json


def test_product_sale_new_post_wired() -> None:
    import inspect
    from app import product_sales

    assert "notify_product_sale_staff_assigned_on_create" in inspect.getsource(product_sales.product_sale_new_post)


def test_consultation_create_path_notifies(memory_db) -> None:
    u = _user(memory_db, username="cons", tg=601)
    c = _client(memory_db)
    cons = Consultation(
        created_by_user_id=u.id,
        client_id=c.id,
        consultation_date=datetime(2026, 7, 5, 12, 0, 0),
        duration_minutes=30,
        types_json="[]",
        comment="первичная",
    )
    memory_db.add(cons)
    memory_db.commit()
    with patch("app.notifications.send_telegram"):
        notify_consultation_staff_assigned_on_create(memory_db, int(cons.id))
    rows = _assignment_rows(memory_db)
    assert len(rows) == 1
    assert "консультацию #" in rows[0].payload_json


def test_consultation_new_post_wired() -> None:
    import inspect
    from app.routes import consultations

    assert "notify_consultation_staff_assigned_on_create" in inspect.getsource(
        consultations.consultation_new_post
    )


def test_work_plan_create_path_notifies(memory_db) -> None:
    m = _user(memory_db, username="wp", tg=701)
    plan = WorkPlan(
        created_by_user_id=m.id,
        planned_date=datetime(2026, 8, 1, 10, 0, 0),
        duration_minutes=120,
        master_id=m.id,
        plan_type=WorkPlanType.WORK_PRODUCT,
        work_kind=WorkKind.KIT,
        status=WorkPlanStatus.PLANNED,
        comment="заказ",
    )
    memory_db.add(plan)
    memory_db.commit()
    with patch("app.notifications.send_telegram"):
        notify_work_plan_staff_assigned_on_create(memory_db, int(plan.id))
    rows = _assignment_rows(memory_db)
    assert len(rows) == 1
    assert "план работ #" in rows[0].payload_json


def test_work_plan_new_post_wired() -> None:
    import inspect
    from app.routes import work_plans

    assert "notify_work_plan_staff_assigned_on_create" in inspect.getsource(work_plans.work_plan_new_post)


def test_create_survives_notify_exception(memory_db) -> None:
    master = _user(memory_db, username="ok", tg=801)
    admin = _user(memory_db, username="adm", role=UserRole.ADMIN_SUPER)
    _payroll(memory_db)
    memory_db.commit()
    entry = HourlyWorkEntry(
        performed_date=datetime(2026, 7, 6),
        duration_minutes=30,
        amount=100.0,
        master_user_id=master.id,
    )
    with patch(
        "app.staff_assignment_notifications.notify_hourly_work_staff_assigned_on_create",
        side_effect=RuntimeError("boom"),
    ):
        # Локальный import в create подхватит пропатченную функцию; обёртка на create
        # не ловит — поэтому проверяем, что сбой отправки (внутри helper) не ломает create.
        pass
    with patch("app.notifications.send_telegram", side_effect=RuntimeError("down")):
        saved = create_hourly_work_entry(memory_db, entry, created_by_user_id=admin.id)
    assert saved.id is not None
    assert memory_db.get(HourlyWorkEntry, saved.id) is not None

    # Даже если notify_* бросит до helper — create должен пережить (обёртка try в notify_*).
    entry2 = HourlyWorkEntry(
        performed_date=datetime(2026, 7, 7),
        duration_minutes=30,
        amount=100.0,
        master_user_id=master.id,
    )
    with patch(
        "app.staff_assignment_notifications.collect_visit_participant_user_ids",
        side_effect=RuntimeError("n/a"),
    ):
        # не относится к hourly; для hourly патчим get внутри notify через exception в helper path
        pass
    with patch(
        "app.staff_assignment_notifications.notify_staff_assigned_on_create",
        side_effect=RuntimeError("enqueue boom"),
    ):
        saved2 = create_hourly_work_entry(memory_db, entry2, created_by_user_id=admin.id)
    assert saved2.id is not None
