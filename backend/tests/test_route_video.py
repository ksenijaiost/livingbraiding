"""3.28: видео «как нас найти» файлом в чат, без ссылки на сайт."""

from __future__ import annotations

import asyncio
import json
import urllib.request
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import AuthUser, get_current_user
from app.client_bot_replies import render_bot_reply
from app.client_notifications import render_client_notify_template
from app.client_notify_history import booking_notify_timeline
from app.db import models as _orm_models  # noqa: F401
from app.db.base import Base
from app.db.models import (
    Booking,
    BookingKind,
    BookingStatus,
    Client,
    NotificationChannel,
    NotificationOutbox,
    NotificationOutboxStatus,
    StudioTemplateVariable,
    User,
    UserRole,
    VisitMastersScope,
)
from app.db.session import get_db
from app.main import app
from app.notifications import process_outbox
from app.route_video_send import (
    ROUTE_VIDEO_ERROR_TEXT,
    deliver_route_video_for_bot_reply,
)
from app.studio_template_variables import (
    clear_route_video,
    ensure_builtin_studio_variables,
    save_builtin_values,
    save_route_video,
)


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _settings():
    return SimpleNamespace(
        telegram_bot_token="tg",
        telegram_api_base="https://tg.example",
        vk_group_token="vk",
        vk_group_id="555",
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


def _mock_http(monkeypatch, *, video_save_error: bool = False):
    calls: list[str] = []
    sleeps: list[float] = []
    max_messages = {"n": 0}

    def urlopen(req, timeout=0):
        url = req.full_url
        data = req.data or b""
        kind = "multipart" if data.startswith(b"----") else "form"
        snippet = data[:240].decode("utf-8", errors="replace")
        calls.append(f"{url} [{kind}] {snippet}")
        if "video.save" in url:
            if video_save_error:
                body = {"error": {"error_code": 15, "error_msg": "Access denied"}}
            else:
                body = {
                    "response": {
                        "upload_url": "https://upload.example/vk-video",
                        "video_id": 9,
                        "owner_id": -5,
                    }
                }
        elif "docs.getMessagesUploadServer" in url:
            body = {"response": {"upload_url": "https://upload.example/vk-doc"}}
        elif "docs.save" in url:
            body = {"response": {"doc": {"owner_id": -5, "id": 3}}}
        elif "messages.send" in url:
            body = {"response": 1}
        elif url.startswith("https://upload.example/vk-doc"):
            body = {"file": "docfile"}
        elif url.startswith("https://upload.example/vk-video"):
            body = {"ok": 1}
        elif "max.example" in url and "/uploads" in url:
            body = {"url": "https://upload.example/max-video"}
        elif url.startswith("https://upload.example/max-video"):
            body = {"token": "vtok"}
        elif "max.example" in url and "/messages" in url:
            max_messages["n"] += 1
            if max_messages["n"] == 1:
                body = {"code": "attachment.not.ready"}
            else:
                body = {"message": {"body": {"mid": "1"}}}
        elif "sendVideo" in url:
            body = {"ok": True, "result": {"video": {"file_id": "vid-1"}}}
        else:
            body = {"ok": True}
        return _Resp(json.dumps(body).encode())

    def max_request(method, url, *, content=None, headers=None, timeout=8):
        req = urllib.request.Request(url, data=content, method=method, headers=headers or {})
        with urlopen(req, timeout=timeout) as resp:
            return 200, resp.read()

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    monkeypatch.setattr("app.max_http.max_request", max_request)
    monkeypatch.setattr("app.route_video_send.time.sleep", lambda sec: sleeps.append(sec))
    settings = _settings()
    monkeypatch.setattr("app.route_video_send.get_settings", lambda: settings)
    monkeypatch.setattr("app.care_memo_send.get_settings", lambda: settings)
    return calls, sleeps


def _video(db, tmp_path) -> StudioTemplateVariable:
    (tmp_path / "clip.mp4").write_bytes(b"fake-mp4")
    ensure_builtin_studio_variables(db)
    row = db.scalar(select(StudioTemplateVariable).where(StudioTemplateVariable.key == "route_video"))
    assert row is not None
    row.file_path = "clip.mp4"
    row.file_name = "how.mp4"
    row.file_size = 8
    row.kind = "video"
    row.vk_video_attachment = None
    row.max_video_token = None
    row.telegram_video_file_id = None
    row.public_token = "a" * 32
    db.flush()
    return row


def _people(db) -> tuple[User, Client, Booking]:
    user = User(
        username="adm",
        password_hash="x",
        display_name="Анна",
        role=UserRole.ADMIN,
        is_active=True,
    )
    client = Client(name="Катя", is_confirmed=True, vk_user_id=42)
    db.add_all([user, client])
    db.flush()
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
    return user, client, booking


def _outbox(db, client, booking, channel, *, text="Ждём вас", n=1) -> NotificationOutbox:
    row = NotificationOutbox(
        client_id=client.id,
        target_kind="client",
        booking_id=booking.id,
        channel=channel,
        event_type="client_booking_reminder",
        payload_json=json.dumps(
            {
                "text": text,
                "target_id": 42,
                "target_kind": "client",
                "attach_route_video": True,
            },
            ensure_ascii=False,
        ),
        body=text,
        status=NotificationOutboxStatus.PENDING,
        dedupe_key=f"rv-{channel.value}-{n}",
        attempt_count=0,
        created_at=datetime(2026, 10, 12, 8, 0),
        scheduled_at=datetime(2026, 10, 12, 8, 0),
    )
    db.add(row)
    db.flush()
    return row


def test_text_has_no_site_link(db, monkeypatch) -> None:
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://crm.example.com")
    ensure_builtin_studio_variables(db)
    save_builtin_values(db, {"address": "дом 5"}, user_id=None)
    row = db.scalar(select(StudioTemplateVariable).where(StudioTemplateVariable.key == "route_video"))
    assert row is not None
    row.public_token = "a" * 32
    row.file_path = "clip.mp4"
    db.commit()
    directions = render_bot_reply(db, "directions_5")
    assert directions == "Как добраться: дом 5"
    assert "/media/" not in directions
    assert "crm.example.com" not in directions
    text = render_client_notify_template(
        "Адрес: {{address}}\n{{route_video}}\nЖдём вас",
        booking=SimpleNamespace(planned_date=datetime(2026, 1, 1), planned_services=[], planned_service=None, masters=[]),  # type: ignore[arg-type]
        client=SimpleNamespace(name="Катя"),  # type: ignore[arg-type]
        tz_name="UTC",
        db=db,
        substitute_system=False,
        system_keys=frozenset({"name"}),
    )
    assert text == "Адрес: дом 5\nЖдём вас"
    assert "/media/" not in text
    assert "crm.example.com" not in text


def test_video_attaches_on_each_channel(db, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LB_MEDIA_ROOT", str(tmp_path))
    calls, sleeps = _mock_http(monkeypatch)
    monkeypatch.setattr("app.notifications.send_vk", lambda *a, **k: None)
    monkeypatch.setattr("app.notifications.send_max", lambda *a, **k: None)
    monkeypatch.setattr("app.notifications.send_telegram", lambda *a, **k: None)
    row = _video(db, tmp_path)
    _, client, booking = _people(db)
    sent: list[str] = []
    for n, channel in enumerate(
        (NotificationChannel.VK, NotificationChannel.MAX, NotificationChannel.TELEGRAM),
        start=1,
    ):
        out = _outbox(db, client, booking, channel, n=n)
        stats = process_outbox(db, only_ids=[out.id])
        assert stats["sent"] == 1
        db.refresh(out)
        assert out.status == NotificationOutboxStatus.SENT
        assert out.body == "Ждём вас"
        sent.append(channel.value)
    assert sent == ["vk", "max", "telegram"]
    joined = "\n".join(calls)
    assert "video.save" in joined
    assert "group_id=555" in joined
    assert "attachment=video-5_9" in joined
    assert "https://max.example/uploads?type=video" in joined
    assert '"type": "video"' in joined
    assert "sendVideo" in joined
    assert sleeps == [1.5]
    db.refresh(row)
    assert row.vk_video_attachment == "video-5_9"
    assert row.max_video_token == "vtok"
    assert row.telegram_video_file_id == "vid-1"

    calls.clear()
    sleeps.clear()
    again = _outbox(db, client, booking, NotificationChannel.VK, n=4)
    process_outbox(db, only_ids=[again.id])
    cached = "\n".join(calls)
    assert "video.save" not in cached
    assert "attachment=video-5_9" in cached

    calls.clear()
    tg = _outbox(db, client, booking, NotificationChannel.TELEGRAM, n=5)
    process_outbox(db, only_ids=[tg.id])
    tg_joined = "\n".join(calls)
    assert "vid-1" in tg_joined
    assert "[form]" in tg_joined
    assert "[multipart]" not in tg_joined

    calls.clear()
    mx = _outbox(db, client, booking, NotificationChannel.MAX, n=6)
    process_outbox(db, only_ids=[mx.id])
    mx_joined = "\n".join(calls)
    assert "/uploads" not in mx_joined
    assert "vtok" in mx_joined


def _mock_max_upload(monkeypatch, *, file_body: bytes, meta_token: str | None):
    uploads: list[bytes | None] = []

    def max_request(method, url, *, content=None, headers=None, timeout=8):
        if "/uploads" in url and "type=video" in url:
            uploads.append(content)
            meta = {"url": "https://upload.example/max-video"}
            if meta_token:
                meta["token"] = meta_token
            return 200, json.dumps(meta).encode()
        if url.startswith("https://upload.example/max-video"):
            return 200, file_body
        if "/messages" in url:
            return 200, json.dumps({"message": {"body": {"mid": "1"}}}).encode()
        return 200, b"{}"

    monkeypatch.setattr("app.max_http.max_request", max_request)
    monkeypatch.setattr("app.route_video_send.time.sleep", lambda sec: None)
    monkeypatch.setattr("app.route_video_send.get_settings", _settings)
    monkeypatch.setattr("app.care_memo_send.get_settings", _settings)
    monkeypatch.setattr("app.notifications.send_max", lambda *a, **k: None)
    return uploads


def test_max_video_upload_accepts_non_json_body(db, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LB_MEDIA_ROOT", str(tmp_path))
    uploads = _mock_max_upload(
        monkeypatch,
        file_body=b"<retval>1</retval>",
        meta_token="vtok",
    )
    row = _video(db, tmp_path)
    _, client, booking = _people(db)
    out = _outbox(db, client, booking, NotificationChannel.MAX)
    stats = process_outbox(db, only_ids=[out.id])
    assert stats["sent"] == 1
    assert uploads == [b""]
    db.refresh(row)
    assert row.max_video_token == "vtok"


def test_max_video_token_from_upload_json(db, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LB_MEDIA_ROOT", str(tmp_path))
    _mock_max_upload(
        monkeypatch,
        file_body=json.dumps({"token": "from-file"}).encode(),
        meta_token=None,
    )
    row = _video(db, tmp_path)
    _, client, booking = _people(db)
    out = _outbox(db, client, booking, NotificationChannel.MAX, n=2)
    stats = process_outbox(db, only_ids=[out.id])
    assert stats["sent"] == 1
    db.refresh(row)
    assert row.max_video_token == "from-file"


def test_attachment_error_history_hides_fake_attempt_count(db) -> None:
    _, client, booking = _people(db)
    now = datetime(2026, 10, 12, 8, 0)
    db.add_all(
        [
            NotificationOutbox(
                client_id=client.id,
                target_kind="client",
                booking_id=booking.id,
                channel=NotificationChannel.MAX,
                event_type="client_route_video_error",
                payload_json='{"text":"Видео „Как нас найти“ — ошибка","target_kind":"client"}',
                body="Видео „Как нас найти“ — ошибка",
                status=NotificationOutboxStatus.FAILED,
                error="некорректный JSON",
                dedupe_key="rv-err",
                attempt_count=99,
                created_at=now,
            ),
            NotificationOutbox(
                client_id=client.id,
                target_kind="client",
                booking_id=booking.id,
                channel=NotificationChannel.MAX,
                event_type="client_care_memo_error",
                payload_json='{"text":"Памятка — ошибка","target_kind":"client"}',
                body="Памятка — ошибка",
                status=NotificationOutboxStatus.FAILED,
                error="нет токена картинки",
                dedupe_key="memo-err",
                attempt_count=99,
                created_at=now,
            ),
            NotificationOutbox(
                client_id=client.id,
                target_kind="client",
                booking_id=booking.id,
                channel=NotificationChannel.VK,
                event_type="client_booking_reminder",
                payload_json='{"text":"напомнить","target_kind":"client"}',
                body="напомнить",
                status=NotificationOutboxStatus.FAILED,
                error="сеть",
                dedupe_key="remind-err",
                attempt_count=3,
                created_at=now,
            ),
        ]
    )
    db.flush()
    labels = [
        item["status_label"]
        for item in booking_notify_timeline(db, int(booking.id), tz_name="UTC")
    ]
    assert "ошибка: некорректный JSON" in labels
    assert "ошибка: нет токена картинки" in labels
    assert all("попыток: 99" not in label for label in labels)
    assert any("ошибка: сеть (попыток: 3)" in label for label in labels)


def test_vk_falls_back_to_doc(db, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LB_MEDIA_ROOT", str(tmp_path))
    calls, _sleeps = _mock_http(monkeypatch, video_save_error=True)
    monkeypatch.setattr("app.notifications.send_vk", lambda *a, **k: None)
    row = _video(db, tmp_path)
    _, client, booking = _people(db)
    out = _outbox(db, client, booking, NotificationChannel.VK)
    stats = process_outbox(db, only_ids=[out.id])
    assert stats["sent"] == 1
    joined = "\n".join(calls)
    assert "docs.getMessagesUploadServer" in joined
    assert "docs.save" in joined
    assert "attachment=doc-5_3" in joined
    db.refresh(row)
    assert row.vk_video_attachment == "doc-5_3"


def test_replace_clears_channel_cache(db, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LB_MEDIA_ROOT", str(tmp_path))
    row = _video(db, tmp_path)
    (tmp_path / "clip.mp4").write_bytes(b"old")
    row.vk_video_attachment = "video-1_2"
    row.telegram_video_file_id = "fid"
    row.max_video_token = "tok"
    db.commit()

    class _Upload:
        filename = "how.mp4"

        def __init__(self) -> None:
            self._done = False

        async def read(self, n: int = 1024) -> bytes:
            if self._done:
                return b""
            self._done = True
            return b"mp4bytes"

        async def seek(self, pos: int) -> None:
            return None

    asyncio.run(save_route_video(db, _Upload(), user_id=None))
    db.refresh(row)
    assert row.file_path != "clip.mp4"
    assert row.vk_video_attachment is None
    assert row.telegram_video_file_id is None
    assert row.max_video_token is None
    assert row.public_token is None

    row.vk_video_attachment = "video-9_9"
    row.max_video_token = "again"
    row.telegram_video_file_id = "again-tg"
    db.commit()
    clear_route_video(db, user_id=None)
    db.refresh(row)
    assert row.file_path is None
    assert row.vk_video_attachment is None
    assert row.max_video_token is None
    assert row.telegram_video_file_id is None


def test_video_error_does_not_block_text(db, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LB_MEDIA_ROOT", str(tmp_path))
    monkeypatch.setattr("app.notifications.send_vk", lambda *a, **k: None)

    def urlopen(req, timeout=0):
        raise OSError("upload down")

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    monkeypatch.setattr("app.route_video_send.get_settings", _settings)
    monkeypatch.setattr("app.care_memo_send.get_settings", _settings)
    _video(db, tmp_path)
    _, client, booking = _people(db)
    out = _outbox(db, client, booking, NotificationChannel.VK, text="Ждём вас")
    stats = process_outbox(db, only_ids=[out.id])
    assert stats["sent"] == 1
    db.refresh(out)
    assert out.status == NotificationOutboxStatus.SENT
    assert out.body == "Ждём вас"
    err = db.scalar(
        select(NotificationOutbox).where(NotificationOutbox.event_type == "client_route_video_error")
    )
    assert err is not None
    assert err.body == "Видео „Как нас найти“ — ошибка"
    assert err.body == ROUTE_VIDEO_ERROR_TEXT
    assert err.status == NotificationOutboxStatus.FAILED
    assert err.attempt_count == 99
    assert err.booking_id == booking.id
    labels = [
        item["type_label"]
        for item in booking_notify_timeline(db, int(booking.id), tz_name="UTC")
    ]
    assert "Видео „Как нас найти“ — ошибка" in labels


def test_bot_reply_attaches_video(db, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LB_MEDIA_ROOT", str(tmp_path))
    calls, _sleeps = _mock_http(monkeypatch)
    _video(db, tmp_path)
    _, client, _booking = _people(db)
    deliver_route_video_for_bot_reply(
        db,
        {
            "reply_key": "directions_5",
            "channel": "vk",
            "messenger_id": 42,
            "client_id": client.id,
            "booking_id": None,
        },
    )
    joined = "\n".join(calls)
    assert "video.save" in joined
    assert "group_id=555" in joined
    assert "attachment=video-5_9" in joined


def test_notifications_hint(db) -> None:
    user = User(
        username="adm",
        password_hash="x",
        display_name="Админ",
        role=UserRole.ADMIN,
        is_active=True,
    )
    db.add(user)
    db.commit()
    auth = AuthUser(
        id=int(user.id),
        username=user.username,
        display_name=user.display_name,
        role=UserRole.ADMIN,
        roles=(UserRole.ADMIN,),
    )

    def _user():
        return auth

    def _db():
        yield db

    app.dependency_overrides[get_current_user] = _user
    app.dependency_overrides[get_db] = _db
    try:
        res = TestClient(app).get("/clients/notifications")
        assert res.status_code == 200
        assert (
            "Видео отправляется файлом в чат, ссылка на сайт не показывается. "
            "Формат MP4 (H.264), до 50 МБ."
        ) in res.text
        assert "/media/v/" not in res.text
        assert "подставится ссылка" not in res.text
    finally:
        app.dependency_overrides.clear()
