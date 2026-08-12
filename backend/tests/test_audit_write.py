"""Тест записи аудита с человекочитаемыми подписями."""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.audit import FieldChange, write_audit_rows
from app.db import models as _orm_models  # noqa: F401
from app.db.base import Base
from app.db.models import ClientAuditLog, User, UserRole
from app.setting_keys import SALON_CUT_PCT
from app.db.models import SettingAuditLog


@pytest.fixture()
def memory_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    with SessionLocal() as db:
        yield db


def test_write_audit_rows_labels_field_names(memory_db) -> None:
    db = memory_db
    admin = User(username="a1", password_hash="x", display_name="Admin", role=UserRole.ADMIN, is_active=True)
    db.add(admin)
    db.flush()
    write_audit_rows(
        db,
        log_model=ClientAuditLog,
        entity_field="client_id",
        entity_id=1,
        changed_by_user_id=admin.id,
        changes=[FieldChange("phone", "+7999", "+7888")],
    )
    row = db.scalar(select(ClientAuditLog).limit(1))
    assert row is not None
    assert row.field_name == "Телефон"


def test_write_audit_rows_setting_key_label(memory_db) -> None:
    db = memory_db
    admin = User(username="a1", password_hash="x", display_name="Admin", role=UserRole.ADMIN, is_active=True)
    db.add(admin)
    db.flush()
    write_audit_rows(
        db,
        log_model=SettingAuditLog,
        entity_field="setting_key",
        entity_id=SALON_CUT_PCT,
        changed_by_user_id=admin.id,
        changes=[FieldChange("value", "0.5", "0.6")],
    )
    row = db.scalar(select(SettingAuditLog).limit(1))
    assert row is not None
    assert row.field_name == "Доля салона"
