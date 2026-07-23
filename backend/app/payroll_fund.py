"""Журнал фондов ЗП: начисления с визита / работы / розницы, сторно, выплаты."""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from typing import Any, Sequence

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.time_utils import utcnow_naive

HOURLY_HELP_LEDGER_COMMENT = "Почасовая помощь"
from app.kit_blank_stock_core import (
    apply_discount_capped,
    keyed_client_price_selected,
    keyed_cost_selected,
    kit_inventory_is_keyed,
    load_catalog_kit_maps,
    parse_composition_totals,
)
from app.db.models import (
    Booking,
    BookingKind,
    BookingStatus,
    Consultation,
    HourlyWorkEntry,
    Kit,
    MaterialPriceCurrent,
    MaterialType,
    PayrollFundEntryKind,
    PayrollFundLedger,
    PayrollFundPayoutPaymentKind,
    PayrollFundSide,
    PayrollFundSourceKind,
    PayrollPeriod,
    ProductSale,
    ProductSaleKind,
    StudioExpense,
    User,
    UserRole,
    Visit,
    VisitMaster,
    VisitMastersScope,
    VisitService,
    VisitServiceMaster,
    WorkForInventory,
    WorkRate,
    WorkScope,
    WorkForInventoryStaff,
)
from app.payroll_ledger_backfill import consultation_fulfillment_event_at, work_event_at
from app.work_rate_keys import (
    CONSULTATION_PAY_AMOUNT_THRESHOLD,
    CONSULTATION_PAY_AT_OR_ABOVE_THRESHOLD,
    CONSULTATION_PAY_BELOW_THRESHOLD,
)

_REVERSIBLE_ENTRY_KINDS = (PayrollFundEntryKind.ACCRUAL, PayrollFundEntryKind.EXPENSE)


def money_q2(x: float) -> float:
    return round(float(x), 2)


def visit_ids_visible_to_master_clause(master_id: int):
    """Визиты, где участвует мастер (как в модалке дня календаря и статистике)."""
    mid = int(master_id)
    return or_(
        Visit.id.in_(select(VisitMaster.visit_id).where(VisitMaster.master_id == mid)),
        Visit.id.in_(
            select(VisitService.visit_id).where(
                VisitService.is_cancelled.is_(False),
                VisitService.mix_bonus_master_id == mid,
            )
        ),
        Visit.id.in_(
            select(VisitService.visit_id).where(
                VisitService.is_cancelled.is_(False),
                VisitService.correction_master_id == mid,
            )
        ),
        Visit.id.in_(
            select(VisitService.visit_id)
            .join(VisitServiceMaster, VisitServiceMaster.visit_service_id == VisitService.id)
            .where(
                VisitService.is_cancelled.is_(False),
                VisitServiceMaster.master_id == mid,
            )
        ),
        Visit.mix_bonus_master_id == mid,
        Visit.correction_master_id == mid,
    )


def sum_ledger_amounts_by_source(
    db: Session,
    *,
    side: PayrollFundSide,
    source_kind: PayrollFundSourceKind,
    source_ids: list[int],
    user_id: int | None = None,
) -> dict[int, float]:
    """Сумма проводок по source_id для одного source_kind."""
    if not source_ids:
        return {}
    stmt = (
        select(PayrollFundLedger.source_id, func.coalesce(func.sum(PayrollFundLedger.amount), 0.0))
        .where(
            PayrollFundLedger.side == side,
            PayrollFundLedger.source_kind == source_kind,
            PayrollFundLedger.source_id.in_(source_ids),
        )
        .group_by(PayrollFundLedger.source_id)
    )
    if user_id is not None:
        stmt = stmt.where(PayrollFundLedger.user_id == user_id)
    out: dict[int, float] = {}
    for sid, amt in db.execute(stmt).all():
        if sid is None:
            continue
        out[int(sid)] = money_q2(float(amt or 0.0))
    return out


def sum_visit_ledger_by_visit_id(
    db: Session,
    *,
    side: PayrollFundSide,
    visit_ids: list[int],
    user_id: int | None = None,
) -> dict[int, float]:
    """Сумма проводок по визиту: legacy VISIT + построчные VISIT_SERVICE."""
    if not visit_ids:
        return {}
    out = sum_ledger_amounts_by_source(
        db,
        side=side,
        source_kind=PayrollFundSourceKind.VISIT,
        source_ids=visit_ids,
        user_id=user_id,
    )
    stmt = (
        select(VisitService.visit_id, func.coalesce(func.sum(PayrollFundLedger.amount), 0.0))
        .join(VisitService, VisitService.id == PayrollFundLedger.source_id)
        .where(
            PayrollFundLedger.side == side,
            PayrollFundLedger.source_kind == PayrollFundSourceKind.VISIT_SERVICE,
            VisitService.visit_id.in_(visit_ids),
        )
        .group_by(VisitService.visit_id)
    )
    if user_id is not None:
        stmt = stmt.where(PayrollFundLedger.user_id == user_id)
    for vid, amt in db.execute(stmt).all():
        if vid is None:
            continue
        k = int(vid)
        out[k] = money_q2(out.get(k, 0.0) + float(amt or 0.0))
    return out


def visit_ids_for_visit_service_source_ids(db: Session, visit_service_ids: list[int]) -> dict[int, int]:
    """visit_service.id → visit.id (для подписи источника в журнале)."""
    if not visit_service_ids:
        return {}
    rows = db.execute(
        select(VisitService.id, VisitService.visit_id).where(VisitService.id.in_(visit_service_ids))
    ).all()
    return {int(vsid): int(vid) for vsid, vid in rows if vsid is not None and vid is not None}


def append_ledger(
    db: Session,
    *,
    entry_kind: PayrollFundEntryKind,
    side: PayrollFundSide,
    user_id: int | None,
    amount: float,
    source_kind: PayrollFundSourceKind,
    source_id: int | None,
    created_by_user_id: int | None,
    storno_of_id: int | None = None,
    comment: str | None = None,
    payout_payment_kind: PayrollFundPayoutPaymentKind | None = None,
    effective_at: datetime | None = None,
        ) -> PayrollFundLedger:
    if side == PayrollFundSide.MASTER and user_id is None:
        raise ValueError("MASTER требует user_id")
    if side == PayrollFundSide.STUDIO:
        if entry_kind == PayrollFundEntryKind.PAYOUT:
            if user_id is None:
                raise ValueError("Выплата из фонда студии: нужен user_id получателя")
        elif user_id is not None:
            raise ValueError("STUDIO: user_id должен быть NULL")
    if payout_payment_kind is not None and entry_kind != PayrollFundEntryKind.PAYOUT:
        raise ValueError("payout_payment_kind только для PAYOUT")
    now = utcnow_naive()
    row = PayrollFundLedger(
        created_at=now,
        effective_at=effective_at if effective_at is not None else now,
        entry_kind=entry_kind,
        side=side,
        user_id=user_id,
        amount=money_q2(amount),
        source_kind=source_kind,
        source_id=source_id,
        created_by_user_id=created_by_user_id,
        storno_of_id=storno_of_id,
        comment=comment,
        payout_payment_kind=payout_payment_kind,
    )
    db.add(row)
    return row


def _has_accruals_for_source(db: Session, source_kind: PayrollFundSourceKind, source_id: int) -> bool:
    return (
        db.scalar(
            select(PayrollFundLedger.id)
            .where(
                PayrollFundLedger.source_kind == source_kind,
                PayrollFundLedger.source_id == source_id,
                PayrollFundLedger.entry_kind == PayrollFundEntryKind.ACCRUAL,
            )
            .limit(1)
        )
        is not None
    )


def storno_source_accruals(
    db: Session,
    source_kind: PayrollFundSourceKind,
    source_id: int,
    created_by_user_id: int | None,
) -> None:
    """Сторно всех исходных проводок источника (начисления и расходы), ещё не покрытых сторно."""
    accruals = list(
        db.scalars(
            select(PayrollFundLedger)
            .where(
                PayrollFundLedger.source_kind == source_kind,
                PayrollFundLedger.source_id == source_id,
                PayrollFundLedger.entry_kind.in_(_REVERSIBLE_ENTRY_KINDS),
            )
            .order_by(PayrollFundLedger.id.asc())
        ).all()
    )
    for acc in accruals:
        exists = db.scalar(
            select(PayrollFundLedger.id)
            .where(PayrollFundLedger.storno_of_id == acc.id)
            .limit(1)
        )
        if exists is not None:
            continue
        append_ledger(
            db,
            entry_kind=PayrollFundEntryKind.STORNO,
            side=acc.side,
            user_id=acc.user_id,
            amount=-money_q2(acc.amount),
            source_kind=source_kind,
            source_id=source_id,
            created_by_user_id=created_by_user_id,
            storno_of_id=acc.id,
            comment=None,
            effective_at=acc.effective_at,
        )


def _append_visit_service_accruals(
    db: Session,
    visit_service: VisitService,
    visit: Visit,
    created_by_user_id: int | None,
) -> None:
    studio_amt = money_q2(
        float(visit_service.salon_profit or 0) + float(visit_service.studio_fund_amount or 0)
    )
    if studio_amt > 0:
        append_ledger(
            db,
            entry_kind=PayrollFundEntryKind.ACCRUAL,
            side=PayrollFundSide.STUDIO,
            user_id=None,
            amount=studio_amt,
            source_kind=PayrollFundSourceKind.VISIT_SERVICE,
            source_id=visit_service.id,
            created_by_user_id=created_by_user_id,
            effective_at=visit.performed_date,
        )

    append_visit_service_master_pool_and_mix_bonus_ledgers(
        db, visit_service, visit, created_by_user_id
    )


def replace_visit_service_accruals(
    db: Session,
    visit_service: VisitService,
    visit: Visit,
    created_by_user_id: int | None,
) -> None:
    """Сторно проводок строки услуги и повторное начисление по актуальным суммам."""
    storno_source_accruals(db, PayrollFundSourceKind.VISIT_SERVICE, visit_service.id, created_by_user_id)
    if visit.is_cancelled or visit_service.is_cancelled:
        return
    _append_visit_service_accruals(db, visit_service, visit, created_by_user_id)


def replace_visit_accruals(
    db: Session,
    visit: Visit,
    created_by_user_id: int | None,
) -> None:
    """Сторно проводок визита (legacy) и повторное начисление."""
    storno_source_accruals(db, PayrollFundSourceKind.VISIT, visit.id, created_by_user_id)
    if visit.is_cancelled:
        return
    services = list(
        db.scalars(
            select(VisitService)
            .where(VisitService.visit_id == visit.id, VisitService.is_cancelled.is_(False))
            .order_by(VisitService.sort_order.asc(), VisitService.id.asc())
        ).all()
    )
    if services:
        for vs in services:
            replace_visit_service_accruals(db, vs, visit, created_by_user_id)
        replace_visit_hourly_help_accruals(db, visit, created_by_user_id)
        return
    append_visit_master_pool_and_mix_bonus_ledgers(db, visit, created_by_user_id)
    studio_amt = money_q2(float(visit.salon_profit or 0) + float(visit.studio_fund_amount or 0))
    if studio_amt > 0:
        append_ledger(
            db,
            entry_kind=PayrollFundEntryKind.ACCRUAL,
            side=PayrollFundSide.STUDIO,
            user_id=None,
            amount=studio_amt,
            source_kind=PayrollFundSourceKind.VISIT,
            source_id=visit.id,
            created_by_user_id=created_by_user_id,
            effective_at=visit.performed_date,
        )
    replace_visit_hourly_help_accruals(db, visit, created_by_user_id)


def post_visit_service_accruals(
    db: Session,
    visit_service: VisitService,
    visit: Visit,
    created_by_user_id: int | None,
) -> None:
    """Начисления ЗП по одной строке услуги (VISIT_SERVICE)."""
    if visit.is_cancelled or visit_service.is_cancelled:
        return
    if _has_accruals_for_source(db, PayrollFundSourceKind.VISIT_SERVICE, visit_service.id):
        return

    _append_visit_service_accruals(db, visit_service, visit, created_by_user_id)


def append_visit_service_master_pool_and_mix_bonus_ledgers(
    db: Session,
    visit_service: VisitService,
    visit: Visit,
    created_by_user_id: int | None,
) -> None:
    mp = float(visit_service.masters_pool or 0)
    if visit.masters_scope == VisitMastersScope.PER_SERVICE:
        masters = list(
            db.scalars(
                select(VisitServiceMaster)
                .where(VisitServiceMaster.visit_service_id == visit_service.id)
                .order_by(VisitServiceMaster.id.asc())
            ).all()
        )
    else:
        masters = list(
            db.scalars(select(VisitMaster).where(VisitMaster.visit_id == visit.id).order_by(VisitMaster.id.asc())).all()
        )
    for vm in masters:
        pct = float(vm.percent or 0) / 100.0
        amt = money_q2(mp * pct)
        if amt > 0:
            append_ledger(
                db,
                entry_kind=PayrollFundEntryKind.ACCRUAL,
                side=PayrollFundSide.MASTER,
                user_id=int(vm.master_id),
                amount=amt,
                source_kind=PayrollFundSourceKind.VISIT_SERVICE,
                source_id=visit_service.id,
                created_by_user_id=created_by_user_id,
                effective_at=visit.performed_date,
            )

    bonus_mid = visit_service.mix_bonus_master_id
    bonus_amt = money_q2(float(visit_service.mix_bonus_amount or 0))
    if bonus_mid and bonus_amt > 0:
        append_ledger(
            db,
            entry_kind=PayrollFundEntryKind.ACCRUAL,
            side=PayrollFundSide.MASTER,
            user_id=int(bonus_mid),
            amount=bonus_amt,
            source_kind=PayrollFundSourceKind.VISIT_SERVICE,
            source_id=visit_service.id,
            created_by_user_id=created_by_user_id,
            effective_at=visit.performed_date,
        )

    corr_mid = visit_service.correction_master_id
    corr_amt = money_q2(float(visit_service.correction_master_amount or 0))
    if corr_mid and corr_amt > 0:
        append_ledger(
            db,
            entry_kind=PayrollFundEntryKind.ACCRUAL,
            side=PayrollFundSide.MASTER,
            user_id=int(corr_mid),
            amount=corr_amt,
            source_kind=PayrollFundSourceKind.VISIT_SERVICE,
            source_id=visit_service.id,
            created_by_user_id=created_by_user_id,
            effective_at=visit.performed_date,
        )


def post_visit_accruals(db: Session, visit: Visit, created_by_user_id: int | None) -> None:
    if visit.is_cancelled:
        return
    services = list(
        db.scalars(
            select(VisitService)
            .where(VisitService.visit_id == visit.id, VisitService.is_cancelled.is_(False))
            .order_by(VisitService.sort_order.asc(), VisitService.id.asc())
        ).all()
    )
    if services:
        for vs in services:
            post_visit_service_accruals(db, vs, visit, created_by_user_id)
    else:
        if not _has_accruals_for_source(db, PayrollFundSourceKind.VISIT, visit.id):
            studio_amt = money_q2(float(visit.salon_profit or 0) + float(visit.studio_fund_amount or 0))
            if studio_amt > 0:
                append_ledger(
                    db,
                    entry_kind=PayrollFundEntryKind.ACCRUAL,
                    side=PayrollFundSide.STUDIO,
                    user_id=None,
                    amount=studio_amt,
                    source_kind=PayrollFundSourceKind.VISIT,
                    source_id=visit.id,
                    created_by_user_id=created_by_user_id,
                    effective_at=visit.performed_date,
                )

            append_visit_master_pool_and_mix_bonus_ledgers(db, visit, created_by_user_id)

    post_visit_hourly_help_accruals(db, visit, created_by_user_id)


def _has_visit_hourly_help_accruals(db: Session, visit_id: int) -> bool:
    return (
        db.scalar(
            select(PayrollFundLedger.id)
            .where(
                PayrollFundLedger.source_kind == PayrollFundSourceKind.VISIT,
                PayrollFundLedger.source_id == visit_id,
                PayrollFundLedger.comment == HOURLY_HELP_LEDGER_COMMENT,
                PayrollFundLedger.entry_kind == PayrollFundEntryKind.ACCRUAL,
                PayrollFundLedger.storno_of_id.is_(None),
            )
            .limit(1)
        )
        is not None
    )


def append_visit_hourly_help_ledgers(db: Session, visit: Visit, created_by_user_id: int | None) -> None:
    from app.hourly_help import hourly_help_rows_from_visit

    for row in hourly_help_rows_from_visit(visit):
        amt = money_q2(float(row.amount or 0))
        if amt <= 0:
            continue
        append_ledger(
            db,
            entry_kind=PayrollFundEntryKind.ACCRUAL,
            side=PayrollFundSide.MASTER,
            user_id=int(row.master_id),
            amount=amt,
            source_kind=PayrollFundSourceKind.VISIT,
            source_id=visit.id,
            created_by_user_id=created_by_user_id,
            comment=HOURLY_HELP_LEDGER_COMMENT,
            effective_at=visit.performed_date,
        )


def post_visit_hourly_help_accruals(db: Session, visit: Visit, created_by_user_id: int | None) -> None:
    if visit.is_cancelled:
        return
    if _has_visit_hourly_help_accruals(db, int(visit.id)):
        return
    append_visit_hourly_help_ledgers(db, visit, created_by_user_id)


def storno_visit_hourly_help_accruals(
    db: Session,
    visit_id: int,
    created_by_user_id: int | None,
) -> None:
    accruals = list(
        db.scalars(
            select(PayrollFundLedger).where(
                PayrollFundLedger.source_kind == PayrollFundSourceKind.VISIT,
                PayrollFundLedger.source_id == visit_id,
                PayrollFundLedger.comment == HOURLY_HELP_LEDGER_COMMENT,
                PayrollFundLedger.entry_kind == PayrollFundEntryKind.ACCRUAL,
                PayrollFundLedger.storno_of_id.is_(None),
            )
        ).all()
    )
    for acc in accruals:
        already = db.scalar(
            select(PayrollFundLedger.id)
            .where(PayrollFundLedger.storno_of_id == acc.id)
            .limit(1)
        )
        if already is not None:
            continue
        append_ledger(
            db,
            entry_kind=PayrollFundEntryKind.STORNO,
            side=acc.side,
            user_id=acc.user_id,
            amount=-money_q2(float(acc.amount or 0)),
            source_kind=acc.source_kind,
            source_id=acc.source_id,
            created_by_user_id=created_by_user_id,
            storno_of_id=acc.id,
            comment=HOURLY_HELP_LEDGER_COMMENT,
            effective_at=acc.effective_at,
        )


def replace_visit_hourly_help_accruals(
    db: Session,
    visit: Visit,
    created_by_user_id: int | None,
) -> None:
    storno_visit_hourly_help_accruals(db, int(visit.id), created_by_user_id)
    if visit.is_cancelled:
        return
    append_visit_hourly_help_ledgers(db, visit, created_by_user_id)


def append_visit_master_pool_and_mix_bonus_ledgers(
    db: Session, visit: Visit, created_by_user_id: int | None
) -> None:
    """Начисления мастерам с визита: доля от masters_pool + бонус за смешку (без проводки студии)."""
    mp = float(visit.masters_pool or 0)
    masters = list(
        db.scalars(select(VisitMaster).where(VisitMaster.visit_id == visit.id).order_by(VisitMaster.id.asc())).all()
    )
    for vm in masters:
        pct = float(vm.percent or 0) / 100.0
        amt = money_q2(mp * pct)
        if amt > 0:
            append_ledger(
                db,
                entry_kind=PayrollFundEntryKind.ACCRUAL,
                side=PayrollFundSide.MASTER,
                user_id=int(vm.master_id),
                amount=amt,
                source_kind=PayrollFundSourceKind.VISIT,
                source_id=visit.id,
                created_by_user_id=created_by_user_id,
                effective_at=visit.performed_date,
            )

    bonus_mid = visit.mix_bonus_master_id
    bonus_amt = money_q2(float(visit.mix_bonus_amount or 0))
    if bonus_mid and bonus_amt > 0:
        append_ledger(
            db,
            entry_kind=PayrollFundEntryKind.ACCRUAL,
            side=PayrollFundSide.MASTER,
            user_id=int(bonus_mid),
            amount=bonus_amt,
            source_kind=PayrollFundSourceKind.VISIT,
            source_id=visit.id,
            created_by_user_id=created_by_user_id,
            effective_at=visit.performed_date,
        )


def _has_master_side_accrual_for_visit(db: Session, visit_id: int) -> bool:
    return (
        db.scalar(
            select(PayrollFundLedger.id)
            .where(
                PayrollFundLedger.source_kind == PayrollFundSourceKind.VISIT,
                PayrollFundLedger.source_id == visit_id,
                PayrollFundLedger.entry_kind == PayrollFundEntryKind.ACCRUAL,
                PayrollFundLedger.side == PayrollFundSide.MASTER,
            )
            .limit(1)
        )
        is not None
    )


def backfill_visit_master_accruals_if_missing(db: Session, visit: Visit, created_by_user_id: int | None) -> None:
    """
    Восстановление после бага: студия уже в журнале, а строки мастеров не создались
    (VisitMaster не были flush до первого post_visit_accruals).
    """
    if visit.is_cancelled:
        return
    if not _has_accruals_for_source(db, PayrollFundSourceKind.VISIT, visit.id):
        return
    if _has_master_side_accrual_for_visit(db, visit.id):
        return
    mp = float(visit.masters_pool or 0)
    bonus_amt = money_q2(float(visit.mix_bonus_amount or 0))
    masters = list(
        db.scalars(select(VisitMaster).where(VisitMaster.visit_id == visit.id).order_by(VisitMaster.id.asc())).all()
    )
    if mp <= 0 and bonus_amt <= 0:
        return
    if mp > 0 and not masters:
        return
    append_visit_master_pool_and_mix_bonus_ledgers(db, visit, created_by_user_id)


def backfill_all_visit_master_accruals_if_missing(db: Session) -> None:
    """Проход по всем визитам: восстановить начисления мастерам после частичного первого прохода."""
    for visit in db.scalars(select(Visit).where(Visit.is_cancelled.is_(False))).all():
        backfill_visit_master_accruals_if_missing(db, visit, visit.created_by_user_id)


def post_work_accruals(
    db: Session,
    work_id: int,
    staff_rows: Sequence[WorkForInventoryStaff],
    created_by_user_id: int | None,
) -> None:
    if _has_accruals_for_source(db, PayrollFundSourceKind.WORK, work_id):
        return
    _append_work_accruals(db, work_id, staff_rows, created_by_user_id)


def replace_work_accruals(
    db: Session,
    work_id: int,
    staff_rows: Sequence[WorkForInventoryStaff],
    created_by_user_id: int | None,
) -> None:
    storno_source_accruals(db, PayrollFundSourceKind.WORK, work_id, created_by_user_id)
    _append_work_accruals(db, work_id, staff_rows, created_by_user_id)


def _append_work_accruals(
    db: Session,
    work_id: int,
    staff_rows: Sequence[WorkForInventoryStaff],
    created_by_user_id: int | None,
) -> None:
    w = db.get(WorkForInventory, int(work_id))
    _work_eff = work_event_at(w) if w else utcnow_naive()
    total_staff = 0.0
    for s in staff_rows:
        amt = money_q2(float(s.master_profit_amount or 0))
        if amt > 0:
            total_staff = money_q2(total_staff + amt)
            append_ledger(
                db,
                entry_kind=PayrollFundEntryKind.ACCRUAL,
                side=PayrollFundSide.MASTER,
                user_id=int(s.user_id),
                amount=amt,
                source_kind=PayrollFundSourceKind.WORK,
                source_id=work_id,
                created_by_user_id=created_by_user_id,
                effective_at=_work_eff,
            )
    if w and w.scope == WorkScope.CUSTOM_ORDER:
        studio_amt = money_q2(float(w.studio_profit_amount or 0))
        if studio_amt != 0:
            append_ledger(
                db,
                entry_kind=PayrollFundEntryKind.ACCRUAL,
                side=PayrollFundSide.STUDIO,
                user_id=None,
                amount=studio_amt,
                source_kind=PayrollFundSourceKind.WORK,
                source_id=work_id,
                created_by_user_id=created_by_user_id,
                effective_at=_work_eff,
            )
    # Для работ «в наличие» начисление мастерам должно идти из фонда студии,
    # чтобы не появлялись «деньги из воздуха» до продажи.
    elif total_staff > 0 and w and w.scope == WorkScope.IN_STOCK:
        append_ledger(
            db,
            entry_kind=PayrollFundEntryKind.EXPENSE,
            side=PayrollFundSide.STUDIO,
            user_id=None,
            amount=-money_q2(total_staff),
            source_kind=PayrollFundSourceKind.WORK,
            source_id=work_id,
            created_by_user_id=created_by_user_id,
            comment="Оплата мастерам (работа в наличие)",
            effective_at=_work_eff,
        )


def _net_work_ledger_sum(db: Session, work_id: int, side: PayrollFundSide) -> float:
    v = db.scalar(
        select(func.coalesce(func.sum(PayrollFundLedger.amount), 0.0)).where(
            PayrollFundLedger.source_kind == PayrollFundSourceKind.WORK,
            PayrollFundLedger.source_id == int(work_id),
            PayrollFundLedger.side == side,
            PayrollFundLedger.entry_kind.in_(_REVERSIBLE_ENTRY_KINDS),
        )
    )
    return money_q2(float(v or 0.0))


def backfill_work_accruals_if_missing(db: Session, work: WorkForInventory) -> None:
    """Восстановить проводки работы, если в карточке есть ЗП, а в журнале — нет."""
    if work.is_voided or work.id is None:
        return
    staff = list(
        db.scalars(
            select(WorkForInventoryStaff).where(WorkForInventoryStaff.work_id == int(work.id))
        ).all()
    )
    exp_master = money_q2(sum(float(s.master_profit_amount or 0) for s in staff))
    exp_studio = (
        money_q2(float(work.studio_profit_amount or 0))
        if work.scope == WorkScope.CUSTOM_ORDER
        else 0.0
    )
    net_master = _net_work_ledger_sum(db, int(work.id), PayrollFundSide.MASTER)
    net_studio = _net_work_ledger_sum(db, int(work.id), PayrollFundSide.STUDIO)
    needs = False
    if exp_master > 0 and abs(net_master - exp_master) > 0.02:
        needs = True
    if abs(exp_studio) > 0.001 and abs(net_studio - exp_studio) > 0.02:
        needs = True
    if needs:
        replace_work_accruals(db, int(work.id), staff, work.created_by_user_id)


def sum_work_master_payroll_by_work_id(
    db: Session,
    *,
    work_ids: list[int],
    user_id: int | None = None,
    backfill: bool = True,
) -> dict[int, float]:
    if not work_ids:
        return {}
    out = sum_ledger_amounts_by_source(
        db,
        side=PayrollFundSide.MASTER,
        source_kind=PayrollFundSourceKind.WORK,
        source_ids=work_ids,
        user_id=user_id,
    )
    missing = [wid for wid in work_ids if abs(out.get(int(wid), 0.0)) < 0.001]
    if not missing:
        return out
    works = list(
        db.scalars(
            select(WorkForInventory)
            .options(selectinload(WorkForInventory.staff_rows))
            .where(WorkForInventory.id.in_(missing))
        ).all()
    )
    for w in works:
        wid = int(w.id)
        if user_id is not None:
            amt = money_q2(
                sum(
                    float(s.master_profit_amount or 0)
                    for s in w.staff_rows
                    if int(s.user_id) == int(user_id)
                )
            )
        else:
            amt = money_q2(sum(float(s.master_profit_amount or 0) for s in w.staff_rows))
        if amt > 0:
            out[wid] = amt
            if backfill:
                backfill_work_accruals_if_missing(db, w)
    return out


def sum_work_studio_payroll_by_work_id(
    db: Session,
    *,
    work_ids: list[int],
    backfill: bool = True,
) -> dict[int, float]:
    if not work_ids:
        return {}
    out = sum_ledger_amounts_by_source(
        db,
        side=PayrollFundSide.STUDIO,
        source_kind=PayrollFundSourceKind.WORK,
        source_ids=work_ids,
        user_id=None,
    )
    missing = [wid for wid in work_ids if abs(out.get(int(wid), 0.0)) < 0.001]
    if not missing:
        return out
    works = list(
        db.scalars(select(WorkForInventory).where(WorkForInventory.id.in_(missing))).all()
    )
    for w in works:
        if w.scope != WorkScope.CUSTOM_ORDER:
            continue
        amt = money_q2(float(w.studio_profit_amount or 0))
        if abs(amt) > 0.001:
            out[int(w.id)] = amt
            if backfill:
                backfill_work_accruals_if_missing(db, w)
    return out


def _product_sale_kit_line_price_deduction(
    db: Session,
    *,
    kit: Kit,
    pieces_sold: int,
    breakdown: dict[str, int] | None,
) -> float:
    """Доля цены комплекта (как при списании с визита: по ключам или пропорционально), после скидки."""
    raw_price = kit.stock_price_total
    if raw_price is None or float(raw_price) <= 0:
        return 0.0
    n = int(pieces_sold)
    if n <= 0:
        return 0.0
    if kit_inventory_is_keyed(db, int(kit.id)) and breakdown:
        price_map, meta_by_key, _labels = load_catalog_kit_maps(db)
        comp = parse_composition_totals(kit)
        selected_price = keyed_client_price_selected(
            breakdown, price_map=price_map, meta_by_key=meta_by_key
        )
        selected_cost = keyed_cost_selected(
            breakdown, comp=comp, kit_cost_total=max(0.0, float(kit.cost_total or 0.0))
        )
        _disc, net = apply_discount_capped(
            selected_price,
            discount_percent=int(kit.discount_percent or 0),
            cost_floor=selected_cost,
        )
        return float(net)
    price = float(raw_price)
    total_pieces = max(int(kit.pieces_total or 0), 1)
    kit_cost_full = max(0.0, float(kit.cost_total or 0.0))
    max_disc_margin = max(0.0, price - kit_cost_full)
    pct = max(0, min(100, int(kit.discount_percent or 0)))
    discount_full = price * (pct / 100.0)
    discount_full = min(discount_full, max_disc_margin, price)
    net_full = max(0.0, price - discount_full)
    k = float(n) / float(total_pieces)
    return float(net_full * k)


def compute_product_sale_studio_margin(db: Session, sale: ProductSale) -> float:
    amt = float(sale.amount_from_client or 0)
    kind = sale.kind
    if kind == ProductSaleKind.KIT:
        from app.product_sales import _sale_kit_line_tuples_from_sale

        lines = _sale_kit_line_tuples_from_sale(sale)
        if not lines:
            return money_q2(max(0.0, amt))
        total_deduction = 0.0
        for kid, ps, bd in lines:
            kit = db.get(Kit, int(kid))
            if not kit:
                continue
            total_deduction += _product_sale_kit_line_price_deduction(
                db, kit=kit, pieces_sold=int(ps), breakdown=bd
            )
        return money_q2(max(0.0, amt - total_deduction))
    if kind == ProductSaleKind.MATERIAL:
        # Маржа материала выставляется в finalize_material_sale_fields (роутер / sync).
        return money_q2(float(sale.studio_margin_amount or 0))
    if kind == ProductSaleKind.RUBBER:
        cost = float(sale.rubber_price_override or 0)
        return money_q2(max(0.0, amt - cost))
    if kind == ProductSaleKind.OTHER:
        cost = float(getattr(sale, "other_cost", None) or 0)
        return money_q2(max(0.0, amt - cost))
    return money_q2(max(0.0, amt))


def _append_product_sale_ledger_rows(
    db: Session,
    sale: ProductSale,
    created_by_user_id: int | None,
) -> None:
    margin = money_q2(float(sale.studio_margin_amount or 0))
    if margin > 0:
        append_ledger(
            db,
            entry_kind=PayrollFundEntryKind.ACCRUAL,
            side=PayrollFundSide.STUDIO,
            user_id=None,
            amount=margin,
            source_kind=PayrollFundSourceKind.PRODUCT_SALE,
            source_id=sale.id,
            created_by_user_id=created_by_user_id,
            effective_at=sale.performed_date,
        )
    bonus_uid = sale.material_mix_bonus_user_id
    bonus_amt = money_q2(float(sale.material_mix_bonus_amount or 0))
    if bonus_uid and bonus_amt > 0:
        append_ledger(
            db,
            entry_kind=PayrollFundEntryKind.ACCRUAL,
            side=PayrollFundSide.MASTER,
            user_id=int(bonus_uid),
            amount=bonus_amt,
            source_kind=PayrollFundSourceKind.PRODUCT_SALE,
            source_id=sale.id,
            created_by_user_id=created_by_user_id,
            effective_at=sale.performed_date,
        )


def post_product_sale_studio_accrual(
    db: Session,
    sale: ProductSale,
    created_by_user_id: int | None,
) -> None:
    if sale.is_voided:
        return
    if _has_accruals_for_source(db, PayrollFundSourceKind.PRODUCT_SALE, sale.id):
        return
    _append_product_sale_ledger_rows(db, sale, created_by_user_id)


def replace_product_sale_studio_accrual(
    db: Session,
    sale: ProductSale,
    created_by_user_id: int | None,
) -> None:
    storno_source_accruals(db, PayrollFundSourceKind.PRODUCT_SALE, sale.id, created_by_user_id)
    if sale.is_voided:
        return
    _append_product_sale_ledger_rows(db, sale, created_by_user_id)


def post_payout(
    db: Session,
    *,
    side: PayrollFundSide,
    user_id: int | None,
    amount: float,
    created_by_user_id: int,
    comment: str | None,
    payout_payment_kind: PayrollFundPayoutPaymentKind = PayrollFundPayoutPaymentKind.UNSPECIFIED,
    effective_at: datetime | None = None,
) -> None:
    """Выплата из фонда: положительная сумма уменьшает сальдо фонда (отрицательная запись в журнале).

    Отрицательная сумма вводимая в форме даёт положительную проводку (возврат в фонд).
    effective_at — учётная дата выплаты (дата события); по умолчанию момент записи.
    """
    pay = money_q2(amount)
    if pay == 0:
        raise ValueError("Сумма не может быть нулевой")
    append_ledger(
        db,
        entry_kind=PayrollFundEntryKind.PAYOUT,
        side=side,
        user_id=user_id,
        amount=-pay,
        source_kind=PayrollFundSourceKind.MANUAL,
        source_id=None,
        created_by_user_id=created_by_user_id,
        comment=(comment or "").strip() or None,
        payout_payment_kind=payout_payment_kind,
        effective_at=effective_at,
    )


def post_manual_adjustment(
    db: Session,
    *,
    side: PayrollFundSide,
    user_id: int | None,
    amount_delta: float,
    created_by_user_id: int,
    comment: str | None,
) -> None:
    """Manual adjustment (including initial balances).

    Uses source_kind=MANUAL and entry_kind=ACCRUAL. The delta may be negative.
    """
    amt = money_q2(float(amount_delta))
    if amt == 0:
        raise ValueError("Сумма не может быть нулевой")
    append_ledger(
        db,
        entry_kind=PayrollFundEntryKind.ACCRUAL,
        side=side,
        user_id=user_id,
        amount=amt,
        source_kind=PayrollFundSourceKind.MANUAL,
        source_id=None,
        created_by_user_id=created_by_user_id,
        comment=(comment or "").strip() or None,
    )


def current_fund_balance(db: Session, *, side: PayrollFundSide, user_id: int | None) -> float:
    if side == PayrollFundSide.STUDIO:
        return studio_fund_balance(db)
    if user_id is None:
        raise ValueError("MASTER требует user_id")
    return employee_fund_balance(db, int(user_id))

def ledger_balances(db: Session) -> tuple[list[dict], float]:
    """Сальдо: мастера списком + студия одной суммой."""
    stmt = (
        select(
            PayrollFundLedger.side,
            PayrollFundLedger.user_id,
            func.coalesce(func.sum(PayrollFundLedger.amount), 0.0),
        )
        .group_by(PayrollFundLedger.side, PayrollFundLedger.user_id)
    )
    rows = list(db.execute(stmt).all())
    masters: list[dict] = []
    studio_total = 0.0
    for side, uid, total in rows:
        t = money_q2(float(total or 0))
        if side == PayrollFundSide.STUDIO:
            studio_total = money_q2(studio_total + t)
        else:
            if uid is not None:
                masters.append({"user_id": int(uid), "balance": t})
    masters.sort(key=lambda x: x["user_id"])
    return masters, studio_total


def studio_fund_balance(db: Session) -> float:
    """Остаток фонда студии (все проводки со стороны STUDIO)."""
    v = db.scalar(
        select(func.coalesce(func.sum(PayrollFundLedger.amount), 0.0)).where(
            PayrollFundLedger.side == PayrollFundSide.STUDIO
        )
    )
    return money_q2(float(v or 0))


def employee_fund_balance(db: Session, user_id: int) -> float:
    """Сальдо личного фонда сотрудника (сторона MASTER, все проводки)."""
    v = db.scalar(
        select(func.coalesce(func.sum(PayrollFundLedger.amount), 0.0)).where(
            PayrollFundLedger.side == PayrollFundSide.MASTER,
            PayrollFundLedger.user_id == user_id,
        )
    )
    return money_q2(float(v or 0))


def employee_payout_total_net(db: Session, user_id: int) -> float:
    """Нетто выплат сотруднику: минус сумма amount по PAYOUT с user_id этого сотрудника."""
    v = db.scalar(
        select(func.coalesce(func.sum(PayrollFundLedger.amount), 0.0)).where(
            PayrollFundLedger.entry_kind == PayrollFundEntryKind.PAYOUT,
            PayrollFundLedger.user_id == user_id,
        )
    )
    return money_q2(-float(v or 0))


def resolve_current_payroll_period(
    db: Session, today: date
) -> tuple[PayrollPeriod | None, datetime, datetime]:
    """Текущий (открытый или покрывающий сегодня) период и границы [start, end_excl) для журнала."""
    from app.payroll_utils import payroll_period_day_end

    p = db.scalar(
        select(PayrollPeriod)
        .where(PayrollPeriod.closed_at.is_(None))
        .order_by(PayrollPeriod.date_from.desc(), PayrollPeriod.id.desc())
        .limit(1)
    )
    if p is None:
        p = db.scalar(
            select(PayrollPeriod)
            .where(
                PayrollPeriod.date_from <= datetime.combine(today, time.max),
                PayrollPeriod.date_to >= datetime.combine(today, time.min),
            )
            .order_by(PayrollPeriod.date_from.desc(), PayrollPeriod.id.desc())
            .limit(1)
        )
    if p is None:
        p = db.scalar(select(PayrollPeriod).order_by(PayrollPeriod.date_from.desc(), PayrollPeriod.id.desc()).limit(1))
    if p is None:
        return None, datetime.combine(today, time.min), datetime.combine(today + timedelta(days=1), time.min)

    period_end = p.date_to
    if p.closed_at is None:
        period_end = payroll_period_day_end(today)
    start = datetime.combine(p.date_from.date(), time.min) if isinstance(p.date_from, datetime) else p.date_from
    end_day = period_end.date() if isinstance(period_end, datetime) else period_end
    end_excl = datetime.combine(end_day + timedelta(days=1), time.min)
    return p, start, end_excl


def employee_payroll_net_in_period(
    db: Session,
    user_id: int,
    start: datetime,
    end_excl: datetime,
) -> float:
    """Нетто начислений сотруднику за период (ACCRUAL + STORNO)."""
    v = db.scalar(
        select(func.coalesce(func.sum(PayrollFundLedger.amount), 0.0)).where(
            PayrollFundLedger.side == PayrollFundSide.MASTER,
            PayrollFundLedger.user_id == user_id,
            PayrollFundLedger.effective_at >= start,
            PayrollFundLedger.effective_at < end_excl,
            PayrollFundLedger.entry_kind.in_(
                (PayrollFundEntryKind.ACCRUAL, PayrollFundEntryKind.STORNO)
            ),
        )
    )
    return money_q2(float(v or 0))


def employee_payouts_in_period(
    db: Session,
    user_id: int,
    start: datetime,
    end_excl: datetime,
) -> float:
    """Сумма выплат сотруднику за период (положительное число)."""
    v = db.scalar(
        select(func.coalesce(func.sum(PayrollFundLedger.amount), 0.0)).where(
            PayrollFundLedger.entry_kind == PayrollFundEntryKind.PAYOUT,
            PayrollFundLedger.user_id == user_id,
            PayrollFundLedger.effective_at >= start,
            PayrollFundLedger.effective_at < end_excl,
        )
    )
    return money_q2(-float(v or 0))


def studio_payroll_net_in_period(db: Session, start: datetime, end_excl: datetime) -> float:
    """Нетто начислений в фонд студии за период (ACCRUAL + STORNO)."""
    v = db.scalar(
        select(func.coalesce(func.sum(PayrollFundLedger.amount), 0.0)).where(
            PayrollFundLedger.side == PayrollFundSide.STUDIO,
            PayrollFundLedger.effective_at >= start,
            PayrollFundLedger.effective_at < end_excl,
            PayrollFundLedger.entry_kind.in_(
                (PayrollFundEntryKind.ACCRUAL, PayrollFundEntryKind.STORNO)
            ),
        )
    )
    return money_q2(float(v or 0))


def studio_payouts_in_period(db: Session, start: datetime, end_excl: datetime) -> float:
    """Сумма выплат из фонда студии за период (положительное число)."""
    v = db.scalar(
        select(func.coalesce(func.sum(PayrollFundLedger.amount), 0.0)).where(
            PayrollFundLedger.side == PayrollFundSide.STUDIO,
            PayrollFundLedger.entry_kind == PayrollFundEntryKind.PAYOUT,
            PayrollFundLedger.effective_at >= start,
            PayrollFundLedger.effective_at < end_excl,
        )
    )
    return money_q2(-float(v or 0))


def studio_fund_net_in_period(db: Session, start: datetime, end_excl: datetime) -> float:
    """Изменение остатка фонда студии за период (все виды проводок)."""
    v = db.scalar(
        select(func.coalesce(func.sum(PayrollFundLedger.amount), 0.0)).where(
            PayrollFundLedger.side == PayrollFundSide.STUDIO,
            PayrollFundLedger.effective_at >= start,
            PayrollFundLedger.effective_at < end_excl,
        )
    )
    return money_q2(float(v or 0))


def build_home_payroll_period_ctx(
    db: Session,
    *,
    today: date,
    user_id: int,
    include_studio: bool,
) -> dict[str, Any] | None:
    """Данные текущего периода ЗП для сводки на главной."""
    p, start, end_excl = resolve_current_payroll_period(db, today)
    if p is None:
        return None

    personal_accrued = employee_payroll_net_in_period(db, user_id, start, end_excl)
    personal_paid = employee_payouts_in_period(db, user_id, start, end_excl)
    personal_balance = money_q2(personal_accrued - personal_paid)

    period_end = end_excl - timedelta(microseconds=1)
    out: dict[str, Any] = {
        "id": int(p.id),
        "date_from": p.date_from,
        "date_to": period_end,
        "personal_accrued": personal_accrued,
        "personal_paid": personal_paid,
        "personal_balance": personal_balance,
    }

    if include_studio:
        studio_accrued = studio_payroll_net_in_period(db, start, end_excl)
        studio_paid = studio_payouts_in_period(db, start, end_excl)
        studio_balance = studio_fund_net_in_period(db, start, end_excl)
        expenses_sum = (
            db.scalar(
                select(func.coalesce(func.sum(StudioExpense.amount), 0.0)).where(
                    StudioExpense.is_voided.is_(False),
                    StudioExpense.date >= start,
                    StudioExpense.date <= period_end,
                )
            )
            or 0.0
        )
        out.update(
            {
                "studio_accrued": studio_accrued,
                "studio_paid": studio_paid,
                "studio_balance": studio_balance,
                "expenses_sum": money_q2(float(expenses_sum)),
            }
        )
    return out


def replace_studio_expense_ledger(
    db: Session,
    expense: StudioExpense,
    created_by_user_id: int | None,
) -> None:
    storno_source_accruals(
        db, PayrollFundSourceKind.STUDIO_EXPENSE, expense.id, created_by_user_id
    )
    if expense.is_voided:
        return
    amt = money_q2(float(expense.amount or 0))
    if amt <= 0:
        return
    append_ledger(
        db,
        entry_kind=PayrollFundEntryKind.EXPENSE,
        side=PayrollFundSide.STUDIO,
        user_id=None,
        amount=-amt,
        source_kind=PayrollFundSourceKind.STUDIO_EXPENSE,
        source_id=expense.id,
        created_by_user_id=created_by_user_id,
        comment=None,
        effective_at=expense.date,
    )


def has_unreversed_studio_expense_posting(db: Session, expense_id: int) -> bool:
    """True, если по расходу есть проводка EXPENSE, ещё не закрытая сторно."""
    rows = list(
        db.scalars(
            select(PayrollFundLedger)
            .where(
                PayrollFundLedger.source_kind == PayrollFundSourceKind.STUDIO_EXPENSE,
                PayrollFundLedger.source_id == expense_id,
                PayrollFundLedger.entry_kind == PayrollFundEntryKind.EXPENSE,
            )
            .order_by(PayrollFundLedger.id.asc())
        ).all()
    )
    for r in rows:
        rev = db.scalar(
            select(PayrollFundLedger.id)
            .where(PayrollFundLedger.storno_of_id == r.id)
            .limit(1)
        )
        if rev is None:
            return True
    return False


def sync_operational_payroll_postings(db: Session) -> None:
    """
    Идемпотентно создаёт недостающие проводки по операционным сущностям (сиды, восстановление БД).
    Не дублирует уже учтённые визиты/продажи/работы; расходы — только если нет активной EXPENSE-проводки.
    """
    for visit in db.scalars(select(Visit).where(Visit.is_cancelled.is_(False))).all():
        post_visit_accruals(db, visit, visit.created_by_user_id)
        backfill_visit_master_accruals_if_missing(db, visit, visit.created_by_user_id)

    for sale in db.scalars(select(ProductSale).where(ProductSale.is_voided.is_(False))).all():
        if not _has_accruals_for_source(db, PayrollFundSourceKind.PRODUCT_SALE, sale.id):
            if sale.kind == ProductSaleKind.KIT and sale.kit_id:
                db.refresh(sale, attribute_names=["kit"])
            elif sale.kind == ProductSaleKind.MATERIAL:
                db.refresh(sale, attribute_names=["material_service"])
                from app.product_sale_material import finalize_material_sale_fields

                creator = db.get(User, sale.created_by_user_id)
                finalize_material_sale_fields(
                    db,
                    sale,
                    seller_user_id=int(sale.created_by_user_id),
                    active_role=creator.role if creator else UserRole.MASTER,
                )
            sale.studio_margin_amount = compute_product_sale_studio_margin(db, sale)
            post_product_sale_studio_accrual(db, sale, sale.created_by_user_id)

    for work in db.scalars(select(WorkForInventory).where(WorkForInventory.is_voided.is_(False))).all():
        staff = list(
            db.scalars(
                select(WorkForInventoryStaff).where(WorkForInventoryStaff.work_id == work.id)
            ).all()
        )
        post_work_accruals(db, work.id, staff, work.created_by_user_id)
        backfill_work_accruals_if_missing(db, work)

    for exp in db.scalars(select(StudioExpense).where(StudioExpense.is_voided.is_(False))).all():
        if not has_unreversed_studio_expense_posting(db, exp.id):
            replace_studio_expense_ledger(db, exp, exp.created_by_user_id)


def _work_rate_int(db: Session, key: str, default: int) -> int:
    r = db.scalar(select(WorkRate).where(WorkRate.key == key, WorkRate.is_active.is_(True)).limit(1))
    if not r:
        return default
    try:
        v = json.loads(r.value_json)
        return int(v)
    except Exception:
        return default


def consultation_pay_settings(db: Session) -> tuple[int, int, int]:
    """(ниже порога, от порога, порог суммы) в рублях."""
    below = _work_rate_int(db, CONSULTATION_PAY_BELOW_THRESHOLD, 200)
    above = _work_rate_int(db, CONSULTATION_PAY_AT_OR_ABOVE_THRESHOLD, 300)
    threshold = _work_rate_int(db, CONSULTATION_PAY_AMOUNT_THRESHOLD, 5000)
    return below, above, threshold


def _booking_has_fulfilled_visit_or_sale(db: Session, booking: Booking) -> bool:
    if booking.kind == BookingKind.VISIT:
        vid = db.scalar(
            select(Visit.id).where(
                Visit.booking_id == booking.id,
                Visit.is_cancelled.is_(False),
            ).limit(1)
        )
        return vid is not None
    if booking.kind == BookingKind.PRODUCT_SALE:
        sid = db.scalar(
            select(ProductSale.id).where(
                ProductSale.booking_id == booking.id,
                ProductSale.is_voided.is_(False),
            ).limit(1)
        )
        return sid is not None
    return False


def sum_booking_fulfillment_amount(db: Session, booking_id: int) -> int:
    """Сумма amount_from_client: работы по брони + визит или продажа."""
    b = db.get(Booking, booking_id)
    if not b or b.status != BookingStatus.DONE:
        return 0
    total = 0
    for w in db.scalars(
        select(WorkForInventory).where(
            WorkForInventory.booking_id == booking_id,
            WorkForInventory.is_voided.is_(False),
        )
    ).all():
        total += int(w.amount_from_client or 0)
    if b.kind == BookingKind.VISIT:
        v_amt = db.scalar(
            select(Visit.amount_from_client).where(
                Visit.booking_id == booking_id,
                Visit.is_cancelled.is_(False),
            ).limit(1)
        )
        if v_amt is not None:
            total += int(v_amt)
    elif b.kind == BookingKind.PRODUCT_SALE:
        s_amt = db.scalar(
            select(ProductSale.amount_from_client).where(
                ProductSale.booking_id == booking_id,
                ProductSale.is_voided.is_(False),
            ).limit(1)
        )
        if s_amt is not None:
            total += int(s_amt)
    return total


def post_consultation_accrual(
    db: Session,
    consultation_id: int,
    created_by_user_id: int | None,
) -> None:
    """ЗП мастеру-консультанту после выполненной брони с визитом/продажей."""
    if _has_accruals_for_source(db, PayrollFundSourceKind.CONSULTATION, consultation_id):
        return
    cons = db.get(Consultation, consultation_id)
    if not cons:
        return
    b = db.scalar(select(Booking).where(Booking.consultation_id == consultation_id).limit(1))
    if not b or b.status != BookingStatus.DONE:
        return
    if not _booking_has_fulfilled_visit_or_sale(db, b):
        return
    below, above, threshold = consultation_pay_settings(db)
    base = sum_booking_fulfillment_amount(db, b.id)
    pay = float(above if base >= threshold else below)
    eff = consultation_fulfillment_event_at(db, b) or utcnow_naive()
    append_ledger(
        db,
        entry_kind=PayrollFundEntryKind.ACCRUAL,
        side=PayrollFundSide.MASTER,
        user_id=int(cons.created_by_user_id),
        amount=pay,
        source_kind=PayrollFundSourceKind.CONSULTATION,
        source_id=consultation_id,
        created_by_user_id=created_by_user_id,
        comment=f"Консультация #{consultation_id}, бронь #{b.id}, база {base} ₽",
        effective_at=eff,
    )


def post_hourly_work_accruals(
    db: Session,
    entry: HourlyWorkEntry,
    created_by_user_id: int | None,
) -> None:
    """ЗП мастеру за почасовую работу; сумма списывается с фонда студии."""
    if entry.id is None:
        return
    if _has_accruals_for_source(db, PayrollFundSourceKind.HOURLY_WORK, int(entry.id)):
        return
    _append_hourly_work_accruals(db, entry, created_by_user_id)


def _append_hourly_work_accruals(
    db: Session,
    entry: HourlyWorkEntry,
    created_by_user_id: int | None,
) -> None:
    amt = money_q2(float(entry.amount or 0))
    if amt <= 0:
        return
    append_ledger(
        db,
        entry_kind=PayrollFundEntryKind.ACCRUAL,
        side=PayrollFundSide.MASTER,
        user_id=int(entry.master_user_id),
        amount=amt,
        source_kind=PayrollFundSourceKind.HOURLY_WORK,
        source_id=int(entry.id),
        created_by_user_id=created_by_user_id,
        comment="Почасовая работа",
        effective_at=entry.performed_date,
    )
    append_ledger(
        db,
        entry_kind=PayrollFundEntryKind.EXPENSE,
        side=PayrollFundSide.STUDIO,
        user_id=None,
        amount=-amt,
        source_kind=PayrollFundSourceKind.HOURLY_WORK,
        source_id=int(entry.id),
        created_by_user_id=created_by_user_id,
        comment="Почасовая работа",
        effective_at=entry.performed_date,
    )


def replace_hourly_work_accruals(
    db: Session,
    entry: HourlyWorkEntry,
    created_by_user_id: int | None,
) -> None:
    """Сторно прежних проводок почасовой работы и новое начисление."""
    if entry.id is None:
        return
    storno_source_accruals(db, PayrollFundSourceKind.HOURLY_WORK, int(entry.id), created_by_user_id)
    _append_hourly_work_accruals(db, entry, created_by_user_id)


LEDGER_JOURNAL_DEFAULT_LIMIT = 150
LEDGER_JOURNAL_SEARCH_LIMIT = 500

PAYROLL_FUND_SOURCE_KIND_RU: dict[PayrollFundSourceKind, str] = {
    PayrollFundSourceKind.VISIT: "Визит",
    PayrollFundSourceKind.VISIT_SERVICE: "Услуга визита",
    PayrollFundSourceKind.WORK: "Работа",
    PayrollFundSourceKind.PRODUCT_SALE: "Продажа",
    PayrollFundSourceKind.CONSULTATION: "Консультация",
    PayrollFundSourceKind.HOURLY_WORK: "Почасовая работа",
    PayrollFundSourceKind.STUDIO_EXPENSE: "Расход студии",
    PayrollFundSourceKind.MANUAL: "Ручная",
}


def search_ledger_rows(
    db: Session,
    *,
    effective_from: date | None = None,
    effective_to: date | None = None,
    user_id: int | None = None,
    studio_only: bool = False,
    source_kind: PayrollFundSourceKind | None = None,
    limit: int | None = None,
) -> list[PayrollFundLedger]:
    """Журнал фонда: без фильтров — последние записи; с фильтрами — по дате учёта.

    studio_only: проводки без сотрудника (колонка «Кому» = —), обычно фонд студии.
    """
    has_filter = bool(
        effective_from is not None
        or effective_to is not None
        or user_id is not None
        or studio_only
        or source_kind is not None
    )
    if limit is None:
        limit = LEDGER_JOURNAL_SEARCH_LIMIT if has_filter else LEDGER_JOURNAL_DEFAULT_LIMIT

    q = select(PayrollFundLedger).options(
        selectinload(PayrollFundLedger.created_by_user),
        selectinload(PayrollFundLedger.user),
    )
    if effective_from is not None:
        q = q.where(PayrollFundLedger.effective_at >= datetime.combine(effective_from, time.min))
    if effective_to is not None:
        q = q.where(
            PayrollFundLedger.effective_at < datetime.combine(effective_to + timedelta(days=1), time.min)
        )
    if studio_only:
        q = q.where(PayrollFundLedger.user_id.is_(None))
    elif user_id is not None:
        q = q.where(PayrollFundLedger.user_id == int(user_id))
    if source_kind is not None:
        q = q.where(PayrollFundLedger.source_kind == source_kind)

    if has_filter:
        q = q.order_by(PayrollFundLedger.effective_at.desc(), PayrollFundLedger.id.desc())
    else:
        q = q.order_by(PayrollFundLedger.id.desc())

    return list(db.scalars(q.limit(limit)).all())


def recent_ledger_rows(db: Session, limit: int = LEDGER_JOURNAL_DEFAULT_LIMIT) -> list[PayrollFundLedger]:
    return search_ledger_rows(db, limit=limit)
