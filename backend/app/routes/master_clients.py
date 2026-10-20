from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.auth import AuthUser, require_role
from app.db.models import Client, UserRole
from app.db.session import get_db
from app.phone_ru import display_phone, phone_search_digits
from app.display_time import format_naive_utc_datetime, get_display_timezone
from app.thermo_visit import list_client_thermo_templates_for_visit


router = APIRouter()


def _client_suggest_items(db: Session, q: str) -> list[dict[str, str | int | bool]]:
    from app.client_contact import expand_phone_search_clients, suggest_hint_for_client
    from sqlalchemy.orm import selectinload

    needle = (q or "").strip()
    stmt = select(Client).options(selectinload(Client.contact_client)).order_by(Client.name.asc()).limit(30)
    if needle:
        conds = [Client.name.ilike(f"%{needle}%")]
        phone_digits = phone_search_digits(needle)
        owners: list[Client] = []
        if phone_digits:
            owners = list(
                db.scalars(
                    select(Client)
                    .where(Client.phone_normalized.like(f"%{phone_digits}%"))
                    .order_by(Client.name.asc())
                    .limit(30)
                ).all()
            )
            conds.append(Client.phone_normalized.like(f"%{phone_digits}%"))
            if owners:
                conds.append(Client.contact_client_id.in_([int(o.id) for o in owners]))
        rows = list(db.scalars(stmt.where(or_(*conds))).all())
        if phone_digits and owners:
            rows = expand_phone_search_clients(db, owners)
            name_rows = list(
                db.scalars(
                    select(Client)
                    .where(Client.name.ilike(f"%{needle}%"))
                    .options(selectinload(Client.contact_client))
                    .order_by(Client.name.asc())
                    .limit(30)
                ).all()
            )
            seen = {int(c.id) for c in rows}
            for c in name_rows:
                if int(c.id) not in seen:
                    rows.append(c)
                    seen.add(int(c.id))
            rows = rows[:30]
    else:
        rows = list(db.scalars(stmt).all())
    clients: list[dict[str, str | int | bool]] = []
    for c in rows:
        clients.append(
            {
                "id": c.id,
                "name": c.name,
                "hint": suggest_hint_for_client(c),
                "is_draft": not c.is_confirmed,
            }
        )
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

