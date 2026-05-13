"""Безвозвратное удаление тестовых сущностей: только суперадмин, с явным подтверждением."""

from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session, selectinload

from app.audit import diff_fields, write_audit_rows
from app.db.models import (
    Booking,
    Client,
    Kit,
    KitAuditLog,
    KitReserve,
    ProductSale,
    ProductSaleKind,
    Visit,
    VisitAuditLog,
    VisitKitUsage,
    WorkForInventory,
)
from app.forms_parse import parse_int
from app.payroll_fund import PayrollFundSourceKind, storno_source_accruals
from app.routes.bookings import release_booking_kit_reserves
from app.routes.visits import _visit_cancel_revert_stock
from app.time_utils import utcnow_naive
from app.product_sales import _apply_kit_delta

CONFIRM_PHRASE_1 = "УДАЛИТЬ НАВСЕГДА"
CONFIRM_PHRASE_2 = "ТОЧНО-ТОЧНО"


def release_client_kit_reserves(db: Session, *, client_id: int, changed_by_user_id: int | None) -> None:
    """Резервы по клиенту без привязки к брони (и остатки после снятия броней): вернуть заготовки и удалить строки."""
    rows = list(
        db.scalars(
            select(KitReserve)
            .where(KitReserve.reserved_for_client_id == int(client_id))
            .order_by(KitReserve.id.asc())
        ).all()
    )
    for r in rows:
        kit = db.get(Kit, r.kit_id)
        if kit is None:
            db.delete(r)
            continue
        before = SimpleNamespace(pieces_available=kit.pieces_available)
        kit.pieces_available = int(kit.pieces_available or 0) + int(r.pieces_reserved or 0)
        kit.updated_at = utcnow_naive()
        if changed_by_user_id is not None:
            kit.updated_by_user_id = changed_by_user_id
        db.delete(r)
        if changed_by_user_id is not None:
            write_audit_rows(
                db,
                log_model=KitAuditLog,
                entity_field="kit_id",
                entity_id=kit.id,
                changed_by_user_id=changed_by_user_id,
                changes=diff_fields(before, kit, ("pieces_available",)),
            )


def purge_visit_hard(db: Session, visit_id: int, *, actor_user_id: int | None) -> None:
    visit = db.scalar(
        select(Visit)
        .options(selectinload(Visit.kit_usages).selectinload(VisitKitUsage.kit))
        .where(Visit.id == int(visit_id))
    )
    if visit is None:
        raise ValueError("Визит не найден.")
    ok, err = _visit_cancel_revert_stock(db, visit)
    if not ok:
        raise ValueError(err or "Не удалось вернуть комплект на склад для этого визита.")
    storno_source_accruals(db, PayrollFundSourceKind.VISIT, visit.id, actor_user_id)
    db.execute(delete(VisitAuditLog).where(VisitAuditLog.visit_id == visit.id))
    db.delete(visit)


def purge_booking_hard(db: Session, booking_id: int, *, actor_user_id: int | None) -> None:
    b = db.get(Booking, int(booking_id))
    if b is None:
        raise ValueError("Бронь не найдена.")
    release_booking_kit_reserves(db, booking_id=b.id, changed_by_user_id=actor_user_id)
    bid = int(b.id)
    db.execute(update(Visit).where(Visit.booking_id == bid).values(booking_id=None))
    db.execute(update(ProductSale).where(ProductSale.booking_id == bid).values(booking_id=None))
    db.execute(update(WorkForInventory).where(WorkForInventory.booking_id == bid).values(booking_id=None))
    db.delete(b)


def purge_product_sale_hard(db: Session, sale_id: int, *, actor_user_id: int | None) -> None:
    sale = db.get(ProductSale, int(sale_id))
    if sale is None:
        raise ValueError("Продажа не найдена.")
    if not sale.is_voided:
        storno_source_accruals(db, PayrollFundSourceKind.PRODUCT_SALE, sale.id, actor_user_id)
        if sale.kind == ProductSaleKind.KIT and sale.kit_id and sale.kit_pieces_sold:
            _apply_kit_delta(db, int(sale.kit_id), int(sale.kit_pieces_sold))
    db.delete(sale)


def purge_work_hard(db: Session, work_id: int, *, actor_user_id: int | None) -> None:
    w = db.get(WorkForInventory, int(work_id))
    if w is None:
        raise ValueError("Работа не найдена.")
    if not w.is_voided:
        storno_source_accruals(db, PayrollFundSourceKind.WORK, w.id, actor_user_id)
        if w.created_kit_id:
            kit = db.get(Kit, int(w.created_kit_id))
            if kit is not None:
                kit.is_archived = True
                kit.is_in_stock = False
                kit.pieces_available = 0
                kit.updated_at = utcnow_naive()
                if actor_user_id is not None:
                    kit.updated_by_user_id = actor_user_id
    db.delete(w)


def purge_client_hard(db: Session, client_id: int, *, actor_user_id: int | None) -> None:
    cid = int(client_id)
    c = db.get(Client, cid)
    if c is None:
        raise ValueError("Клиент не найден.")

    visit_ids = list(db.scalars(select(Visit.id).where(Visit.client_id == cid).order_by(Visit.id.asc())).all())
    for vid in visit_ids:
        purge_visit_hard(db, int(vid), actor_user_id=actor_user_id)

    sale_ids = list(db.scalars(select(ProductSale.id).where(ProductSale.client_id == cid).order_by(ProductSale.id.asc())).all())
    for sid in sale_ids:
        purge_product_sale_hard(db, int(sid), actor_user_id=actor_user_id)

    work_ids = list(
        db.scalars(select(WorkForInventory.id).where(WorkForInventory.client_id == cid).order_by(WorkForInventory.id.asc())).all()
    )
    for wid in work_ids:
        purge_work_hard(db, int(wid), actor_user_id=actor_user_id)

    booking_ids = list(db.scalars(select(Booking.id).where(Booking.client_id == cid).order_by(Booking.id.asc())).all())
    for bid in booking_ids:
        purge_booking_hard(db, int(bid), actor_user_id=actor_user_id)

    release_client_kit_reserves(db, client_id=cid, changed_by_user_id=actor_user_id)
    db.delete(c)


def run_purge(
    db: Session,
    *,
    entity: str,
    entity_id: int,
    confirm1: str,
    confirm2: str,
    actor_user_id: int | None,
) -> None:
    if (confirm1 or "").strip() != CONFIRM_PHRASE_1 or (confirm2 or "").strip() != CONFIRM_PHRASE_2:
        raise ValueError(
            f"Подтверждение: в первое поле введите «{CONFIRM_PHRASE_1}», во второе — «{CONFIRM_PHRASE_2}»."
        )
    kind = (entity or "").strip().lower()
    if kind == "visit":
        purge_visit_hard(db, entity_id, actor_user_id=actor_user_id)
    elif kind == "booking":
        purge_booking_hard(db, entity_id, actor_user_id=actor_user_id)
    elif kind == "product_sale":
        purge_product_sale_hard(db, entity_id, actor_user_id=actor_user_id)
    elif kind == "work":
        purge_work_hard(db, entity_id, actor_user_id=actor_user_id)
    elif kind == "client":
        purge_client_hard(db, entity_id, actor_user_id=actor_user_id)
    else:
        raise ValueError("Неизвестный тип объекта.")


def parse_purge_entity(entity: str, entity_id_raw: str) -> tuple[str, int]:
    e = (entity or "").strip().lower()
    try:
        eid = parse_int((entity_id_raw or "").strip(), min=1, field_name="id")
    except ValueError as exc:
        raise ValueError(str(exc)) from exc
    return e, int(eid)
