"""3.19: отметки клиента, отписка от акций, фильтры и импорт-флаги."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import AuthUser, get_current_user
from app.client_marks import apply_client_import_marks, client_can_receive_promo
from app.db import models as _orm_models  # noqa: F401
from app.db.base import Base
from app.db.models import Client, User, UserRole, UserRoleAssignment
from app.db.session import get_db
from app.main import app
from app.security import hash_password
from app.settings import get_settings


def _engine_db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def test_client_can_receive_promo() -> None:
    promo = Client(name="А", is_confirmed=True, no_promo=False)
    opted_out = Client(name="Б", is_confirmed=True, no_promo=True)
    assert client_can_receive_promo(promo) is True
    assert client_can_receive_promo(opted_out) is False


def test_import_flags_optional_and_clear_no_promo() -> None:
    c = Client(name="Импорт", is_confirmed=True, is_student=True)
    apply_client_import_marks(c, {"is_model": True, "no_promo": "1", "interested_in_training": False})
    assert c.is_model is True
    assert c.is_student is True
    assert c.interested_in_training is False
    assert c.no_promo is True
    assert c.no_promo_at is not None
    assert c.no_promo_source == "manual"
    apply_client_import_marks(c, {"no_promo": False})
    assert c.no_promo is False
    assert c.no_promo_at is None
    assert c.no_promo_source is None
    assert c.is_model is True


def test_form_save_and_clear_marks_and_list_filter() -> None:
    SessionLocal = _engine_db()
    get_settings.cache_clear()
    with SessionLocal() as db:
        admin = User(
            username="adm",
            password_hash=hash_password("secret1"),
            display_name="Админ",
            role=UserRole.ADMIN_SUPER,
            is_active=True,
        )
        db.add(admin)
        db.flush()
        db.add(UserRoleAssignment(user_id=admin.id, role=UserRole.ADMIN_SUPER))
        student = Client(name="Ученица Катя", phone="+79990001111", is_confirmed=True, is_student=True)
        plain = Client(name="Обычная", phone="+79990002222", is_confirmed=True)
        db.add_all([student, plain])
        db.commit()
        db.refresh(admin)
        db.refresh(student)

        auth = AuthUser(
            id=int(admin.id),
            username=admin.username,
            display_name=admin.display_name,
            role=UserRole.ADMIN_SUPER,
            roles=(UserRole.ADMIN_SUPER,),
            master_level=None,
        )

        def _user():
            return auth

        def _db():
            yield db

        app.dependency_overrides[get_current_user] = _user
        app.dependency_overrides[get_db] = _db
        try:
            client = TestClient(app)
            r = client.post(
                f"/clients/{student.id}/edit",
                data={
                    "name": "Ученица Катя",
                    "phone": "+79990001111",
                    "is_confirmed": "1",
                    "interested_in_training": "1",
                    "is_student": "1",
                    "is_model": "1",
                    "no_promo": "1",
                },
                follow_redirects=False,
            )
            assert r.status_code == 303
            db.refresh(student)
            assert student.interested_in_training is True
            assert student.is_student is True
            assert student.is_model is True
            assert student.no_promo is True
            assert student.no_promo_source == "manual"
            assert student.no_promo_at is not None

            listed = client.get("/clients?is_student=1&no_promo=1")
            assert listed.status_code == 200
            assert "Ученица Катя" in listed.text
            assert "Обычная" not in listed.text

            cleared = client.post(
                f"/clients/{student.id}/edit",
                data={
                    "name": "Ученица Катя",
                    "phone": "+79990001111",
                    "is_confirmed": "1",
                },
                follow_redirects=False,
            )
            assert cleared.status_code == 303
            db.refresh(student)
            assert student.no_promo is False
            assert student.no_promo_at is None
            assert student.no_promo_source is None
            assert student.is_student is False
        finally:
            app.dependency_overrides.clear()
            get_settings.cache_clear()
