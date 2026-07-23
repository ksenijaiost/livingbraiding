"""Резолв учётной даты проводки и backfill PayrollFundLedger.effective_at."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.db.models import (
    Booking,
    BookingKind,
    BookingStatus,
    HourlyWorkEntry,
    PayrollFundEntryKind,
    PayrollFundLedger,
    PayrollFundSourceKind,
    PayrollPeriod,
    ProductSale,
    StudioExpense,
    Visit,
    VisitService,
    WorkForInventory,
)
from app.settings import get_settings


def work_event_at(work: WorkForInventory) -> datetime:
    return work.performed_date or work.created_at


def consultation_fulfillment_event_at(db: Session, booking: Booking) -> datetime | None:
    """Дата визита/продажи, с которых считается ЗП за консультацию."""
    if booking.kind == BookingKind.VISIT:
        v = db.scalar(
            select(Visit)
            .where(Visit.booking_id == booking.id, Visit.is_cancelled.is_(False))
            .order_by(Visit.id.desc())
            .limit(1)
        )
        return v.performed_date if v else None
    if booking.kind == BookingKind.PRODUCT_SALE:
        s = db.scalar(
            select(ProductSale)
            .where(ProductSale.booking_id == booking.id, ProductSale.is_voided.is_(False))
            .order_by(ProductSale.id.desc())
            .limit(1)
        )
        return s.performed_date if s else None
    return None


def resolve_effective_at_for_ledger_row(
    db: Session,
    *,
    source_kind: str | PayrollFundSourceKind,
    source_id: int | None,
    created_at: datetime,
) -> datetime:
    """Дата события для backfill / резолва."""
    sk = source_kind.value if isinstance(source_kind, PayrollFundSourceKind) else str(source_kind)
    if sk == PayrollFundSourceKind.MANUAL.value:
        return created_at
    if source_id is None:
        return created_at

    if sk == PayrollFundSourceKind.VISIT.value:
        visit = db.get(Visit, int(source_id))
        return visit.performed_date if visit else created_at

    if sk == PayrollFundSourceKind.VISIT_SERVICE.value:
        vs = db.get(VisitService, int(source_id))
        if not vs:
            return created_at
        visit = db.get(Visit, int(vs.visit_id))
        return visit.performed_date if visit else created_at

    if sk == PayrollFundSourceKind.WORK.value:
        work = db.get(WorkForInventory, int(source_id))
        return work_event_at(work) if work else created_at

    if sk == PayrollFundSourceKind.PRODUCT_SALE.value:
        sale = db.get(ProductSale, int(source_id))
        return sale.performed_date if sale else created_at

    if sk == PayrollFundSourceKind.HOURLY_WORK.value:
        entry = db.get(HourlyWorkEntry, int(source_id))
        return entry.performed_date if entry else created_at

    if sk == PayrollFundSourceKind.STUDIO_EXPENSE.value:
        exp = db.get(StudioExpense, int(source_id))
        return exp.date if exp else created_at

    if sk == PayrollFundSourceKind.CONSULTATION.value:
        b = db.scalar(
            select(Booking)
            .where(
                Booking.consultation_id == int(source_id),
                Booking.status == BookingStatus.DONE,
            )
            .limit(1)
        )
        if b:
            evt = consultation_fulfillment_event_at(db, b)
            if evt is not None:
                return evt
        return created_at

    return created_at


def _date_in_closed_period(db: Session, event_at: datetime) -> bool:
    p = db.scalar(
        select(PayrollPeriod.id).where(
            PayrollPeriod.closed_at.is_not(None),
            PayrollPeriod.date_from <= event_at,
            PayrollPeriod.date_to >= event_at,
        )
    )
    return p is not None


def backfill_payroll_ledger_effective_at(db: Session, *, allow_closed: bool | None = None) -> int:
    """
    Проставить effective_at у всех строк журнала.
    Возвращает число обновлённых строк.
    """
    if allow_closed is None:
        allow_closed = bool(get_settings().payroll_ledger_backfill_closed)

    updated = 0
    rows = list(db.scalars(select(PayrollFundLedger).order_by(PayrollFundLedger.id.asc())).all())
    by_id = {int(r.id): r for r in rows}

    for r in rows:
        if r.entry_kind == PayrollFundEntryKind.STORNO:
            continue
        target = resolve_effective_at_for_ledger_row(
            db,
            source_kind=r.source_kind,
            source_id=r.source_id,
            created_at=r.created_at,
        )
        if (not allow_closed) and _date_in_closed_period(db, target):
            # Не сдвигаем учёт в закрытый период: оставляем текущую safe-дату или created_at.
            cur = getattr(r, "effective_at", None)
            if cur is not None and not _date_in_closed_period(db, cur):
                continue
            target = r.created_at
        if getattr(r, "effective_at", None) != target:
            r.effective_at = target
            updated += 1

    db.flush()

    for r in rows:
        if r.entry_kind != PayrollFundEntryKind.STORNO:
            continue
        parent = by_id.get(int(r.storno_of_id)) if r.storno_of_id else None
        if parent is None and r.storno_of_id:
            parent = db.get(PayrollFundLedger, int(r.storno_of_id))
        target = parent.effective_at if parent and getattr(parent, "effective_at", None) else r.created_at
        if getattr(r, "effective_at", None) != target:
            r.effective_at = target
            updated += 1

    db.flush()
    return updated


def seed_effective_at_from_created_at_sql(bind) -> None:
    """Первичный SQL: заполнить NULL значениями created_at перед Python-backfill."""
    bind.execute(
        text(
            "UPDATE payroll_fund_ledger SET effective_at = created_at "
            "WHERE effective_at IS NULL"
        )
    )
