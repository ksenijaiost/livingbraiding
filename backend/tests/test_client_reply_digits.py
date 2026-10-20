"""3.34: настраиваемые цифры ответов и {{reply_hint}}."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.client_notifications import DEFAULT_BEFORE_TEMPLATE, render_client_notify_template
from app.client_reply_digits import (
    ACTION_CONFIRM,
    DigitConflictError,
    LEGACY_REPLY_HINT_LINE,
    build_reply_hint,
    ensure_default_reply_digits,
    migrate_replace_legacy_reply_hint,
    save_reply_digits_from_form,
)
from app.db import models as _orm_models  # noqa: F401
from app.db.base import Base
from app.db.models import (
    Booking,
    BookingKind,
    BookingStatus,
    Client,
    ClientNotificationRule,
    ClientNotificationRuleKind,
    ClientNotificationSend,
    ClientReplyDigitAction,
    User,
    UserRole,
    VisitMastersScope,
)
from app.messenger_inbound import (
    MSG_ALREADY_CONFIRMED,
    MSG_CONFIRMED,
    handle_client_text,
)
from app.client_booking_notify import EVENT_CLIENT_BOOKING_REMINDER
from app.time_utils import utcnow_naive


@pytest.fixture()
def memory_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as db:
        yield db


def _seed_client(db, *, with_send: bool = True):
    ensure_default_reply_digits(db)
    user = User(
        username="adm",
        password_hash="x",
        display_name="Админ",
        role=UserRole.ADMIN,
        is_active=True,
    )
    client = Client(
        name="Катя",
        is_confirmed=True,
        vk_user_id=1001,
        max_user_id=2002,
        telegram_chat_id=3003,
    )
    db.add_all([user, client])
    db.commit()
    booking = Booking(
        created_by_user_id=user.id,
        client_id=client.id,
        planned_date=utcnow_naive() + timedelta(hours=20),
        kind=BookingKind.VISIT,
        status=BookingStatus.PENDING_CONFIRMATION,
        masters_scope=VisitMastersScope.VISIT,
        same_master_shares_all_services=False,
    )
    db.add(booking)
    db.commit()
    if with_send:
        db.add(
            ClientNotificationSend(
                client_id=client.id,
                booking_id=booking.id,
                channel="vk",
                event_type=EVENT_CLIENT_BOOKING_REMINDER,
                target_id=1001,
                sent_at=utcnow_naive(),
            )
        )
        db.commit()
    return user, client, booking


def test_swap_1_and_4_changes_actions(memory_db) -> None:
    _user, _client, booking = _seed_client(memory_db)
    ensure_default_reply_digits(memory_db)
    save_reply_digits_from_form(
        memory_db,
        actions=["confirm", "reschedule", "cancel", "contact_admin", "directions"],
        digits=["4", "2", "3", "1", "5"],
        enabled_flags=["1", "1", "1", "1", "1"],
        labels=["подтвердить", "перенести", "отменить", "написать администратору", "как добраться"],
    )
    memory_db.commit()

    ans4 = handle_client_text(memory_db, channel="vk", messenger_id=1001, text="4")
    memory_db.commit()
    memory_db.refresh(booking)
    assert ans4 == MSG_CONFIRMED
    assert booking.client_confirmed_at is not None

    ans1 = handle_client_text(memory_db, channel="vk", messenger_id=1001, text="1")
    memory_db.commit()
    assert "Администратор скоро напишет" in (ans1 or "")

    hint = build_reply_hint(memory_db)
    assert "4 — подтвердить" in hint
    assert "1 — написать администратору" in hint
    assert hint.startswith("Ответьте:")


def test_disabled_action_shows_hint_and_drops_from_reply_hint(memory_db) -> None:
    _seed_client(memory_db)
    save_reply_digits_from_form(
        memory_db,
        actions=["confirm", "reschedule", "cancel", "contact_admin", "directions"],
        digits=["1", "2", "3", "4", "5"],
        enabled_flags=["0", "1", "1", "1", "1"],
        labels=["подтвердить", "перенести", "отменить", "написать администратору", "как добраться"],
    )
    memory_db.commit()
    hint = build_reply_hint(memory_db)
    assert "подтвердить" not in hint
    assert "2 — перенести" in hint
    ans = handle_client_text(memory_db, channel="vk", messenger_id=1001, text="1")
    assert ans == hint


def test_reply_hint_variable_in_template(memory_db) -> None:
    ensure_default_reply_digits(memory_db)
    memory_db.commit()
    client = Client(name="Катя", is_confirmed=True)
    memory_db.add(client)
    memory_db.commit()
    booking = type(
        "B",
        (),
        {
            "planned_date": utcnow_naive() + timedelta(hours=5),
            "planned_services": [],
            "planned_service": None,
            "masters": [],
        },
    )()
    text = render_client_notify_template(
        "Привет, {{name}}. {{reply_hint}}",
        booking=booking,  # type: ignore[arg-type]
        client=client,
        tz_name="UTC",
        db=memory_db,
    )
    assert "Привет, Катя." in text
    assert "{{reply_hint}}" not in text
    assert "1 — подтвердить" in text
    assert DEFAULT_BEFORE_TEMPLATE.endswith("{{reply_hint}}")


def test_digit_conflict_raises_field_errors(memory_db) -> None:
    ensure_default_reply_digits(memory_db)
    with pytest.raises(DigitConflictError) as caught:
        save_reply_digits_from_form(
            memory_db,
            actions=["confirm", "reschedule", "cancel", "contact_admin", "directions"],
            digits=["1", "1", "3", "4", "5"],
            enabled_flags=["1", "1", "1", "1", "1"],
            labels=["подтвердить", "перенести", "отменить", "написать администратору", "как добраться"],
        )
    assert ACTION_CONFIRM in caught.value.field_errors
    assert "reschedule" in caught.value.field_errors


def test_migration_replaces_exact_legacy_line(memory_db) -> None:
    ensure_default_reply_digits(memory_db)
    rule = ClientNotificationRule(
        kind=ClientNotificationRuleKind.BEFORE_BOOKING,
        hours=24,
        template=f"Добрый день.\n\n{LEGACY_REPLY_HINT_LINE}",
        position=0,
        is_enabled=True,
    )
    custom = ClientNotificationRule(
        kind=ClientNotificationRuleKind.BEFORE_BOOKING,
        hours=2,
        template="Ответьте: 1 — ок, 2 — нет",
        position=1,
        is_enabled=True,
    )
    memory_db.add_all([rule, custom])
    memory_db.commit()
    report = migrate_replace_legacy_reply_hint(memory_db.connection())
    memory_db.commit()
    memory_db.refresh(rule)
    memory_db.refresh(custom)
    assert "{{reply_hint}}" in rule.template
    assert LEGACY_REPLY_HINT_LINE not in rule.template
    assert int(rule.id) in report["replaced"]
    assert "Ответьте: 1 — ок" in custom.template
    assert int(custom.id) in report["manual"]
