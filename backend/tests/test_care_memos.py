"""3.25: памятки-картинки после визита."""

from __future__ import annotations

import json
import urllib.request
import zipfile
from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.care_memo_send import deliver_care_memos
from app.care_memos import (
    CareMemoInUse,
    MEMO_AFTER,
    MEMO_BEFORE,
    MODE_CUSTOM,
    MODE_INHERIT,
    MODE_NONE,
    SLOT_INHERIT,
    SLOT_NONE,
    apply_category_memos,
    apply_entity_memos,
    care_memo_ids_for_booking,
    care_memo_ids_for_visit,
    create_care_memo,
    delete_care_memo,
    legacy_after_slot_from_old,
    migrate_care_memo_before_after,
    parse_memo_name,
    rename_care_memo,
    replace_care_memo_file,
    resolve_care_memo,
    store_care_memo_bytes,
)
from app.client_after_visit import enqueue_due_after_booking_deadlines, schedule_after_visit_notifications
from app.client_notifications import KIND_AFTER
from app.client_notify_history import booking_notify_timeline
from app.db import models as _orm_models  # noqa: F401
from app.db.base import Base
from app.db.models import (
    Booking,
    BookingKind,
    BookingPlannedService,
    BookingStatus,
    CareMemo,
    Client,
    ClientNotificationRule,
    ConsultationKind,
    NotificationChannel,
    NotificationOutbox,
    NotificationOutboxStatus,
    Service,
    ServiceCategory,
    ServiceSubcategory,
    User,
    UserRole,
    Visit,
    VisitClientType,
    VisitMastersScope,
    VisitPriceType,
    VisitService,
)
from app.media_store import build_media_manifest, media_backup_arcname
from app.notifications import process_outbox
from app.routes.techspec_media import _build_backup_zip


def memory_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


@pytest.fixture()
def db():
    session = memory_db()
    try:
        yield session
    finally:
        session.close()


class _Upload:
    def __init__(self, name: str, data: bytes) -> None:
        self.filename = name
        self._data = data

    async def read(self) -> bytes:
        return self._data


def _memo(db, name: str = "Уход", filename: str = "a.png") -> CareMemo:
    rel, file_name, mime, size = store_care_memo_bytes(b"img", filename)
    row = CareMemo(
        name=name,
        file_path=rel,
        file_name=file_name,
        mime=mime,
        size=size,
        vk_attachment="photo1_2",
        max_image_token="old",
        telegram_file_id="old-file",
        created_at=datetime(2026, 10, 12, 8, 0),
    )
    db.add(row)
    db.flush()
    return row


def _after_slot(mode: str, memo) -> tuple[str, int | None]:
    if mode == MODE_INHERIT:
        return MODE_INHERIT, None
    if memo is not None:
        return MODE_CUSTOM, int(memo.id)
    return MODE_NONE, None


def _catalog(db, *, cat_memo=None, sub_mode="inherit", sub_memo=None, svc_mode="inherit", svc_memo=None):
    """Старый смысл одной памятки → слот «после» (как миграция)."""
    cat = ServiceCategory(
        name="Косы",
        consultation_kind=ConsultationKind.BRAIDING,
        care_memo_before_mode=MODE_INHERIT,
        care_memo_after_mode=MODE_CUSTOM if cat_memo else MODE_NONE,
        care_memo_after_id=cat_memo.id if cat_memo else None,
    )
    db.add(cat)
    db.flush()
    sub_mode_v, sub_id = _after_slot(sub_mode, sub_memo)
    sub = ServiceSubcategory(
        category_id=cat.id,
        name="Афро",
        care_memo_before_mode=MODE_INHERIT,
        care_memo_after_mode=sub_mode_v,
        care_memo_after_id=sub_id,
    )
    db.add(sub)
    db.flush()
    svc_mode_v, svc_id = _after_slot(svc_mode, svc_memo)
    svc = Service(
        subcategory_id=sub.id,
        name="Сенегалки",
        estimated_duration_minutes=60,
        care_memo_before_mode=MODE_INHERIT,
        care_memo_after_mode=svc_mode_v,
        care_memo_after_id=svc_id,
    )
    db.add(svc)
    db.flush()
    return cat, sub, svc


def _people(db):
    user = User(
        username="adm",
        password_hash="x",
        display_name="Админ",
        role=UserRole.ADMIN,
        is_active=True,
    )
    client = Client(name="Катя", is_confirmed=True, vk_user_id=1001, telegram_chat_id=2002, max_user_id=3003)
    db.add_all([user, client])
    db.flush()
    return user, client


def _visit_with(db, client, services: list[Service]) -> Visit:
    visit = Visit(
        created_at=datetime(2026, 10, 12, 8, 0),
        performed_date=datetime(2026, 10, 12),
        client_id=client.id,
        client_type=VisitClientType.RETURNING,
        price_type=VisitPriceType.CLIENT,
        masters_scope=VisitMastersScope.VISIT,
        is_cancelled=False,
    )
    db.add(visit)
    db.flush()
    for i, svc in enumerate(services):
        sub = db.get(ServiceSubcategory, svc.subcategory_id)
        cat = db.get(ServiceCategory, sub.category_id)
        db.add(
            VisitService(
                visit_id=visit.id,
                service_id=svc.id,
                category_name=cat.name,
                subcategory_name=sub.name,
                service_name=svc.name,
                sort_order=i,
            )
        )
    db.flush()
    return visit


def test_name_validation_and_crud(db, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LB_MEDIA_ROOT", str(tmp_path))
    with pytest.raises(ValueError):
        parse_memo_name("   ")
    with pytest.raises(ValueError):
        parse_memo_name("x" * 81)
    row = _memo(db, "Уход")
    with pytest.raises(ValueError):
        rename_care_memo(db, row.id, "  ")

    import asyncio

    second = asyncio.run(create_care_memo(db, name="Мытьё", upload=_Upload("b.webp", b"webp")))
    assert second.file_path.startswith("care_memos/")
    with pytest.raises(ValueError):
        asyncio.run(create_care_memo(db, name="уход", upload=_Upload("c.jpg", b"jpg")))
    with pytest.raises(ValueError):
        asyncio.run(create_care_memo(db, name="PDF", upload=_Upload("x.pdf", b"%PDF")))

    replaced = asyncio.run(replace_care_memo_file(db, row.id, _Upload("n.png", b"new")))
    assert replaced.vk_attachment is None
    assert replaced.max_image_token is None
    assert replaced.telegram_file_id is None
    assert replaced.file_path != row.file_path or replaced.size == 3
    renamed = rename_care_memo(db, row.id, "После мытья")
    assert renamed.name == "После мытья"
    delete_care_memo(db, second.id, confirmed=False)
    assert db.get(CareMemo, second.id) is None


def test_delete_warns_and_resets_links(db, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LB_MEDIA_ROOT", str(tmp_path))
    memo = _memo(db, "Сушка")
    other = _memo(db, "Другая", "b.jpg")
    cat, sub, svc = _catalog(db, cat_memo=memo, sub_mode="custom", sub_memo=memo, svc_mode="custom", svc_memo=other)
    apply_category_memos(db, cat, SLOT_INHERIT, str(memo.id))
    with pytest.raises(CareMemoInUse) as caught:
        delete_care_memo(db, memo.id, confirmed=False)
    links_preview = caught.value.links
    kinds = {item["kind"] for item in links_preview}
    assert "категория" in kinds
    assert "подкатегория" in kinds
    assert any(item.get("slot") == "после" for item in links_preview)
    assert any(item["href"].endswith("/edit") for item in links_preview)
    assert db.get(CareMemo, memo.id) is not None

    delete_care_memo(db, memo.id, confirmed=True)
    db.refresh(cat)
    db.refresh(sub)
    assert cat.care_memo_after_id is None
    assert sub.care_memo_after_id is None
    assert sub.care_memo_after_mode == MODE_INHERIT
    assert db.get(CareMemo, other.id) is not None
    assert svc.care_memo_after_id == other.id


def test_inherit_category_subcategory_service(db, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LB_MEDIA_ROOT", str(tmp_path))
    cat_memo = _memo(db, "Категория")
    sub_memo = _memo(db, "Подкатегория", "b.png")
    svc_memo = _memo(db, "Услуга", "c.webp")
    _cat, sub, svc = _catalog(db, cat_memo=cat_memo)
    assert resolve_care_memo(db, svc, MEMO_AFTER).id == cat_memo.id

    apply_entity_memos(db, sub, SLOT_INHERIT, str(sub_memo.id))
    assert resolve_care_memo(db, svc, MEMO_AFTER).id == sub_memo.id

    apply_entity_memos(db, svc, SLOT_INHERIT, SLOT_NONE)
    assert resolve_care_memo(db, svc, MEMO_AFTER) is None

    apply_entity_memos(db, svc, SLOT_INHERIT, str(svc_memo.id))
    assert resolve_care_memo(db, svc, MEMO_AFTER).id == svc_memo.id

    apply_entity_memos(db, svc, SLOT_INHERIT, SLOT_INHERIT)
    assert svc.care_memo_after_id is None
    assert resolve_care_memo(db, svc, MEMO_AFTER).id == sub_memo.id


def test_before_after_independent_and_none_blocks_parent(db, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LB_MEDIA_ROOT", str(tmp_path))
    before_m = _memo(db, "До")
    after_m = _memo(db, "После", "b.png")
    cat, sub, svc = _catalog(db)
    apply_category_memos(db, cat, str(before_m.id), str(after_m.id))
    assert resolve_care_memo(db, svc, MEMO_BEFORE).id == before_m.id
    assert resolve_care_memo(db, svc, MEMO_AFTER).id == after_m.id
    assert care_memo_ids_for_booking(db, _booking_with(db, svc), kind=MEMO_BEFORE) == [before_m.id]

    apply_entity_memos(db, sub, SLOT_INHERIT, SLOT_NONE)
    assert resolve_care_memo(db, svc, MEMO_AFTER) is None
    assert resolve_care_memo(db, svc, MEMO_BEFORE).id == before_m.id

    apply_entity_memos(db, sub, SLOT_NONE, SLOT_NONE)
    assert resolve_care_memo(db, svc, MEMO_BEFORE) is None


def test_legacy_after_slot_and_sql_migration(db, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LB_MEDIA_ROOT", str(tmp_path))
    assert legacy_after_slot_from_old(memo_id=5, is_category=True) == (MODE_CUSTOM, 5)
    assert legacy_after_slot_from_old(memo_id=None, is_category=True) == (MODE_NONE, None)
    assert legacy_after_slot_from_old(memo_id=None, mode="inherit") == (MODE_INHERIT, None)
    assert legacy_after_slot_from_old(memo_id=3, mode="custom") == (MODE_CUSTOM, 3)
    assert legacy_after_slot_from_old(memo_id=None, mode="custom") == (MODE_NONE, None)

    from sqlalchemy import create_engine, text

    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE care_memos (id INTEGER PRIMARY KEY)"))
        conn.execute(
            text(
                """
                CREATE TABLE service_categories (
                    id INTEGER PRIMARY KEY,
                    care_memo_id INTEGER,
                    care_memo_before_mode VARCHAR(16) DEFAULT 'inherit',
                    care_memo_before_id INTEGER,
                    care_memo_after_mode VARCHAR(16) DEFAULT 'inherit',
                    care_memo_after_id INTEGER
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE service_subcategories (
                    id INTEGER PRIMARY KEY,
                    care_memo_mode VARCHAR(16),
                    care_memo_id INTEGER,
                    care_memo_before_mode VARCHAR(16) DEFAULT 'inherit',
                    care_memo_before_id INTEGER,
                    care_memo_after_mode VARCHAR(16) DEFAULT 'inherit',
                    care_memo_after_id INTEGER
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE services (
                    id INTEGER PRIMARY KEY,
                    care_memo_mode VARCHAR(16),
                    care_memo_id INTEGER,
                    care_memo_before_mode VARCHAR(16) DEFAULT 'inherit',
                    care_memo_before_id INTEGER,
                    care_memo_after_mode VARCHAR(16) DEFAULT 'inherit',
                    care_memo_after_id INTEGER
                )
                """
            )
        )
        conn.execute(text("INSERT INTO service_categories (id, care_memo_id) VALUES (1, 10)"))
        conn.execute(
            text(
                "INSERT INTO service_subcategories (id, care_memo_mode, care_memo_id) VALUES "
                "(2, 'custom', 10), (3, 'inherit', NULL), (4, 'custom', NULL)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO services (id, care_memo_mode, care_memo_id) VALUES (5, 'custom', 11)"
            )
        )
        migrate_care_memo_before_after(conn)
        cat = conn.execute(
            text(
                "SELECT care_memo_before_mode, care_memo_after_mode, care_memo_after_id "
                "FROM service_categories WHERE id=1"
            )
        ).one()
        assert cat == (MODE_INHERIT, MODE_CUSTOM, 10)
        sub_custom = conn.execute(
            text("SELECT care_memo_after_mode, care_memo_after_id FROM service_subcategories WHERE id=2")
        ).one()
        assert sub_custom == (MODE_CUSTOM, 10)
        sub_inh = conn.execute(
            text("SELECT care_memo_after_mode, care_memo_after_id FROM service_subcategories WHERE id=3")
        ).one()
        assert sub_inh == (MODE_INHERIT, None)
        sub_none = conn.execute(
            text("SELECT care_memo_after_mode, care_memo_after_id FROM service_subcategories WHERE id=4")
        ).one()
        assert sub_none == (MODE_NONE, None)
        svc_row = conn.execute(
            text("SELECT care_memo_before_mode, care_memo_after_mode, care_memo_after_id FROM services WHERE id=5")
        ).one()
        assert svc_row == (MODE_INHERIT, MODE_CUSTOM, 11)


def _booking_with(db, svc: Service) -> Booking:
    user, client = _people(db)
    booking = Booking(
        created_by_user_id=user.id,
        client_id=client.id,
        planned_date=datetime(2026, 10, 12, 5, 0),
        kind=BookingKind.VISIT,
        status=BookingStatus.ACTIVE,
        masters_scope=VisitMastersScope.VISIT,
        same_master_shares_all_services=False,
    )
    db.add(booking)
    db.flush()
    db.add(BookingPlannedService(booking_id=booking.id, service_id=svc.id, sort_order=0))
    db.flush()
    return booking


def test_unique_memos_on_visit_and_checkbox(db, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LB_MEDIA_ROOT", str(tmp_path))
    shared = _memo(db, "Общая")
    own = _memo(db, "Своя", "b.jpg")
    cat, sub, first = _catalog(db, cat_memo=shared)
    second = Service(
        subcategory_id=sub.id,
        name="Зизи",
        estimated_duration_minutes=30,
        care_memo_before_mode=MODE_INHERIT,
        care_memo_after_mode=MODE_CUSTOM,
        care_memo_after_id=own.id,
    )
    third = Service(
        subcategory_id=sub.id,
        name="Повтор",
        estimated_duration_minutes=30,
        care_memo_before_mode=MODE_INHERIT,
        care_memo_after_mode=MODE_INHERIT,
    )
    db.add_all([second, third])
    db.flush()
    user, client = _people(db)
    visit = _visit_with(db, client, [first, second, third])
    assert care_memo_ids_for_visit(db, visit) == [shared.id, own.id]

    booking = Booking(
        created_by_user_id=user.id,
        client_id=client.id,
        planned_date=datetime(2026, 10, 12, 5, 0),
        kind=BookingKind.VISIT,
        status=BookingStatus.ACTIVE,
        masters_scope=VisitMastersScope.VISIT,
        same_master_shares_all_services=False,
    )
    db.add(booking)
    db.flush()
    db.add(BookingPlannedService(booking_id=booking.id, service_id=first.id, sort_order=0))
    db.add(BookingPlannedService(booking_id=booking.id, service_id=second.id, sort_order=1))
    db.flush()
    assert care_memo_ids_for_booking(db, booking) == [shared.id, own.id]

    rule = ClientNotificationRule(
        kind=KIND_AFTER,
        hours=0,
        latest_time=None,
        template="Спасибо, {{name}}",
        position=0,
        is_enabled=True,
        send_care_memo=True,
        created_at=datetime(2026, 1, 1),
    )
    db.add(rule)
    db.flush()
    rows = schedule_after_visit_notifications(db, visit)
    assert rows
    payload = json.loads(rows[0].payload_json)
    assert payload["care_memo_ids"] == [shared.id, own.id]

    rule.send_care_memo = False
    visit2 = _visit_with(db, client, [first])
    rows_off = schedule_after_visit_notifications(db, visit2)
    assert rows_off
    assert "care_memo_ids" not in json.loads(rows_off[0].payload_json)


def _settings():
    return SimpleNamespace(
        telegram_bot_token="tg",
        telegram_api_base="https://tg.example",
        vk_group_token="vk",
        vk_api_version="5.199",
        max_bot_token="mx",
        max_api_base="https://max.example",
    )


class _Resp:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _mock_http(monkeypatch):
    calls: list[str] = []

    def urlopen(req, timeout=0):
        url = req.full_url
        calls.append(url.split("?")[0] + ("?" + url.split("?", 1)[1].split("&access_token")[0] if "?" in url else ""))
        if "photos.getMessagesUploadServer" in url:
            body = {"response": {"upload_url": "https://upload.example/vk"}}
        elif url.startswith("https://upload.example/vk"):
            body = {"server": 1, "photo": "p", "hash": "h"}
        elif "photos.saveMessagesPhoto" in url:
            body = {"response": [{"owner_id": -5, "id": 9}]}
        elif "messages.send" in url:
            body = {"response": 1}
        elif "/uploads" in url:
            body = {"url": "https://upload.example/max"}
        elif url.startswith("https://upload.example/max"):
            body = {"token": "tok-1"}
        elif "/messages" in url and "max.example" in url:
            body = {}
        elif "sendPhoto" in url:
            body = {"ok": True, "result": {"photo": [{"file_id": "fid-1"}]}}
        elif "sendMediaGroup" in url:
            body = {"ok": True, "result": []}
        else:
            body = {"ok": True}
        return _Resp(json.dumps(body).encode())

    def max_request(method, url, *, content=None, headers=None, timeout=8):
        req = urllib.request.Request(url, data=content, method=method, headers=headers or {})
        with urlopen(req, timeout=timeout) as resp:
            return 200, resp.read()

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    monkeypatch.setattr("app.max_http.max_request", max_request)
    monkeypatch.setattr("app.care_memo_send.get_settings", _settings)
    monkeypatch.setattr("app.notifications.get_settings", _settings)
    return calls


def _outbox(db, client, channel, memo_ids, *, text="Спасибо"):
    row = NotificationOutbox(
        client_id=client.id,
        target_kind="client",
        channel=channel,
        event_type="client_after_booking",
        payload_json=json.dumps(
            {"text": text, "target_id": 42, "target_kind": "client", "care_memo_ids": memo_ids},
            ensure_ascii=False,
        ),
        body=text,
        status=NotificationOutboxStatus.PENDING,
        dedupe_key=f"t-{channel.value}-{memo_ids}",
        attempt_count=0,
        created_at=datetime(2026, 10, 12, 8, 0),
        scheduled_at=datetime(2026, 10, 12, 8, 0),
    )
    db.add(row)
    db.flush()
    return row


def test_send_each_channel(db, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LB_MEDIA_ROOT", str(tmp_path))
    calls = _mock_http(monkeypatch)
    sent_text: list[str] = []
    monkeypatch.setattr("app.notifications.send_vk", lambda *a, **k: sent_text.append("vk"))
    monkeypatch.setattr("app.notifications.send_max", lambda *a, **k: sent_text.append("max"))
    monkeypatch.setattr("app.notifications.send_telegram", lambda *a, **k: sent_text.append("tg"))
    _, client = _people(db)
    memo = _memo(db, "Картинка")
    memo.vk_attachment = None
    memo.max_image_token = None
    memo.telegram_file_id = None
    db.flush()
    for channel in (NotificationChannel.VK, NotificationChannel.MAX, NotificationChannel.TELEGRAM):
        row = _outbox(db, client, channel, [memo.id])
        stats = process_outbox(db, only_ids=[row.id])
        assert stats["sent"] == 1
        db.refresh(row)
        assert row.status == NotificationOutboxStatus.SENT
        payload = json.loads(row.payload_json)
        assert payload["care_memos_attached"] == ["Картинка"]
    assert sent_text == ["vk", "max", "tg"]
    joined = " ".join(calls)
    assert "photos.getMessagesUploadServer" in joined
    assert "photos.saveMessagesPhoto" in joined
    assert "messages.send" in joined
    assert "https://max.example/uploads" in joined
    assert "sendPhoto" in joined
    db.refresh(memo)
    assert memo.vk_attachment == "photo-5_9"
    assert memo.max_image_token == "tok-1"
    assert memo.telegram_file_id == "fid-1"

    memo.telegram_file_id = "cached"
    second = _memo(db, "Вторая", "b.jpg")
    second.telegram_file_id = "cached-2"
    second.vk_attachment = None
    second.max_image_token = None
    db.flush()
    calls.clear()
    row = _outbox(db, client, NotificationChannel.TELEGRAM, [memo.id, second.id])
    deliver_care_memos(db, row)
    assert any("sendMediaGroup" in url for url in calls)


def test_image_error_does_not_block_text(db, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LB_MEDIA_ROOT", str(tmp_path))
    monkeypatch.setattr("app.notifications.send_vk", lambda *a, **k: None)
    monkeypatch.setattr("app.care_memo_send.get_settings", _settings)

    def urlopen(req, timeout=0):
        raise OSError("upload down")

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    _, client = _people(db)
    memo = _memo(db, "Сбой")
    memo.vk_attachment = None
    db.flush()
    row = _outbox(db, client, NotificationChannel.VK, [memo.id], text="Текст ушёл")
    stats = process_outbox(db, only_ids=[row.id])
    assert stats["sent"] == 1
    db.refresh(row)
    assert row.status == NotificationOutboxStatus.SENT
    assert row.body == "Текст ушёл"
    errors = list(
        db.scalars(select(NotificationOutbox).where(NotificationOutbox.event_type == "client_care_memo_error")).all()
    )
    assert len(errors) == 1
    assert errors[0].body == "Памятка Сбой — ошибка"
    assert errors[0].status == NotificationOutboxStatus.FAILED


def test_history_lists_attached_memos(db) -> None:
    user, client = _people(db)
    booking = Booking(
        created_by_user_id=user.id,
        client_id=client.id,
        planned_date=datetime(2026, 10, 12, 5, 0),
        kind=BookingKind.VISIT,
        status=BookingStatus.ACTIVE,
        masters_scope=VisitMastersScope.VISIT,
        same_master_shares_all_services=False,
    )
    db.add(booking)
    db.flush()
    db.add(
        NotificationOutbox(
            client_id=client.id,
            booking_id=booking.id,
            target_kind="client",
            channel=NotificationChannel.VK,
            event_type="client_after_booking",
            payload_json=json.dumps({"text": "Спасибо", "care_memos_attached": ["Уход", "Сушка"]}),
            body="Спасибо",
            status=NotificationOutboxStatus.SENT,
            dedupe_key="hist-1",
            attempt_count=1,
            created_at=datetime(2026, 10, 12, 8, 0),
            sent_at=datetime(2026, 10, 12, 8, 1),
        )
    )
    db.flush()
    timeline = booking_notify_timeline(db, booking.id, tz_name="UTC")
    assert timeline[0]["memos_label"] == "Памятки: Уход, Сушка"


def test_care_memos_in_media_archive(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LB_MEDIA_ROOT", str(tmp_path))
    root_name = "a" * 32 + ".jpg"
    (tmp_path / root_name).write_bytes(b"root")
    rel, _, _, _ = store_care_memo_bytes(b"memo", "pic.png")
    manifest = build_media_manifest(tmp_path)
    names = {item["name"] for item in manifest["files"]}
    assert root_name in names
    assert rel in names
    from app.media_store import iter_media_backup_paths

    zip_path = _build_backup_zip(iter_media_backup_paths(tmp_path))
    with zipfile.ZipFile(zip_path, "r") as zf:
        listed = set(zf.namelist())
    assert root_name in listed
    assert rel in listed
    assert media_backup_arcname(tmp_path, tmp_path / rel) == rel


def test_booking_deadline_collects_memos(db, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LB_MEDIA_ROOT", str(tmp_path))
    memo = _memo(db)
    _cat, _sub, svc = _catalog(db, cat_memo=memo)
    user, client = _people(db)
    from app.db.models import Setting
    from app.setting_keys import DISPLAY_TIMEZONE

    db.add(Setting(key=DISPLAY_TIMEZONE, value="Asia/Novosibirsk", updated_at=datetime(2026, 1, 1)))
    booking = Booking(
        created_by_user_id=user.id,
        client_id=client.id,
        planned_date=datetime(2026, 10, 12, 8, 0),
        kind=BookingKind.VISIT,
        status=BookingStatus.ACTIVE,
        masters_scope=VisitMastersScope.VISIT,
        same_master_shares_all_services=False,
    )
    db.add(booking)
    db.flush()
    db.add(BookingPlannedService(booking_id=booking.id, service_id=svc.id, sort_order=0))
    db.add(
        ClientNotificationRule(
            kind=KIND_AFTER,
            hours=2,
            latest_time=datetime(2026, 1, 1, 20, 0).time(),
            template="Ждём",
            position=0,
            is_enabled=True,
            send_care_memo=True,
            created_at=datetime(2026, 1, 1),
        )
    )
    db.flush()
    stats = enqueue_due_after_booking_deadlines(db, now=datetime(2026, 10, 12, 14, 0))
    assert stats["enqueued"] >= 1
    row = db.scalar(select(NotificationOutbox).limit(1))
    assert json.loads(row.payload_json)["care_memo_ids"] == [memo.id]


def test_name_match_when_service_missing(db, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LB_MEDIA_ROOT", str(tmp_path))
    memo = _memo(db, "По имени")
    _cat, _sub, svc = _catalog(db, cat_memo=memo)
    _, client = _people(db)
    visit = Visit(
        created_at=datetime(2026, 10, 12, 8, 0),
        performed_date=datetime(2026, 10, 12),
        client_id=client.id,
        client_type=VisitClientType.RETURNING,
        price_type=VisitPriceType.CLIENT,
        masters_scope=VisitMastersScope.VISIT,
    )
    db.add(visit)
    db.flush()
    db.add(
        VisitService(
            visit_id=visit.id,
            service_id=svc.id,
            category_name="Косы",
            subcategory_name="Афро",
            service_name="Сенегалки",
            sort_order=0,
        )
    )
    db.flush()
    row = db.scalar(select(VisitService).limit(1))
    row.service_id = 999999
    db.flush()
    from app.care_memos import resolve_visit_service_memo

    assert resolve_visit_service_memo(db, row).id == memo.id
