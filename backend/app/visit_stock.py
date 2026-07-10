"""Откат складских списаний по визиту / строке услуги."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Kit, Visit, VisitKitUsage
from app.kit_blank_stock_core import (
    parse_usage_breakdown_json,
    planned_kit_stock_revert_pieces,
    return_stock_to_kit,
)


def _revert_kit_usages(
    db: Session,
    usages: list[VisitKitUsage],
) -> tuple[bool, str]:
    if not usages:
        return True, ""
    kit_rows: list[tuple[Kit, int, dict[str, int] | None]] = []
    for u in usages:
        kit = db.get(Kit, u.kit_id)
        if not kit:
            return False, "Не найден комплект для отката списания (kit_id)."
        pieces = int(u.pieces_used or 0)
        if pieces <= 0:
            continue
        bd = parse_usage_breakdown_json(getattr(u, "usage_breakdown_json", None))
        revert_pieces, revert_bd = planned_kit_stock_revert_pieces(db, kit, pieces, bd)
        kit_rows.append((kit, revert_pieces, revert_bd))
    for kit, pieces, bd in kit_rows:
        if pieces > 0:
            return_stock_to_kit(db, kit_id=int(kit.id), breakdown=bd, pieces_used=pieces)
            if kit.pieces_available > 0:
                kit.is_in_stock = True
    for u in usages:
        db.delete(u)
    return True, ""


def visit_service_revert_stock(db: Session, visit_service_id: int) -> tuple[bool, str]:
    usages = list(
        db.scalars(select(VisitKitUsage).where(VisitKitUsage.visit_service_id == visit_service_id)).all()
    )
    return _revert_kit_usages(db, usages)


def visit_cancel_revert_stock(db: Session, visit: Visit) -> tuple[bool, str]:
    """Revert stock kit usages for a visit."""
    usages = list(getattr(visit, "kit_usages", []) or [])
    return _revert_kit_usages(db, usages)
