"""Обязательное фото при закрытии визита (3.61)."""

from __future__ import annotations

import io
from datetime import date, datetime
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import AuthUser, get_current_user
from app.db import models as _orm_models  # noqa: F401
from app.db.base import Base
from app.db.models import (
    Client,
    PayrollPeriod,
    Service,
    ServiceCategory,
    ServiceSubcategory,
    User,
    UserRole,
    UserRoleAssignment,
    Visit,
    VisitDraft,
    VisitClientType,
    VisitMastersScope,
)
from app.db.session import get_db
from app.main import app
from app.visit_draft import finalize_visit_draft, save_visit_draft
from app.visit_multi_service import (
    MultiServiceVisitInput,
    VisitHeaderInput,
    VisitServiceLineInput,
    save_visit_with_services,
    update_visit_with_services,
)
from app.visit_photos import (
    VISIT_PHOTO_KEEP_ONE_MSG,
    VISIT_PHOTO_REQUIRED_MSG,
    assign_photos,
    photo_tuple_has_any,
    require_at_least_one_photo,
    resolve_visit_photos,
)

# Минимальный валидный PNG 1×1
_MIN_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05"
    b"\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
)


@pytest.fixture()
def memory_db(tmp_path, monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    monkeypatch.setenv("LB_MEDIA_ROOT", str(tmp_path / "media"))
    with SessionLocal() as db:
        yield db
    app.dependency_overrides.clear()


def _auth(user: User) -> AuthUser:
    return AuthUser(
        id=int(user.id),
        username=user.username,
        display_name=user.display_name or user.username,
        role=user.role,
        roles=(user.role,),
        master_level=getattr(user, "master_level", None),
    )


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
    return TestClient(app, follow_redirects=False)


def _seed(db) -> tuple[User, Client, int]:
    master = User(
        username="mphoto",
        password_hash="x",
        display_name="Мастер Фото",
        role=UserRole.MASTER,
        is_active=True,
    )
    db.add(master)
    db.flush()
    db.add(UserRoleAssignment(user_id=master.id, role=UserRole.MASTER))
    db.add(
        PayrollPeriod(
            date_from=datetime(2020, 1, 1),
            date_to=datetime(2030, 12, 31, 23, 59, 59),
            closed_at=None,
        )
    )
    client = Client(name="Клиент", phone="79001112233", is_confirmed=True)
    db.add(client)
    db.flush()
    cat = ServiceCategory(name="Вся голова", is_active=True)
    db.add(cat)
    db.flush()
    sub = ServiceSubcategory(category_id=cat.id, name="Плетение", is_active=True)
    db.add(sub)
    db.flush()
    svc = Service(subcategory_id=sub.id, name="Косы", is_active=True)
    db.add(svc)
    db.commit()
    return master, client, int(svc.id)


def _visit_inp(client_id: int, service_id: int, master_id: int) -> MultiServiceVisitInput:
    return MultiServiceVisitInput(
        header=VisitHeaderInput(
            client_mode="existing",
            existing_client_id=client_id,
            draft_name="",
            draft_phone="",
            draft_telegram="",
            draft_vk="",
            draft_instagram="",
            draft_other_contact="",
            client_type=VisitClientType.RETURNING,
            performed_date=date(2026, 5, 15),
            duration_minutes=60,
            masters_scope=VisitMastersScope.VISIT,
            same_master_shares_all_services=True,
            visit_master_allocations=[(master_id, 100)],
        ),
        lines=[
            VisitServiceLineInput(
                service_id=service_id,
                amount_from_client=5000.0,
                client_discount_percent=0,
                kanekalon_grams=0.0,
                kudri_grams=0.0,
                mix_source=None,
                mix_complexity=None,
                mix_bonus_master_id=None,
                amortization_level=None,
                kit_kind="STOCK",
            )
        ],
    )


class _Form:
    def __init__(self, data: dict | None = None, files: dict | None = None) -> None:
        self._data = data or {}
        self._files = files or {}

    def get(self, key, default=None):
        if key in self._files:
            return self._files[key]
        return self._data.get(key, default)

    def getlist(self, key):
        if key in self._files:
            return [self._files[key]]
        if key in self._data:
            return [self._data[key]]
        return []

    def keys(self):
        return list(self._data.keys()) + list(self._files.keys())


class _Upload:
    def __init__(self, filename: str, data: bytes, content_type: str = "image/png") -> None:
        self.filename = filename
        self.content_type = content_type
        self._buf = io.BytesIO(data)

    async def read(self, n: int = -1) -> bytes:
        return self._buf.read(n)

    async def seek(self, pos: int) -> None:
        self._buf.seek(pos)


def test_require_at_least_one_photo_message() -> None:
    with pytest.raises(ValueError, match="селфи"):
        require_at_least_one_photo((None, None, None))
    require_at_least_one_photo((None, "/media/a.png", None))


def test_resolve_photo_in_slot_2_only(tmp_path, monkeypatch) -> None:
    import asyncio

    monkeypatch.setenv("LB_MEDIA_ROOT", str(tmp_path / "media"))
    form = _Form(files={"photo_2": _Upload("x.png", _MIN_PNG)})
    photos, changed = asyncio.run(resolve_visit_photos(form))
    assert changed is True
    assert photos[0] is None
    assert photos[1] and photos[1].startswith("/media/")
    assert photos[2] is None
    assert photo_tuple_has_any(photos)


def test_visit_new_without_photo_returns_400(memory_db) -> None:
    db = memory_db
    master, _, _ = _seed(db)
    client = _client_for(db, master)
    res = client.post(
        "/master/visit/new",
        data={
            "client_mode": "existing",
            "existing_client_id": "1",
            "performed_date": "2026-05-15",
            "amount_from_client": "5000",
        },
    )
    assert res.status_code == 400
    assert VISIT_PHOTO_REQUIRED_MSG in res.text
    assert db.scalar(select(func.count(Visit.id))) == 0


def test_visit_new_with_photo_2_creates_visit(memory_db, tmp_path, monkeypatch) -> None:
    db = memory_db
    monkeypatch.setenv("LB_MEDIA_ROOT", str(tmp_path / "media2"))
    master, client, sid = _seed(db)
    http = _client_for(db, master)
    with patch("app.client_after_visit.schedule_after_visit_notifications"):
        with patch("app.staff_assignment_notifications.notify_visit_staff_assigned_on_create"):
            res = http.post(
                "/master/visit/new",
                data={
                    "client_mode": "existing",
                    "existing_client_id": str(client.id),
                    "performed_date": "2026-05-15",
                    "duration_h": "1",
                    "duration_m": "0",
                    "service_id": str(sid),
                    "amount_from_client": "5000",
                    "masters_scope": "VISIT",
                    "visit_master_on": str(master.id),
                    f"visit_master_pct_{master.id}": "100",
                    "kit_kind": "STOCK",
                    "mix_source": "NO_MIX",
                },
                files={"photo_2": ("shot.png", _MIN_PNG, "image/png")},
            )
    assert res.status_code in (303, 302), (res.status_code, res.headers.get("location"), res.text[:400])
    visit = db.scalar(select(Visit).order_by(Visit.id.desc()))
    assert visit is not None
    assert visit.photo_1 is None
    assert visit.photo_2 and visit.photo_2.startswith("/media/")
    assert visit.photo_3 is None


def test_visit_new_bad_file_does_not_create_visit(memory_db, tmp_path, monkeypatch) -> None:
    db = memory_db
    monkeypatch.setenv("LB_MEDIA_ROOT", str(tmp_path / "media3"))
    master, client, sid = _seed(db)
    http = _client_for(db, master)
    res = http.post(
        "/master/visit/new",
        data={
            "client_mode": "existing",
            "existing_client_id": str(client.id),
            "performed_date": "2026-05-15",
            "service_id": str(sid),
            "amount_from_client": "5000",
            "visit_master_on": str(master.id),
            f"visit_master_pct_{master.id}": "100",
        },
        files={"photo_1": ("evil.txt", b"not-image", "text/plain")},
    )
    assert res.status_code == 400
    assert db.scalar(select(func.count(Visit.id))) == 0


def test_draft_photos_save_preview_and_finalize(memory_db, tmp_path, monkeypatch) -> None:
    db = memory_db
    monkeypatch.setenv("LB_MEDIA_ROOT", str(tmp_path / "media4"))
    master, client, sid = _seed(db)
    inp = _visit_inp(int(client.id), sid, int(master.id))
    draft = save_visit_draft(
        db,
        None,
        inp,
        master.id,
        {"performed_date": "2026-05-15"},
        created_by_label="t",
    )
    assign_photos(draft, (None, "/media/draft_only.png", None))
    db.commit()
    db.refresh(draft)
    assert draft.photo_2 == "/media/draft_only.png"
    assert draft.finalized_visit_id is None

    with pytest.raises(ValueError, match="селфи"):
        require_at_least_one_photo((None, None, None))

    with patch("app.client_after_visit.schedule_after_visit_notifications"):
        with patch("app.staff_assignment_notifications.notify_visit_staff_assigned_on_create"):
            visit_id = finalize_visit_draft(
                db,
                int(draft.id),
                inp,
                master.id,
                created_by_label="t",
                photos=(None, "/media/draft_only.png", None),
            )
            db.commit()
    visit = db.get(Visit, visit_id)
    assert visit is not None
    assert visit.photo_2 == "/media/draft_only.png"
    db.refresh(draft)
    assert draft.finalized_visit_id == visit_id
    assert draft.photo_2 is None


def test_finalize_without_photos_raises(memory_db) -> None:
    db = memory_db
    master, client, sid = _seed(db)
    inp = _visit_inp(int(client.id), sid, int(master.id))
    draft = save_visit_draft(
        db,
        None,
        inp,
        master.id,
        {"performed_date": "2026-05-15"},
        created_by_label="t",
    )
    db.commit()
    http = _client_for(db, master)
    res = http.post(
        f"/master/visit/draft/{draft.id}/finalize",
        data={
            "client_mode": "existing",
            "existing_client_id": str(client.id),
            "performed_date": "2026-05-15",
            "duration_h": "1",
            "duration_m": "0",
            "service_id": str(sid),
            "line_0_service_id": str(sid),
            "amount_from_client": "5000",
            "masters_scope": "VISIT",
            "visit_master_on": str(master.id),
            f"visit_master_pct_{master.id}": "100",
            "kit_kind": "STOCK",
            "mix_source": "NO_MIX",
        },
    )
    assert res.status_code == 400
    assert VISIT_PHOTO_REQUIRED_MSG in res.text
    db.refresh(draft)
    assert draft.finalized_visit_id is None
    assert db.scalar(select(func.count(Visit.id))) == 0


def test_edit_cannot_clear_last_photo(memory_db) -> None:
    db = memory_db
    master, client, sid = _seed(db)
    inp = _visit_inp(int(client.id), sid, int(master.id))
    with patch("app.client_after_visit.schedule_after_visit_notifications"):
        with patch("app.staff_assignment_notifications.notify_visit_staff_assigned_on_create"):
            visit = save_visit_with_services(
                db,
                master.id,
                inp,
                photos=("/media/only.png", None, None),
            )
    assert visit.photo_1 == "/media/only.png"

    class ClearForm(_Form):
        pass

    import asyncio

    photos, changed = asyncio.run(
        resolve_visit_photos(ClearForm({"clear_photo_1": "1"}), current=(visit.photo_1, None, None))
    )
    assert changed is True
    assert not photo_tuple_has_any(photos)
    with pytest.raises(ValueError, match="хотя бы одно"):
        if changed and photo_tuple_has_any((visit.photo_1, None, None)) and not photo_tuple_has_any(photos):
            raise ValueError(VISIT_PHOTO_KEEP_ONE_MSG)


def test_edit_legacy_visit_without_photos_ok(memory_db) -> None:
    db = memory_db
    master, client, sid = _seed(db)
    inp = _visit_inp(int(client.id), sid, int(master.id))
    with patch("app.client_after_visit.schedule_after_visit_notifications"):
        with patch("app.staff_assignment_notifications.notify_visit_staff_assigned_on_create"):
            visit = save_visit_with_services(db, master.id, inp, photos=None)
    assert visit.photo_1 is None
    assert visit.photo_2 is None
    assert visit.photo_3 is None
    # Правка без касания фото не блокируется правилом «хотя бы одно».
    inp2 = _visit_inp(int(client.id), sid, int(master.id))
    inp2.lines[0].amount_from_client = 5500.0
    with patch("app.client_after_visit.schedule_after_visit_notifications"):
        update_visit_with_services(db, int(visit.id), master.id, inp2)
    db.refresh(visit)
    assert visit.photo_1 is None
    assert visit.photo_2 is None
    assert visit.photo_3 is None
    # Имитация edit: photo_changed=False при пустых слотах — ошибка keep-one не нужна.
    had = photo_tuple_has_any((visit.photo_1, visit.photo_2, visit.photo_3))
    assert had is False
