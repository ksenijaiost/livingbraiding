from __future__ import annotations

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db import models as _orm_models  # noqa: F401
from app.db.base import Base
from app.db.models import Kit, SuperAdminPurgeLog, User
from app.kit_deletion import (
    KIT_DELETE_SOURCE_CARD,
    hard_delete_kit,
    kit_hard_delete_error,
    write_kit_deletion_log,
)


def _db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Sess = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    return Sess()


def _seed_kit(db, *, is_active: bool = False) -> Kit:
    user = User(username="u1", password_hash="x", display_name="U", role="ADMIN", is_active=True)
    db.add(user)
    db.flush()
    kit = Kit(
        sku="SKU-1",
        title="Test kit",
        is_active=is_active,
        pieces_total=1,
        pieces_available=1,
        composition_json='{"k1": 1}',
        created_by_user_id=user.id,
    )
    db.add(kit)
    db.flush()
    return kit


def test_kit_hard_delete_error_requires_inactive() -> None:
    db = _db()
    kit = _seed_kit(db, is_active=True)
    db.commit()
    assert kit_hard_delete_error(db, kit) is not None


def test_hard_delete_kit_writes_purge_log() -> None:
    db = _db()
    kit = _seed_kit(db, is_active=False)
    db.commit()
    kid = int(kit.id)
    hard_delete_kit(db, kit, actor_user_id=1, source=KIT_DELETE_SOURCE_CARD)
    db.commit()
    assert db.get(Kit, kid) is None
    log = db.scalar(select(SuperAdminPurgeLog).where(SuperAdminPurgeLog.entity_ids_text == str(kid)))
    assert log is not None
    assert log.entity_kind == "kit"
    assert "SKU-1" in (log.details_text or "")
    assert "kit_card" in (log.details_text or "")


def test_write_kit_deletion_log_includes_composition() -> None:
    db = _db()
    kit = _seed_kit(db, is_active=False)
    db.commit()
    write_kit_deletion_log(db, kit, actor_user_id=1, source=KIT_DELETE_SOURCE_CARD)
    db.commit()
    log = db.scalar(select(SuperAdminPurgeLog))
    assert log is not None
    assert "composition_json" in (log.details_text or "")
