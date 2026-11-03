"""3.54: уровни амортизации в настройках."""

from __future__ import annotations

from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.amortization_levels import (
    amortization_amount_for,
    amortization_choices,
    delete_amortization_level,
    ensure_default_amortization_levels,
    list_amortization_levels,
    parse_amortization_input,
    resolve_amortization_amount,
    save_amortization_level,
)
from app.auth import AuthUser, get_current_user
from app.db import models as _orm_models  # noqa: F401
from app.db.base import Base
from app.db.models import (
    AmortizationLevelOption,
    Client,
    MixSource,
    Service,
    ServiceCategory,
    ServiceSubcategory,
    User,
    UserRole,
    UserRoleAssignment,
    VisitClientType,
    VisitMastersScope,
)
from app.db.session import get_db
from app.main import app
from app.visit_multi_service import (
    VisitHeaderInput,
    VisitServiceLineInput,
    compute_visit_service_line,
)


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


def _auth(user: User) -> AuthUser:
    return AuthUser(
        id=int(user.id),
        username=user.username,
        display_name=user.display_name or user.username,
        role=user.role,
        roles=(user.role,),
        master_level=user.master_level,
    )


def _seed_super(db) -> User:
    u = User(
        username="super_amort",
        password_hash="x",
        display_name="Super",
        role=UserRole.ADMIN_SUPER,
        is_active=True,
    )
    db.add(u)
    db.flush()
    db.add(UserRoleAssignment(user_id=u.id, role=UserRole.ADMIN_SUPER))
    db.commit()
    db.refresh(u)
    return u


def _client_for(db, user: User) -> TestClient:
    auth = _auth(user)

    def _override_user():
        return auth

    def _override_db():
        try:
            yield db
        finally:
            pass

    app.dependency_overrides[get_current_user] = _override_user
    app.dependency_overrides[get_db] = _override_db
    return TestClient(app)


def _clear_overrides() -> None:
    app.dependency_overrides.clear()


def test_ensure_defaults_seed_min_mid_max(memory_db) -> None:
    db = memory_db
    assert ensure_default_amortization_levels(db) is True
    db.commit()
    rows = list_amortization_levels(db)
    assert [r.code for r in rows] == ["MIN", "MID", "MAX"]
    assert amortization_amount_for(db, "MIN") == 100.0
    assert amortization_amount_for(db, "MID") == 200.0
    assert amortization_amount_for(db, "MAX") == 500.0
    assert ensure_default_amortization_levels(db) is False


def test_parse_and_choices_keep_deleted(memory_db) -> None:
    db = memory_db
    ensure_default_amortization_levels(db)
    db.commit()
    assert parse_amortization_input("NONE", ["MIN"]) is None
    assert parse_amortization_input("MIN", ["MIN", "MID"]) == "MIN"
    with pytest.raises(ValueError):
        parse_amortization_input("MAX", ["MIN"])
    row = db.scalar(select(AmortizationLevelOption).where(AmortizationLevelOption.code == "MAX"))
    assert row is not None
    delete_amortization_level(db, level_id=int(row.id), user_id=1)
    db.commit()
    choices = amortization_choices(db, "MAX", current_amount=777.0)
    codes = [c.code for c in choices]
    assert "MAX" in codes
    deleted = next(c for c in choices if c.code == "MAX")
    assert deleted.name == "(удалён)"
    assert deleted.amount == 777.0
    assert deleted.deleted is True


def test_cannot_delete_last_level(memory_db) -> None:
    db = memory_db
    ensure_default_amortization_levels(db)
    db.commit()
    for code in ("MID", "MAX"):
        row = db.scalar(select(AmortizationLevelOption).where(AmortizationLevelOption.code == code))
        delete_amortization_level(db, level_id=int(row.id), user_id=1)
    db.commit()
    last = list_amortization_levels(db)
    assert len(last) == 1
    with pytest.raises(ValueError, match="последний"):
        delete_amortization_level(db, level_id=int(last[0].id), user_id=1)


def test_settings_http_add_save_delete_and_forbidden(memory_db) -> None:
    db = memory_db
    super_u = _seed_super(db)
    ensure_default_amortization_levels(db)
    db.commit()
    client = _client_for(db, super_u)
    try:
        r = client.get("/admin/settings")
        assert r.status_code == 200
        assert "Амортизация" in r.text
        assert "Минимальная" in r.text

        r = client.post(
            "/admin/settings/amortization",
            data={"action": "save", "name": "Тест", "amount": "250", "description": "подсказка"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert "amortization_saved=1" in r.headers.get("location", "")
        db.expire_all()
        codes = [x.code for x in list_amortization_levels(db)]
        assert any(c.startswith("L") for c in codes)
        new_row = next(x for x in list_amortization_levels(db) if x.name == "Тест")
        assert float(new_row.amount) == 250.0

        r = client.post(
            "/admin/settings/amortization",
            data={
                "action": "save",
                "id": str(new_row.id),
                "name": "Тест2",
                "amount": "260",
                "description": "",
            },
            follow_redirects=False,
        )
        assert r.status_code == 303
        db.expire_all()
        assert db.get(AmortizationLevelOption, new_row.id).name == "Тест2"

        r = client.post(
            "/admin/settings/amortization",
            data={"action": "delete", "id": str(new_row.id)},
            follow_redirects=False,
        )
        assert r.status_code == 303
        db.expire_all()
        assert db.get(AmortizationLevelOption, new_row.id) is None
    finally:
        _clear_overrides()

    master = User(
        username="master_amort",
        password_hash="x",
        display_name="M",
        role=UserRole.MASTER,
        is_active=True,
    )
    db.add(master)
    db.flush()
    db.add(UserRoleAssignment(user_id=master.id, role=UserRole.MASTER))
    db.commit()
    client_m = _client_for(db, master)
    try:
        r = client_m.post(
            "/admin/settings/amortization",
            data={"action": "save", "name": "X", "amount": "1"},
            follow_redirects=False,
        )
        assert r.status_code == 403
    finally:
        _clear_overrides()


def _seed_visit_service(db) -> tuple[User, Client, Service]:
    master = User(username="ma", password_hash="x", display_name="MA", role=UserRole.MASTER, is_active=True)
    db.add(master)
    db.flush()
    db.add(UserRoleAssignment(user_id=master.id, role=UserRole.MASTER))
    client = Client(name="К", is_confirmed=True)
    db.add(client)
    cat = ServiceCategory(name="Кат", is_active=True)
    db.add(cat)
    db.flush()
    sub = ServiceSubcategory(category_id=cat.id, name="Под", is_active=True)
    db.add(sub)
    db.flush()
    svc = Service(subcategory_id=sub.id, name="Услуга", is_active=True)
    db.add(svc)
    ensure_default_amortization_levels(db)
    db.commit()
    db.refresh(master)
    db.refresh(client)
    db.refresh(svc)
    return master, client, svc


def test_new_level_amount_in_cost(memory_db) -> None:
    db = memory_db
    master, client, svc = _seed_visit_service(db)
    row = save_amortization_level(
        db, level_id=None, name="Особая", amount=333.0, description=None, user_id=int(master.id)
    )
    db.commit()
    header = VisitHeaderInput(
        client_mode="existing",
        existing_client_id=client.id,
        draft_name="",
        draft_phone="",
        draft_telegram="",
        draft_vk="",
        draft_instagram="",
        draft_other_contact="",
        client_type=VisitClientType.RETURNING,
        performed_date=datetime(2026, 5, 1).date(),
        duration_minutes=60,
        masters_scope=VisitMastersScope.VISIT,
        same_master_shares_all_services=False,
        visit_master_allocations=[(master.id, 100)],
        booking_id=None,
    )
    line = VisitServiceLineInput(
        service_id=svc.id,
        amount_from_client=5000.0,
        client_discount_percent=0,
        kanekalon_grams=0.0,
        kudri_grams=0.0,
        mix_source=MixSource.NO_MIX,
        mix_complexity=None,
        mix_bonus_master_id=None,
        amortization_level=row.code,
        kit_kind="OWN",
        stock_kit_lines=[],
        own_origin="STUDIO",
        own_correction=False,
        own_extra_blanks=False,
    )
    computed = compute_visit_service_line(
        db, line, header, default_mix_bonus_master_id=master.id, apply_kit_stock=False
    )
    assert computed.amortization_level == row.code
    assert computed.amortization_amount == 333.0


def test_edit_keeps_snapshot_when_level_unchanged(memory_db) -> None:
    db = memory_db
    master, client, svc = _seed_visit_service(db)
    mid = db.scalar(select(AmortizationLevelOption).where(AmortizationLevelOption.code == "MID"))
    assert mid is not None
    mid.amount = 999.0
    db.commit()

    header = VisitHeaderInput(
        client_mode="existing",
        existing_client_id=client.id,
        draft_name="",
        draft_phone="",
        draft_telegram="",
        draft_vk="",
        draft_instagram="",
        draft_other_contact="",
        client_type=VisitClientType.RETURNING,
        performed_date=datetime(2026, 5, 1).date(),
        duration_minutes=60,
        masters_scope=VisitMastersScope.VISIT,
        same_master_shares_all_services=False,
        visit_master_allocations=[(master.id, 100)],
        booking_id=None,
    )
    line = VisitServiceLineInput(
        service_id=svc.id,
        amount_from_client=5000.0,
        client_discount_percent=0,
        kanekalon_grams=0.0,
        kudri_grams=0.0,
        mix_source=MixSource.NO_MIX,
        mix_complexity=None,
        mix_bonus_master_id=None,
        amortization_level="MID",
        kit_kind="OWN",
        stock_kit_lines=[],
        own_origin="STUDIO",
        own_correction=False,
        own_extra_blanks=False,
        previous_amortization_level="MID",
        previous_amortization_amount=200.0,
    )
    computed = compute_visit_service_line(
        db, line, header, default_mix_bonus_master_id=master.id, apply_kit_stock=False
    )
    assert computed.amortization_amount == 200.0

    line2 = VisitServiceLineInput(
        service_id=svc.id,
        amount_from_client=5000.0,
        client_discount_percent=0,
        kanekalon_grams=0.0,
        kudri_grams=0.0,
        mix_source=MixSource.NO_MIX,
        mix_complexity=None,
        mix_bonus_master_id=None,
        amortization_level="MIN",
        kit_kind="OWN",
        stock_kit_lines=[],
        own_origin="STUDIO",
        own_correction=False,
        own_extra_blanks=False,
        previous_amortization_level="MID",
        previous_amortization_amount=200.0,
    )
    computed2 = compute_visit_service_line(
        db, line2, header, default_mix_bonus_master_id=master.id, apply_kit_stock=False
    )
    assert computed2.amortization_amount == 100.0
    assert resolve_amortization_amount(db, "MID") == 999.0


def test_deleted_level_still_prefills_and_keeps_amount(memory_db) -> None:
    db = memory_db
    master, client, svc = _seed_visit_service(db)
    custom = save_amortization_level(
        db, level_id=None, name="Временный", amount=444.0, description="x", user_id=int(master.id)
    )
    db.commit()
    code = custom.code
    delete_amortization_level(db, level_id=int(custom.id), user_id=int(master.id))
    db.commit()
    choices = amortization_choices(db, code, current_amount=444.0)
    assert any(c.code == code and c.deleted for c in choices)
    assert parse_amortization_input(code, [c.code for c in choices]) == code

    header = VisitHeaderInput(
        client_mode="existing",
        existing_client_id=client.id,
        draft_name="",
        draft_phone="",
        draft_telegram="",
        draft_vk="",
        draft_instagram="",
        draft_other_contact="",
        client_type=VisitClientType.RETURNING,
        performed_date=datetime(2026, 5, 1).date(),
        duration_minutes=60,
        masters_scope=VisitMastersScope.VISIT,
        same_master_shares_all_services=False,
        visit_master_allocations=[(master.id, 100)],
        booking_id=None,
    )
    line = VisitServiceLineInput(
        service_id=svc.id,
        amount_from_client=1000.0,
        client_discount_percent=0,
        kanekalon_grams=0.0,
        kudri_grams=0.0,
        mix_source=MixSource.NO_MIX,
        mix_complexity=None,
        mix_bonus_master_id=None,
        amortization_level=code,
        kit_kind="OWN",
        stock_kit_lines=[],
        own_origin="STUDIO",
        own_correction=False,
        own_extra_blanks=False,
        previous_amortization_level=code,
        previous_amortization_amount=444.0,
    )
    computed = compute_visit_service_line(
        db, line, header, default_mix_bonus_master_id=master.id, apply_kit_stock=False
    )
    assert computed.amortization_level == code
    assert computed.amortization_amount == 444.0
