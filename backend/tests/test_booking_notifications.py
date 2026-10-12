"""3.4 / 3.9: уведомления мастерам при create/update/cancel брони + diff изменений."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, delete, func, select
from sqlalchemy.orm import sessionmaker

from app.booking_notifications import (
    notify_booking_cancelled,
    notify_booking_created,
    notify_booking_updated_with_master_diff,
)
from app.db import models as _orm_models  # noqa: F401
from app.db.base import Base
from app.db.models import (
    Booking,
    BookingKind,
    BookingMaster,
    BookingPlannedService,
    BookingStatus,
    Client,
    NotificationOutbox,
    NotificationOutboxStatus,
    Service,
    ServiceCategory,
    ServiceSubcategory,
    User,
    UserRole,
    VisitMastersScope,
)
from app.notifications import (
    EVENT_BOOKING_CANCELLED,
    EVENT_BOOKING_CREATED,
    EVENT_BOOKING_UPDATED,
    capture_booking_notify_snapshot,
    diff_booking_notify_snapshots,
)
from app.time_utils import utcnow_naive


@pytest.fixture()
def memory_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as db:
        yield db


def _seed(db, *, tg: int | None = 555, notify: bool = True, planned_delta_hours: int = 48, username: str = "m1"):
    from app.notify_prefs import apply_notify_prefs_from_legacy_flag

    u = User(
        username=username,
        password_hash="x",
        display_name="Мастер",
        role=UserRole.MASTER,
        is_active=True,
        telegram_chat_id=tg,
        notify_enabled=notify,
    )
    c = Client(name="Клиент", phone=f"+7999{abs(hash(username)) % 10000000:07d}", is_confirmed=True)
    db.add_all([u, c])
    db.flush()
    apply_notify_prefs_from_legacy_flag(u, [UserRole.MASTER], enabled=notify)
    db.commit()
    db.refresh(u)
    db.refresh(c)
    b = Booking(
        created_by_user_id=u.id,
        client_id=c.id,
        planned_date=utcnow_naive() + timedelta(hours=planned_delta_hours),
        kind=BookingKind.VISIT,
        status=BookingStatus.PENDING_CONFIRMATION,
        comment="тест",
        masters_scope=VisitMastersScope.VISIT,
        same_master_shares_all_services=False,
    )
    db.add(b)
    db.commit()
    db.refresh(b)
    db.add(BookingMaster(booking_id=b.id, master_id=u.id))
    db.commit()
    db.refresh(b)
    return u, b


def _make_services(db, names: list[str]) -> list[Service]:
    cat = ServiceCategory(name="Кат", is_active=True)
    db.add(cat)
    db.flush()
    sub = ServiceSubcategory(category_id=cat.id, name="Под", is_active=True)
    db.add(sub)
    db.flush()
    out: list[Service] = []
    for name in names:
        s = Service(subcategory_id=sub.id, name=name, is_active=True)
        db.add(s)
        out.append(s)
    db.commit()
    for s in out:
        db.refresh(s)
    return out


def _outbox_texts(db) -> list[str]:
    import json

    texts: list[str] = []
    for row in db.scalars(select(NotificationOutbox)).all():
        payload = json.loads(row.payload_json or "{}")
        texts.append(str(payload.get("text") or ""))
    return texts


def test_create_enqueues_and_sends(memory_db) -> None:
    u, b = _seed(memory_db)
    sent: list[tuple[int, str]] = []

    def _fake(chat_id: int, text: str) -> None:
        sent.append((chat_id, text))

    with patch("app.notifications.send_telegram", side_effect=_fake):
        notify_booking_created(memory_db, int(b.id))

    rows = list(memory_db.scalars(select(NotificationOutbox)).all())
    assert len(rows) == 1
    assert rows[0].event_type == EVENT_BOOKING_CREATED
    assert rows[0].status == NotificationOutboxStatus.SENT
    assert rows[0].user_id == u.id
    assert len(sent) == 1
    assert "создана" in sent[0][1]


def test_noop_resave_no_duplicate_updated(memory_db) -> None:
    u, b = _seed(memory_db)
    mids = [u.id]
    snap = capture_booking_notify_snapshot(memory_db, b)
    with patch("app.notifications.send_telegram"):
        notify_booking_updated_with_master_diff(
            memory_db, int(b.id), old_master_ids=mids, old_snapshot=snap
        )
        n1 = int(memory_db.scalar(select(func.count()).select_from(NotificationOutbox)) or 0)
        snap2 = capture_booking_notify_snapshot(memory_db, b)
        notify_booking_updated_with_master_diff(
            memory_db, int(b.id), old_master_ids=mids, old_snapshot=snap2
        )
        n2 = int(memory_db.scalar(select(func.count()).select_from(NotificationOutbox)) or 0)
    assert n1 == 0
    assert n2 == 0


def test_date_change_sends_new_updated(memory_db) -> None:
    u, b = _seed(memory_db)
    mids = [u.id]
    snap = capture_booking_notify_snapshot(memory_db, b)
    with patch("app.notifications.send_telegram"):
        b.planned_date = b.planned_date + timedelta(days=1)
        b.updated_at = utcnow_naive()
        memory_db.commit()
        notify_booking_updated_with_master_diff(
            memory_db, int(b.id), old_master_ids=mids, old_snapshot=snap
        )
        n = int(memory_db.scalar(select(func.count()).select_from(NotificationOutbox)) or 0)
    assert n == 1
    kinds = {r.event_type for r in memory_db.scalars(select(NotificationOutbox)).all()}
    assert EVENT_BOOKING_UPDATED in kinds
    text = _outbox_texts(memory_db)[0]
    assert "Что изменилось:" in text
    assert "→" in text
    assert "Дата/время:" in text


def test_cancel_notifies(memory_db) -> None:
    _, b = _seed(memory_db)
    b.status = BookingStatus.CANCELLED
    b.cancelled_at = utcnow_naive()
    memory_db.commit()
    with patch("app.notifications.send_telegram") as mock_send:
        notify_booking_cancelled(memory_db, int(b.id))
    row = memory_db.scalars(select(NotificationOutbox)).first()
    assert row is not None
    assert row.event_type == EVENT_BOOKING_CANCELLED
    assert mock_send.called


def test_send_error_does_not_break_flow(memory_db) -> None:
    _, b = _seed(memory_db)

    def _boom(chat_id: int, text: str) -> None:
        raise RuntimeError("telegram down")

    with patch("app.notifications.send_telegram", side_effect=_boom):
        notify_booking_created(memory_db, int(b.id))
    row = memory_db.scalars(select(NotificationOutbox)).first()
    assert row is not None
    assert row.status == NotificationOutboxStatus.FAILED
    assert "telegram down" in (row.error or "")


def test_no_chat_or_notify_disabled_skips(memory_db) -> None:
    _, b1 = _seed(memory_db, tg=None, notify=True, username="m_no_tg")
    with patch("app.notifications.send_telegram") as mock_send:
        notify_booking_created(memory_db, int(b1.id))
    assert (
        memory_db.scalar(
            select(func.count()).select_from(NotificationOutbox).where(NotificationOutbox.booking_id == b1.id)
        )
        == 0
    )
    assert not mock_send.called

    _, b2 = _seed(memory_db, tg=999, notify=False, username="m_off")
    with patch("app.notifications.send_telegram") as mock_send:
        notify_booking_created(memory_db, int(b2.id))
    rows = list(
        memory_db.scalars(select(NotificationOutbox).where(NotificationOutbox.booking_id == b2.id)).all()
    )
    assert rows == []
    assert not mock_send.called


def test_past_booking_skipped(memory_db) -> None:
    _, b = _seed(memory_db, planned_delta_hours=-5)
    with patch("app.notifications.send_telegram") as mock_send:
        notify_booking_created(memory_db, int(b.id))
    assert memory_db.scalar(select(func.count()).select_from(NotificationOutbox)) == 0
    assert not mock_send.called


def test_master_added_gets_created_removed_gets_unassign(memory_db) -> None:
    from app.notify_prefs import apply_notify_prefs_from_legacy_flag

    u1, b = _seed(memory_db, tg=111, username="m_a")
    u2 = User(
        username="m_b",
        password_hash="x",
        display_name="Мастер2",
        role=UserRole.MASTER,
        is_active=True,
        telegram_chat_id=222,
        notify_enabled=True,
    )
    memory_db.add(u2)
    memory_db.flush()
    apply_notify_prefs_from_legacy_flag(u2, [UserRole.MASTER], enabled=True)
    memory_db.commit()
    memory_db.refresh(u2)

    snap = capture_booking_notify_snapshot(memory_db, b)
    memory_db.execute(delete(BookingMaster).where(BookingMaster.booking_id == b.id))
    memory_db.add(BookingMaster(booking_id=b.id, master_id=u2.id))
    b.updated_at = utcnow_naive()
    memory_db.commit()

    with patch("app.notifications.send_telegram") as mock_send:
        notify_booking_updated_with_master_diff(
            memory_db, int(b.id), old_master_ids=[u1.id], old_snapshot=snap
        )

    rows = list(memory_db.scalars(select(NotificationOutbox)).all())
    by_user = {r.user_id: r for r in rows}
    assert by_user[u2.id].event_type == EVENT_BOOKING_CREATED
    assert by_user[u1.id].event_type == EVENT_BOOKING_CANCELLED
    texts = " ".join(call.args[1] for call in mock_send.call_args_list)
    assert "снята с вас" in texts
    assert "Что изменилось:" not in texts


def test_service_change_shows_added_and_removed(memory_db) -> None:
    u, b = _seed(memory_db)
    svc_a, svc_b = _make_services(memory_db, ["Услуга A", "Услуга B"])
    memory_db.add(BookingPlannedService(booking_id=b.id, service_id=svc_a.id, sort_order=0))
    memory_db.commit()
    snap = capture_booking_notify_snapshot(memory_db, b)

    memory_db.execute(delete(BookingPlannedService).where(BookingPlannedService.booking_id == b.id))
    memory_db.add(BookingPlannedService(booking_id=b.id, service_id=svc_b.id, sort_order=0))
    b.updated_at = utcnow_naive()
    memory_db.commit()

    with patch("app.notifications.send_telegram"):
        notify_booking_updated_with_master_diff(
            memory_db, int(b.id), old_master_ids=[u.id], old_snapshot=snap
        )
    text = _outbox_texts(memory_db)[0]
    assert "Что изменилось:" in text
    assert "− Услуга Услуга A" in text
    assert "+ Услуга Услуга B" in text


def test_comment_only_change(memory_db) -> None:
    u, b = _seed(memory_db)
    snap = capture_booking_notify_snapshot(memory_db, b)
    b.comment = "новый комментарий длинный"
    b.updated_at = utcnow_naive()
    memory_db.commit()
    with patch("app.notifications.send_telegram"):
        notify_booking_updated_with_master_diff(
            memory_db, int(b.id), old_master_ids=[u.id], old_snapshot=snap
        )
    text = _outbox_texts(memory_db)[0]
    assert "Комментарий: изменён" in text
    assert "→" in text
    assert "тест" in text
    assert "новый комментарий" in text


def test_multiple_changes_at_once(memory_db) -> None:
    u, b = _seed(memory_db)
    snap = capture_booking_notify_snapshot(memory_db, b)
    old_when = b.planned_date
    b.planned_date = old_when + timedelta(hours=2)
    b.comment = "и коммент"
    b.updated_at = utcnow_naive()
    memory_db.commit()
    with patch("app.notifications.send_telegram"):
        notify_booking_updated_with_master_diff(
            memory_db, int(b.id), old_master_ids=[u.id], old_snapshot=snap
        )
    text = _outbox_texts(memory_db)[0]
    assert "Дата/время:" in text and "→" in text
    assert "Комментарий: изменён" in text
    assert text.index("Что изменилось:") < text.index("Клиент:")


def test_insignificant_fields_no_notification(memory_db) -> None:
    u, b = _seed(memory_db)
    snap = capture_booking_notify_snapshot(memory_db, b)
    b.photo_1 = "/uploads/x.jpg"
    b.deposit_amount = 500
    b.quoted_price_text = "10000"
    b.status = BookingStatus.ACTIVE
    b.updated_at = utcnow_naive()
    memory_db.commit()
    with patch("app.notifications.send_telegram") as mock_send:
        notify_booking_updated_with_master_diff(
            memory_db, int(b.id), old_master_ids=[u.id], old_snapshot=snap
        )
    assert memory_db.scalar(select(func.count()).select_from(NotificationOutbox)) == 0
    assert not mock_send.called


def test_diff_arrow_for_datetime_utc() -> None:
    old = {
        "planned_date": "2026-10-05T10:00:00",
        "client_id": 1,
        "client_name": "Клиент",
        "kind": "VISIT",
        "planned_service_id": 0,
        "planned_product_kind": "",
        "comment": "",
        "master_ids": [1],
        "master_names": {"1": "Анна"},
        "service_names": [],
        "service_lines": [],
    }
    new = dict(old)
    new["planned_date"] = "2026-10-05T11:00:00"
    lines = diff_booking_notify_snapshots(old, new, tz_name="UTC")
    assert any("→" in x and "Дата/время:" in x for x in lines)
    assert "05.10.2026 10:00 → 05.10.2026 11:00" in lines[0]
