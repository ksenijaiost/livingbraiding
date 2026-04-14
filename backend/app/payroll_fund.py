"""Журнал фондов ЗП: начисления с визита / работы / розницы, сторно, выплаты."""

from __future__ import annotations

from datetime import datetime
from typing import Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.db.models import (
    Kit,
    MaterialPriceCurrent,
    MaterialType,
    PayrollFundEntryKind,
    PayrollFundLedger,
    PayrollFundPayoutPaymentKind,
    PayrollFundSide,
    PayrollFundSourceKind,
    ProductSale,
    ProductSaleKind,
    StudioExpense,
    User,
    UserRole,
    Visit,
    VisitMaster,
    WorkForInventory,
    WorkForInventoryStaff,
)

_REVERSIBLE_ENTRY_KINDS = (PayrollFundEntryKind.ACCRUAL, PayrollFundEntryKind.EXPENSE)


def money_q2(x: float) -> float:
    return round(float(x), 2)


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
    row = PayrollFundLedger(
        created_at=datetime.utcnow(),
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
        )


def post_visit_accruals(db: Session, visit: Visit, created_by_user_id: int | None) -> None:
    if visit.is_cancelled:
        return
    if _has_accruals_for_source(db, PayrollFundSourceKind.VISIT, visit.id):
        return

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
        )

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
        )


def post_work_accruals(
    db: Session,
    work_id: int,
    staff_rows: Sequence[WorkForInventoryStaff],
    created_by_user_id: int | None,
) -> None:
    if _has_accruals_for_source(db, PayrollFundSourceKind.WORK, work_id):
        return
    for s in staff_rows:
        amt = money_q2(float(s.master_profit_amount or 0))
        if amt > 0:
            append_ledger(
                db,
                entry_kind=PayrollFundEntryKind.ACCRUAL,
                side=PayrollFundSide.MASTER,
                user_id=int(s.user_id),
                amount=amt,
                source_kind=PayrollFundSourceKind.WORK,
                source_id=work_id,
                created_by_user_id=created_by_user_id,
            )


def replace_work_accruals(
    db: Session,
    work_id: int,
    staff_rows: Sequence[WorkForInventoryStaff],
    created_by_user_id: int | None,
) -> None:
    storno_source_accruals(db, PayrollFundSourceKind.WORK, work_id, created_by_user_id)
    for s in staff_rows:
        amt = money_q2(float(s.master_profit_amount or 0))
        if amt > 0:
            append_ledger(
                db,
                entry_kind=PayrollFundEntryKind.ACCRUAL,
                side=PayrollFundSide.MASTER,
                user_id=int(s.user_id),
                amount=amt,
                source_kind=PayrollFundSourceKind.WORK,
                source_id=work_id,
                created_by_user_id=created_by_user_id,
            )


def compute_product_sale_studio_margin(db: Session, sale: ProductSale) -> float:
    amt = float(sale.amount_from_client or 0)
    kind = sale.kind
    if kind == ProductSaleKind.KIT and sale.kit_id and sale.kit_pieces_sold:
        kit = sale.kit or db.get(Kit, sale.kit_id)
        if kit and kit.stock_price_total is not None and float(kit.stock_price_total) > 0:
            pt = max(int(kit.pieces_total or 0), 1)
            per = float(kit.stock_price_total) / pt
            cost = per * int(sale.kit_pieces_sold)
            return money_q2(max(0.0, amt - cost))
        return money_q2(max(0.0, amt))
    if kind == ProductSaleKind.MATERIAL:
        # Маржа материала выставляется в finalize_material_sale_fields (роутер / sync).
        return money_q2(float(sale.studio_margin_amount or 0))
    if kind in (ProductSaleKind.RUBBER, ProductSaleKind.OTHER):
        return money_q2(max(0.0, amt))
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
) -> None:
    """Выплата из фонда: положительная сумма уменьшает сальдо фонда (отрицательная запись в журнале).

    Отрицательная сумма вводимая в форме даёт положительную проводку (возврат в фонд).
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
    )


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

    for exp in db.scalars(select(StudioExpense).where(StudioExpense.is_voided.is_(False))).all():
        if not has_unreversed_studio_expense_posting(db, exp.id):
            replace_studio_expense_ledger(db, exp, exp.created_by_user_id)


def recent_ledger_rows(db: Session, limit: int = 150) -> list[PayrollFundLedger]:
    return list(
        db.scalars(
            select(PayrollFundLedger)
            .options(
                selectinload(PayrollFundLedger.created_by_user),
                selectinload(PayrollFundLedger.user),
            )
            .order_by(PayrollFundLedger.id.desc())
            .limit(limit)
        ).all()
    )
