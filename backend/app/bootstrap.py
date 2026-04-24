from __future__ import annotations

import os

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import User, UserRole, UserRoleAssignment
from app.security import hash_password
from app.time_utils import utcnow_naive


def ensure_initial_techspec_user(db: Session) -> None:
    """
    Первый старт на пустой базе: создаём техпользователя для настройки/отладки.
    Без сидов, всегда, но только если таблица users пустая.
    """
    users_count = int(db.scalar(select(func.count()).select_from(User)) or 0)
    if users_count > 0:
        return

    username = (os.environ.get("LB_TECHSPEC_USERNAME") or "techspec").strip().lower()
    password = (os.environ.get("LB_TECHSPEC_PASSWORD") or "techspec").strip()
    display_name = (os.environ.get("LB_TECHSPEC_DISPLAY_NAME") or "Техспец").strip()

    u = User(
        username=username,
        display_name=display_name,
        role=UserRole.TECHSPEC,
        password_hash=hash_password(password),
        is_active=True,
        master_level=None,
        phone=None,
        created_at=utcnow_naive(),
    )
    db.add(u)
    db.flush()
    db.add(UserRoleAssignment(user_id=u.id, role=UserRole.TECHSPEC))
    db.commit()
