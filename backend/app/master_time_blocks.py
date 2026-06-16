"""Ручные блоки занятости мастера (работы и пр.)."""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import MasterScheduleDay, MasterScheduleStatus, MasterTimeBlock
from app.master_schedule import (
    TimeRangeMinutes,
    _interval_overlaps,
    _time_to_minutes,
    day_state,
    resolve_day_interval,
)

COLOR_TIME_BLOCK = "#cccac0"


class TimeBlockValidationError(ValueError):
    pass


def time_block_to_dict(block: MasterTimeBlock) -> dict[str, Any]:
    return {
        "id": int(block.id),
        "date": block.block_date.isoformat(),
        "time_from": block.time_from.strftime("%H:%M"),
        "time_to": block.time_to.strftime("%H:%M"),
        "comment": (block.comment or "").strip(),
    }


def list_time_blocks_for_day(db: Session, *, master_id: int, block_date: date) -> list[MasterTimeBlock]:
    return list(
        db.scalars(
            select(MasterTimeBlock)
            .where(MasterTimeBlock.master_id == master_id, MasterTimeBlock.block_date == block_date)
            .order_by(MasterTimeBlock.time_from.asc(), MasterTimeBlock.id.asc())
        ).all()
    )


def list_time_blocks_for_masters_on_day(
    db: Session, *, master_ids: list[int], block_date: date
) -> list[MasterTimeBlock]:
    if not master_ids:
        return []
    return list(
        db.scalars(
            select(MasterTimeBlock)
            .where(
                MasterTimeBlock.master_id.in_(master_ids),
                MasterTimeBlock.block_date == block_date,
            )
            .order_by(MasterTimeBlock.master_id.asc(), MasterTimeBlock.time_from.asc())
        ).all()
    )


def _validate_block_interval(
    db: Session,
    *,
    master_id: int,
    block_date: date,
    time_from: time,
    time_to: time,
    exclude_block_id: int | None = None,
) -> tuple[int, int]:
    if time_to <= time_from:
        raise TimeBlockValidationError("Время окончания должно быть позже начала")

    st = day_state(db, master_id=master_id, d=block_date)
    if st != "working":
        raise TimeBlockValidationError("Блок можно поставить только в рабочий день")

    row = db.scalar(
        select(MasterScheduleDay).where(
            MasterScheduleDay.master_id == master_id, MasterScheduleDay.work_date == block_date
        )
    )
    if row is None or row.status != MasterScheduleStatus.WORKING:
        raise TimeBlockValidationError("Блок можно поставить только в рабочий день")

    work_start, work_end = resolve_day_interval(db, row)
    start_m = _time_to_minutes(time_from)
    end_m = _time_to_minutes(time_to)
    assert start_m is not None and end_m is not None

    if start_m < work_start or end_m > work_end:
        raise TimeBlockValidationError("Интервал должен быть внутри рабочей смены")

    break_start = _time_to_minutes(row.break_from)
    break_end = _time_to_minutes(row.break_to)
    block_range = TimeRangeMinutes(start_minutes=start_m, end_minutes=end_m)
    if break_start is not None and break_end is not None and break_end > break_start:
        break_range = TimeRangeMinutes(start_minutes=break_start, end_minutes=break_end)
        if _interval_overlaps(block_range, break_range):
            raise TimeBlockValidationError("Интервал не должен пересекать перерыв")

    existing = list_time_blocks_for_day(db, master_id=master_id, block_date=block_date)
    for b in existing:
        if exclude_block_id is not None and int(b.id) == int(exclude_block_id):
            continue
        bs = _time_to_minutes(b.time_from)
        be = _time_to_minutes(b.time_to)
        assert bs is not None and be is not None
        if _interval_overlaps(block_range, TimeRangeMinutes(start_minutes=bs, end_minutes=be)):
            raise TimeBlockValidationError("Пересечение с другим блоком занятости")

    from app.calendar_occupancy import build_occupancy_for_day

    occ = build_occupancy_for_day(db, day=block_date, hour_from=0, hour_to=24, bookings=None)
    for seg in occ.get("segments") or []:
        if int(seg.get("master_id") or 0) != int(master_id):
            continue
        seg_range = TimeRangeMinutes(
            start_minutes=int(seg["start_minutes"]),
            end_minutes=int(seg["end_minutes"]),
        )
        if _interval_overlaps(block_range, seg_range):
            raise TimeBlockValidationError("Пересечение с существующей бронью")

    return start_m, end_m


def master_interval_overlaps_time_block(
    db: Session,
    *,
    master_id: int,
    d: date,
    start_m: int,
    end_m: int,
) -> bool:
    service_range = TimeRangeMinutes(start_minutes=start_m, end_minutes=end_m)
    for b in list_time_blocks_for_day(db, master_id=master_id, block_date=d):
        bs = _time_to_minutes(b.time_from)
        be = _time_to_minutes(b.time_to)
        assert bs is not None and be is not None
        if _interval_overlaps(service_range, TimeRangeMinutes(start_minutes=bs, end_minutes=be)):
            return True
    return False


def create_time_block(
    db: Session,
    *,
    master_id: int,
    block_date: date,
    time_from: time,
    time_to: time,
    comment: str | None,
    created_by_user_id: int | None,
) -> MasterTimeBlock:
    _validate_block_interval(
        db,
        master_id=master_id,
        block_date=block_date,
        time_from=time_from,
        time_to=time_to,
    )
    block = MasterTimeBlock(
        master_id=master_id,
        block_date=block_date,
        time_from=time_from,
        time_to=time_to,
        comment=(comment or "").strip() or None,
        created_at=datetime.utcnow(),
        created_by_user_id=created_by_user_id,
    )
    db.add(block)
    db.flush()
    return block


def delete_time_block(db: Session, *, block_id: int, master_id: int) -> bool:
    block = db.scalar(
        select(MasterTimeBlock).where(MasterTimeBlock.id == block_id, MasterTimeBlock.master_id == master_id)
    )
    if block is None:
        return False
    db.delete(block)
    return True
