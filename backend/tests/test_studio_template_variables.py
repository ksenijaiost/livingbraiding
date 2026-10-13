"""3.21: переменные студии в шаблонах уведомлений."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.client_notifications import render_client_notify_template
from app.db import models as _orm_models  # noqa: F401
from app.db.base import Base
from app.db.models import StudioTemplateVariable
from app.db.session import get_db
from app.main import app
from app.studio_template_variables import (
    add_custom_variable,
    delete_variable,
    ensure_builtin_studio_variables,
    normalize_custom_key,
    save_builtin_values,
)


@pytest.fixture()
def memory_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as db:
        yield db


def _booking():
    return SimpleNamespace(
        planned_date=datetime(2026, 9, 16, 5, 0, 0),
        planned_services=[],
        planned_service=None,
        masters=[],
    )


def test_custom_key_validation() -> None:
    assert normalize_custom_key("Park_1") == "park_1"
    with pytest.raises(ValueError, match="2 до 32"):
        normalize_custom_key("a")
    with pytest.raises(ValueError, match="2 до 32"):
        normalize_custom_key("адрес")
    with pytest.raises(ValueError, match="системн"):
        normalize_custom_key("date")
    with pytest.raises(ValueError, match="встроенн"):
        normalize_custom_key("address")


def test_builtin_cannot_be_deleted_custom_can(memory_db) -> None:
    ensure_builtin_studio_variables(memory_db)
    add_custom_variable(memory_db, "parking", "двор", user_id=None)
    memory_db.commit()
    with pytest.raises(ValueError, match="нельзя удалить"):
        delete_variable(memory_db, "address")
    delete_variable(memory_db, "parking")
    memory_db.commit()
    left = memory_db.scalar(select(StudioTemplateVariable).where(StudioTemplateVariable.key == "parking"))
    assert left is None
    address = memory_db.scalar(select(StudioTemplateVariable).where(StudioTemplateVariable.key == "address"))
    assert address is not None and address.is_builtin is True


def test_render_studio_and_custom_before_and_after(memory_db) -> None:
    ensure_builtin_studio_variables(memory_db)
    save_builtin_values(memory_db, {"address": "ул. Ленина, 1", "review_yandex": ""}, user_id=None)
    add_custom_variable(memory_db, "parking", "за домом", user_id=None)
    memory_db.commit()
    booking = _booking()
    client = SimpleNamespace(name="Катя")
    before = render_client_notify_template(
        "Привет, {{name}}! {{address}} / {{parking}} / {{review_yandex}} / {{unknown}}",
        booking=booking,  # type: ignore[arg-type]
        client=client,  # type: ignore[arg-type]
        tz_name="UTC",
        db=memory_db,
    )
    assert before == "Привет, Катя! ул. Ленина, 1 / за домом /  / {{unknown}}"
    after = render_client_notify_template(
        "Спасибо, {{name}}. Ждём по адресу {{address}}.",
        booking=booking,  # type: ignore[arg-type]
        client=client,  # type: ignore[arg-type]
        tz_name="UTC",
        db=memory_db,
        substitute_system=False,
    )
    assert after == "Спасибо, {{name}}. Ждём по адресу ул. Ленина, 1."


def test_public_video_by_token(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LB_MEDIA_ROOT", str(tmp_path))
    token = "a" * 32
    (tmp_path / "clip.mp4").write_bytes(b"fake-mp4")
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as db:
        db.add(
            StudioTemplateVariable(
                key="route_video",
                kind="video",
                is_builtin=True,
                public_token=token,
                file_path="clip.mp4",
                file_name="how.mp4",
                file_size=8,
            )
        )
        db.commit()

        def _db():
            yield db

        app.dependency_overrides[get_db] = _db
        try:
            client = TestClient(app)
            ok = client.get(f"/media/v/{token}")
            assert ok.status_code == 404
            missing = client.get("/media/v/" + ("b" * 32))
            assert missing.status_code == 404
            short = client.get("/media/v/nope")
            assert short.status_code == 404
        finally:
            app.dependency_overrides.clear()
