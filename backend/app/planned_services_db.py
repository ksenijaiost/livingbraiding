"""Сохранение нескольких услуг в консультации и брони."""

from __future__ import annotations

from datetime import datetime, time
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.db.models import (
    Booking,
    BookingPlannedService,
    Consultation,
    ConsultationService,
    Service,
)


def parse_service_ids_from_form(form: Any, *, list_field: str = "planned_service_ids") -> list[int]:
    """ID услуг из hidden JSON или getlist чекбоксов."""
    raw = form.get(list_field)
    ids: list[int] = []
    if raw is not None and not hasattr(raw, "read"):
        s = raw.decode().strip() if isinstance(raw, (bytes, bytearray)) else str(raw).strip()
        if s.startswith("["):
            import json

            try:
                arr = json.loads(s)
                if isinstance(arr, list):
                    for x in arr:
                        try:
                            i = int(x)
                        except (TypeError, ValueError):
                            continue
                        if i > 0 and i not in ids:
                            ids.append(i)
            except json.JSONDecodeError:
                pass
        elif s.isdigit():
            ids.append(int(s))
    if hasattr(form, "getlist"):
        for x in form.getlist("planned_service_on"):
            try:
                i = int(str(x).strip())
            except ValueError:
                continue
            if i > 0 and i not in ids:
                ids.append(i)
    return ids


def sync_consultation_services(db: Session, consultation_id: int, service_ids: list[int]) -> None:
    db.execute(delete(ConsultationService).where(ConsultationService.consultation_id == consultation_id))
    for i, sid in enumerate(service_ids):
        if db.get(Service, sid) is None:
            continue
        db.add(ConsultationService(consultation_id=consultation_id, service_id=sid, sort_order=i))
    c = db.get(Consultation, consultation_id)
    if c:
        c.service_id = service_ids[0] if service_ids else None


def sync_booking_planned_services(
    db: Session,
    booking_id: int,
    service_ids: list[int],
    *,
    planned_date: datetime | None = None,
) -> None:
    db.execute(delete(BookingPlannedService).where(BookingPlannedService.booking_id == booking_id))
    for i, sid in enumerate(service_ids):
        if db.get(Service, sid) is None:
            continue
        db.add(
            BookingPlannedService(
                booking_id=booking_id,
                service_id=sid,
                sort_order=i,
                planned_start_time=None,
            )
        )
    b = db.get(Booking, booking_id)
    if b:
        b.planned_service_id = service_ids[0] if service_ids else None


def consultation_service_ids(db: Session, consultation_id: int) -> list[int]:
    rows = list(
        db.scalars(
            select(ConsultationService.service_id)
            .where(ConsultationService.consultation_id == consultation_id)
            .order_by(ConsultationService.sort_order.asc(), ConsultationService.id.asc())
        ).all()
    )
    if rows:
        return [int(x) for x in rows]
    c = db.get(Consultation, consultation_id)
    if c and c.service_id:
        return [int(c.service_id)]
    return []


def booking_planned_service_ids(db: Session, booking_id: int) -> list[int]:
    rows = list(
        db.scalars(
            select(BookingPlannedService.service_id)
            .where(BookingPlannedService.booking_id == booking_id)
            .order_by(BookingPlannedService.sort_order.asc(), BookingPlannedService.id.asc())
        ).all()
    )
    if rows:
        return [int(x) for x in rows]
    b = db.get(Booking, booking_id)
    if b and b.planned_service_id:
        return [int(b.planned_service_id)]
    return []
