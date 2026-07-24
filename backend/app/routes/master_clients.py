from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.auth import AuthUser, require_role
from app.db.models import Client, UserRole
from app.db.session import get_db
from app.display_time import format_naive_utc_datetime, get_display_timezone
from app.thermo_visit import list_client_thermo_templates_for_visit


router = APIRouter()


def _client_suggest_items(db: Session, q: str) -> list[dict[str, str | int | bool]]:
    needle = (q or "").strip()
    stmt = select(Client).order_by(Client.name.asc()).limit(30)
    if needle:
        digits = "".join(ch for ch in needle if ch.isdigit())
        phone_norm = func.replace(
            func.replace(
                func.replace(
                    func.replace(
                        func.replace(func.replace(func.coalesce(Client.phone, ""), "+", ""), " ", ""),
                        "-",
                        "",
                    ),
                    "(",
                    "",
                ),
                ")",
                "",
            ),
            ".",
            "",
        )
        conds = [Client.name.ilike(f"%{needle}%")]
        if digits:
            conds.append(phone_norm.like(f"%{digits}%"))
        stmt = select(Client).where(or_(*conds)).order_by(Client.name.asc()).limit(30)
    rows = list(db.scalars(stmt).all())
    clients: list[dict[str, str | int | bool]] = []
    for c in rows:
        parts: list[str] = []
        if c.phone:
            parts.append(c.phone)
        if c.telegram:
            parts.append(f"TG {c.telegram}")
        if c.vk:
            parts.append(f"VK {c.vk}")
        if c.instagram:
            parts.append(f"IG {c.instagram}")
        if c.other_contact:
            parts.append((c.other_contact or "")[:48])
        hint = " · ".join(parts) if parts else "без контакта"
        clients.append({"id": c.id, "name": c.name, "hint": hint, "is_draft": not c.is_confirmed})
    return clients


@router.get("/master/clients/suggest")
def master_clients_suggest(
    q: str = "",
    current_user: AuthUser = Depends(require_role(UserRole.MASTER, UserRole.ADMIN, UserRole.ADMIN_SUPER)),
    db: Session = Depends(get_db),
):
    return JSONResponse({"clients": _client_suggest_items(db, q)})


@router.get("/master/clients/{client_id}/thermo-templates")
def master_client_thermo_templates(
    client_id: int,
    current_user: AuthUser = Depends(require_role(UserRole.MASTER)),
    db: Session = Depends(get_db),
):
    rows = list_client_thermo_templates_for_visit(db, client_id)
    tz = get_display_timezone(db)
    return JSONResponse(
        {
            "templates": [
                {
                    "id": t.id,
                    "label": t.label,
                    "created_at": format_naive_utc_datetime(t.created_at, tz),
                }
                for t in rows
            ]
        }
    )

