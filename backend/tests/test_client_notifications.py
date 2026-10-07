"""3.17: настройки клиентских уведомлений — render шаблона и дефолты правил."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.client_notifications import (
    DEFAULT_AFTER_TEMPLATE,
    DEFAULT_SERVICES_MARKER,
    KIND_AFTER,
    KIND_BEFORE,
    ensure_default_rules,
    get_services_marker,
    parse_rule_hours,
    render_client_notify_template,
    rules_for_ui,
)
from app.db import models as _orm_models  # noqa: F401
from app.db.base import Base
from app.db.models import ClientNotificationRule, ClientNotificationRuleKind


@pytest.fixture()
def memory_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as db:
        yield db


def _booking_stub(*, planned_utc: datetime | None = None):
    master = SimpleNamespace(id=10, display_name="Анна", username="anna")
    master2 = SimpleNamespace(id=11, display_name="", username="masha")
    svc = SimpleNamespace(name="Электроэпиляция")
    ps = SimpleNamespace(
        id=1,
        sort_order=0,
        service=svc,
        duration_minutes=1,
        masters=[SimpleNamespace(master_id=10, master=master)],
    )
    ps2 = SimpleNamespace(
        id=2,
        sort_order=1,
        service=SimpleNamespace(name="Тонирование"),
        duration_minutes=None,
        masters=[SimpleNamespace(master_id=11, master=master2)],
    )
    return SimpleNamespace(
        planned_date=planned_utc or datetime(2026, 9, 16, 5, 0, 0),  # 16.09.2026 12:00 Asia/Novosibirsk (UTC+7)
        planned_services=[ps, ps2],
        planned_service=None,
        masters=[
            SimpleNamespace(master_id=10, master=master),
            SimpleNamespace(master_id=11, master=master2),
        ],
    )


def test_render_all_variables() -> None:
    booking = _booking_stub()
    client = SimpleNamespace(name="Катя")
    text = render_client_notify_template(
        "Добрый день, {{name}}!\n"
        "{{date}}|{{time}}|{{datetime}}\n"
        "{{date_text}} ({{weekday}})\n"
        "{{services}}\n"
        "Мастер: {{masters}}\n"
        "raw={{unknown}}",
        booking=booking,  # type: ignore[arg-type]
        client=client,  # type: ignore[arg-type]
        tz_name="Asia/Novosibirsk",
        marker="•",
    )
    assert "Добрый день, Катя!" in text
    assert "16.09.2026|12:00|16.09.2026 12:00" in text
    assert "16 сентября (среда)" in text
    assert "• Электроэпиляция 1 минута" in text
    assert "• Тонирование" in text
    assert "Мастер: Анна, masha" in text
    assert "raw={{unknown}}" in text


def test_render_empty_services_masters() -> None:
    booking = SimpleNamespace(
        planned_date=datetime(2026, 1, 1, 0, 0, 0),
        planned_services=[],
        planned_service=None,
        masters=[],
    )
    client = SimpleNamespace(name="X")
    text = render_client_notify_template(
        "S={{services}};M={{masters}}",
        booking=booking,  # type: ignore[arg-type]
        client=client,  # type: ignore[arg-type]
        tz_name="UTC",
        marker="*",
    )
    assert text == "S=;M="


def test_parse_hours_before_after() -> None:
    assert parse_rule_hours(24, kind=KIND_BEFORE) == 24
    assert parse_rule_hours(0, kind=KIND_AFTER) == 0
    with pytest.raises(ValueError, match="больше 0"):
        parse_rule_hours(0, kind=KIND_BEFORE)
    with pytest.raises(ValueError, match="целым"):
        parse_rule_hours("1.5", kind=KIND_BEFORE)


def test_ensure_defaults(memory_db) -> None:
    assert ensure_default_rules(memory_db) is True
    memory_db.commit()
    rows = list(memory_db.scalars(select(ClientNotificationRule)).all())
    assert len(rows) == 3
    kinds = {r.kind for r in rows}
    assert kinds == {
        ClientNotificationRuleKind.BEFORE_BOOKING,
        ClientNotificationRuleKind.AFTER_BOOKING,
    }
    after = [r for r in rows if r.kind == KIND_AFTER]
    assert after[0].hours == 0
    assert after[0].template == DEFAULT_AFTER_TEMPLATE
    assert get_services_marker(memory_db) == DEFAULT_SERVICES_MARKER
    assert ensure_default_rules(memory_db) is False


def test_rules_for_ui_after_seed(memory_db) -> None:
    ensure_default_rules(memory_db)
    memory_db.commit()
    data = rules_for_ui(memory_db)
    assert len(data["before"]) == 2
    assert len(data["after"]) == 1
    assert data["before"][0]["hours"] == 24
    assert data["before"][1]["hours"] == 2
