from __future__ import annotations


def test_clients_export_forbidden_without_super_role() -> None:
    from starlette.testclient import TestClient

    from app.auth import AuthUser, get_current_user
    from app.db.models import MasterLevel, UserRole
    from app.main import app

    def fake_user() -> AuthUser:
        return AuthUser(
            id=1,
            username="m",
            display_name="Master",
            role=UserRole.MASTER,
            roles=(UserRole.MASTER, UserRole.ADMIN),
            master_level=MasterLevel.MIDDLE,
        )

    app.dependency_overrides[get_current_user] = fake_user
    try:
        client = TestClient(app)
        r = client.get("/clients/export", follow_redirects=False)
        assert r.status_code == 403
    finally:
        app.dependency_overrides.clear()


def test_clients_export_redirects_when_not_logged_in() -> None:
    from starlette.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    r = client.get("/clients/export", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers.get("location", "").startswith("/login")


def test_build_all_clients_csv_bom_and_columns() -> None:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.client_export import build_all_clients_csv_bytes
    from app.db.base import Base
    from app.db.models import Client

    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Sess = sessionmaker(bind=engine, autocommit=False, autoflush=False, future=True)
    db = Sess()
    db.add(Client(name="Иван", phone="79990001122", is_confirmed=False))
    db.commit()

    raw = build_all_clients_csv_bytes(db)
    assert raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig")
    assert "Имя" in text
    assert "79990001122" in text
    assert "нет" in text  # is_confirmed
    assert "Число визитов" in text
    assert text.splitlines()[-1].split(";")[-1] == "0"
