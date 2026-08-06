"""Безвозвратное удаление тестовых сущностей: только суперадмин, с явным подтверждением."""

from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session, selectinload

from app.db.models import (
    Booking,
    Client,
    Consultation,
    HourlyWorkEntry,
    Kit,
    KitAuditLog,
    KitReserve,
    ProductSale,
    ProductSaleKind,
    SuperAdminPurgeLog,
    Visit,
    VisitAuditLog,
    VisitKitUsage,
    WorkForInventory,
    WorkPlan,
    WorkPlanStatus,
)
from app.display_time import format_naive_utc_datetime, get_display_timezone
from app.forms_parse import parse_int
from app.payroll_fund import PayrollFundSourceKind, storno_source_accruals
from app.kit_blank_stock_core import parse_usage_breakdown_json, return_reserve_row_to_stock
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
        if r.kit_id and r.pieces_reserved:
            kit = db.get(Kit, int(r.kit_id))
            if kit is not None:
                return_reserve_row_to_stock(db, kit, r)
                kit.updated_at = utcnow_naive()
                if changed_by_user_id is not None:
                    kit.updated_by_user_id = changed_by_user_id
        db.delete(r)


def _storno_consultation(db: Session, consultation_id: int, actor_user_id: int | None) -> None:
    storno_source_accruals(
        db, PayrollFundSourceKind.CONSULTATION, int(consultation_id), actor_user_id
    )


def purge_visit_hard(db: Session, visit_id: int, *, actor_user_id: int | None) -> None:
    visit = db.scalar(
        select(Visit)
        .options(
            selectinload(Visit.kit_usages).selectinload(VisitKitUsage.kit),
            selectinload(Visit.services),
        )
        .where(Visit.id == int(visit_id))
    )
    if visit is None:
        raise ValueError("Визит не найден.")
    ok, err = _visit_cancel_revert_stock(db, visit)
    if not ok:
        raise ValueError(err or "Не удалось вернуть комплект на склад для этого визита.")
    # Сначала услуги (иначе cascade удалит visit_services и останутся «Визит ?»).
    for vs in list(visit.services or []):
        storno_source_accruals(db, PayrollFundSourceKind.VISIT_SERVICE, int(vs.id), actor_user_id)
    storno_source_accruals(db, PayrollFundSourceKind.VISIT, visit.id, actor_user_id)
    db.execute(delete(VisitAuditLog).where(VisitAuditLog.visit_id == visit.id))
    db.delete(visit)


def purge_booking_hard(db: Session, booking_id: int, *, actor_user_id: int | None) -> None:
    b = db.get(Booking, int(booking_id))
    if b is None:
        raise ValueError("Бронь не найдена.")
    release_booking_kit_reserves(db, booking_id=b.id, changed_by_user_id=actor_user_id)
    bid = int(b.id)

    # ЗП консультации, привязанной к этой брони как результат.
    if b.consultation_id:
        _storno_consultation(db, int(b.consultation_id), actor_user_id)
        b.consultation_id = None

    # Консультация, у которой эта бронь — source_booking (FK надо снять до delete).
    src_cons = db.scalar(select(Consultation).where(Consultation.source_booking_id == bid))
    if src_cons is not None:
        _storno_consultation(db, int(src_cons.id), actor_user_id)
        db.execute(
            update(Booking)
            .where(Booking.consultation_id == int(src_cons.id))
            .values(consultation_id=None)
        )
        src_cons.source_booking_id = None

    db.execute(update(Visit).where(Visit.booking_id == bid).values(booking_id=None))
    db.execute(update(ProductSale).where(ProductSale.booking_id == bid).values(booking_id=None))
    db.execute(update(WorkForInventory).where(WorkForInventory.booking_id == bid).values(booking_id=None))
    db.flush()
    db.delete(b)


def purge_product_sale_hard(db: Session, sale_id: int, *, actor_user_id: int | None) -> None:
    sale = db.get(ProductSale, int(sale_id))
    if sale is None:
        raise ValueError("Продажа не найдена.")
    # Всегда сторно (идемпотентно): и для аннулированных, если проводки остались.
    storno_source_accruals(db, PayrollFundSourceKind.PRODUCT_SALE, sale.id, actor_user_id)
    if not sale.is_voided:
        if sale.kind == ProductSaleKind.KIT and sale.kit_id and sale.kit_pieces_sold:
            _apply_kit_delta(
                db,
                int(sale.kit_id),
                int(sale.kit_pieces_sold),
                breakdown=parse_usage_breakdown_json(getattr(sale, "kit_breakdown_json", None)),
            )
    db.delete(sale)


def purge_work_hard(db: Session, work_id: int, *, actor_user_id: int | None) -> None:
    w = db.get(WorkForInventory, int(work_id))
    if w is None:
        raise ValueError("Работа не найдена.")
    storno_source_accruals(db, PayrollFundSourceKind.WORK, w.id, actor_user_id)
    if not w.is_voided and w.created_kit_id:
        kit = db.get(Kit, int(w.created_kit_id))
        if kit is not None:
            kit.is_archived = True
            kit.is_in_stock = False
            kit.pieces_available = 0
            kit.updated_at = utcnow_naive()
            if actor_user_id is not None:
                kit.updated_by_user_id = actor_user_id
    db.delete(w)


def purge_consultation_hard(db: Session, consultation_id: int, *, actor_user_id: int | None) -> None:
    """Сторно ЗП и удаление консультации."""
    cons = db.get(Consultation, int(consultation_id))
    if cons is None:
        raise ValueError("Консультация не найдена.")
    _storno_consultation(db, int(cons.id), actor_user_id)
    db.execute(
        update(Booking).where(Booking.consultation_id == int(cons.id)).values(consultation_id=None)
    )
    cons.source_booking_id = None
    db.flush()
    db.delete(cons)


def purge_hourly_work_hard(db: Session, entry_id: int, *, actor_user_id: int | None) -> None:
    """Сторно ЗП и удаление почасовой работы."""
    entry = db.get(HourlyWorkEntry, int(entry_id))
    if entry is None:
        raise ValueError("Почасовая работа не найдена.")
    plan_id = int(entry.work_plan_id) if entry.work_plan_id else None
    storno_source_accruals(db, PayrollFundSourceKind.HOURLY_WORK, int(entry.id), actor_user_id)
    db.delete(entry)
    db.flush()
    if plan_id is not None:
        plan = db.get(WorkPlan, plan_id)
        if plan is not None and plan.status == WorkPlanStatus.COMPLETED:
            still = db.scalar(
                select(HourlyWorkEntry.id).where(
                    HourlyWorkEntry.work_plan_id == plan_id,
                    HourlyWorkEntry.is_voided.is_(False),
                ).limit(1)
            )
            if still is None:
                plan.status = WorkPlanStatus.PLANNED
                plan.completed_at = None
                plan.updated_at = utcnow_naive()


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

    cons_ids = list(
        db.scalars(select(Consultation.id).where(Consultation.client_id == cid).order_by(Consultation.id.asc())).all()
    )
    for cid_cons in cons_ids:
        purge_consultation_hard(db, int(cid_cons), actor_user_id=actor_user_id)

    booking_ids = list(db.scalars(select(Booking.id).where(Booking.client_id == cid).order_by(Booking.id.asc())).all())
    for bid in booking_ids:
        purge_booking_hard(db, int(bid), actor_user_id=actor_user_id)

    release_client_kit_reserves(db, client_id=cid, changed_by_user_id=actor_user_id)
    db.delete(c)


def run_purge(
    db: Session,
    *,
    entity: str,
    entity_id: int | list[int],
    confirm1: str,
    confirm2: str,
    actor_user_id: int | None,
) -> None:
    if (confirm1 or "").strip() != CONFIRM_PHRASE_1 or (confirm2 or "").strip() != CONFIRM_PHRASE_2:
        raise ValueError(
            f"Подтверждение: в первое поле введите «{CONFIRM_PHRASE_1}», во второе — «{CONFIRM_PHRASE_2}»."
        )
    kind = (entity or "").strip().lower()
    preview = build_purge_preview(db, kind, entity_id)
    if not preview.get("ok"):
        raise ValueError(str(preview.get("error") or "Объект не найден."))

    if kind == "visit":
        purge_visit_hard(db, int(entity_id), actor_user_id=actor_user_id)
    elif kind == "booking":
        purge_booking_hard(db, int(entity_id), actor_user_id=actor_user_id)
    elif kind == "product_sale":
        purge_product_sale_hard(db, int(entity_id), actor_user_id=actor_user_id)
    elif kind == "work":
        purge_work_hard(db, int(entity_id), actor_user_id=actor_user_id)
    elif kind == "consultation":
        purge_consultation_hard(db, int(entity_id), actor_user_id=actor_user_id)
    elif kind == "hourly_work":
        purge_hourly_work_hard(db, int(entity_id), actor_user_id=actor_user_id)
    elif kind == "client":
        purge_client_hard(db, int(entity_id), actor_user_id=actor_user_id)
    elif kind == "kit":
        ids = entity_id if isinstance(entity_id, list) else [int(entity_id)]
        purge_kits_hard(db, ids, actor_user_id=actor_user_id)
    else:
        raise ValueError("Неизвестный тип объекта.")

    ids = entity_id if isinstance(entity_id, list) else [int(entity_id)]
    ids_text = ", ".join(str(i) for i in ids[:40]) + ("…" if len(ids) > 40 else "")
    details = "\n".join(str(x) for x in (preview.get("lines") or []))
    db.add(
        SuperAdminPurgeLog(
            purged_at=utcnow_naive(),
            actor_user_id=actor_user_id,
            entity_kind=kind,
            entity_ids_text=ids_text[:500],
            heading=str(preview.get("heading") or "")[:240] or None,
            details_text=details or None,
        )
    )


def list_purge_history(db: Session, *, limit: int = 50) -> list[SuperAdminPurgeLog]:
    return list(
        db.scalars(
            select(SuperAdminPurgeLog)
            .options(selectinload(SuperAdminPurgeLog.actor_user))
            .order_by(SuperAdminPurgeLog.purged_at.desc(), SuperAdminPurgeLog.id.desc())
            .limit(limit)
        ).all()
    )


def parse_purge_entity(entity: str, entity_id_raw: str) -> tuple[str, int | list[int]]:
    e = (entity or "").strip().lower()
    raw = (entity_id_raw or "").strip()
    if e == "kit":
        parts = [p.strip() for p in raw.replace("\n", ",").replace(" ", ",").split(",") if p.strip()]
        if not parts:
            raise ValueError("id: укажите ID (одно число) или список ID через запятую/пробел/перенос строки.")
        ids: list[int] = []
        for p in parts:
            try:
                ids.append(int(parse_int(p, min=1, field_name="id")))
            except ValueError as exc:
                raise ValueError(str(exc)) from exc
        seen: set[int] = set()
        out: list[int] = []
        for x in ids:
            if x in seen:
                continue
            seen.add(x)
            out.append(x)
        return e, out if len(out) > 1 else int(out[0])
    try:
        eid = parse_int(raw, min=1, field_name="id")
    except ValueError as exc:
        raise ValueError(str(exc)) from exc
    return e, int(eid)


def _fmt_dt(dt, tz_name: str) -> str:
    if dt is None:
        return "—"
    try:
        return format_naive_utc_datetime(dt, tz_name) or "—"
    except Exception:
        return str(dt)


def build_purge_preview(db: Session, entity: str, entity_id: int | list[int]) -> dict:
    """Краткое описание объекта для экрана подтверждения удаления (суперадмин)."""
    kind = (entity or "").strip().lower()
    ids = entity_id if isinstance(entity_id, list) else [int(entity_id)]
    eid = int(ids[0]) if ids else 0
    tz = get_display_timezone(db)

    if kind == "visit":
        visit = db.scalar(
            select(Visit)
            .options(
                selectinload(Visit.client),
                selectinload(Visit.services),
            )
            .where(Visit.id == eid)
        )
        if visit is None:
            return {"ok": False, "error": "Визит не найден."}
        cname = (visit.client.name or "").strip() if visit.client else "—"
        phone = (visit.client.phone or "").strip() if visit.client else ""
        svc = " · ".join(
            (s.service_name or "").strip()
            for s in (visit.services or [])
            if (s.service_name or "").strip()
        ) or "—"
        cancelled = "да" if visit.is_cancelled else "нет"
        lines = [
            f"Клиент: {cname}" + (f", {phone}" if phone else ""),
            f"Дата визита: {_fmt_dt(visit.performed_date, tz)}",
            f"Услуги: {svc}",
            f"Сумма от клиента: {float(visit.amount_from_client or 0):.2f} ₽",
            f"Отменён: {cancelled}",
        ]
        return {"ok": True, "heading": f"Визит #{eid}", "lines": lines}

    if kind == "booking":
        b = db.scalar(
            select(Booking)
            .options(selectinload(Booking.client), selectinload(Booking.planned_service))
            .where(Booking.id == eid)
        )
        if b is None:
            return {"ok": False, "error": "Бронь не найдена."}
        cname = (b.client.name or "").strip() if b.client else "—"
        phone = (b.client.phone or "").strip() if b.client else ""
        svc = ""
        if b.planned_service:
            svc = (b.planned_service.name or "").strip()
        lines = [
            f"Клиент: {cname}" + (f", {phone}" if phone else ""),
            f"План: {_fmt_dt(b.planned_date, tz)}",
            f"Тип: {b.kind.value if b.kind else '—'}, статус: {b.status.value if b.status else '—'}",
        ]
        if svc:
            lines.append(f"Запланированная услуга: {svc}")
        if (b.quoted_price_text or "").strip():
            lines.append(f"Ориентир цены: {(b.quoted_price_text or '').strip()}")
        return {"ok": True, "heading": f"Бронь #{eid}", "lines": lines}

    if kind == "product_sale":
        s = db.scalar(select(ProductSale).options(selectinload(ProductSale.client)).where(ProductSale.id == eid))
        if s is None:
            return {"ok": False, "error": "Продажа не найдена."}
        cname = (s.client.name or "").strip() if s.client else "—"
        phone = (s.client.phone or "").strip() if s.client else ""
        voided = "да" if s.is_voided else "нет"
        lines = [
            f"Клиент: {cname}" + (f", {phone}" if phone else ""),
            f"Дата: {_fmt_dt(s.performed_date, tz)}",
            f"Вид: {s.kind.value if s.kind else '—'}",
            f"Сумма: {int(s.amount_from_client or 0)} ₽",
            f"Аннулирована: {voided}",
        ]
        return {"ok": True, "heading": f"Продажа товара #{eid}", "lines": lines}

    if kind == "work":
        w = db.scalar(
            select(WorkForInventory).options(selectinload(WorkForInventory.client)).where(WorkForInventory.id == eid)
        )
        if w is None:
            return {"ok": False, "error": "Работа не найдена."}
        cpart = "—"
        if w.client:
            cpart = (w.client.name or "").strip()
            ph = (w.client.phone or "").strip()
            if ph:
                cpart += f", {ph}"
        elif w.client_id:
            cpart = f"id клиента {w.client_id}"
        voided = "да" if w.is_voided else "нет"
        num = (w.display_number or "").strip() or f"#{w.id}"
        lines = [
            f"Номер записи: {num}",
            f"Клиент: {cpart}",
            f"Дата: {_fmt_dt(w.performed_date or w.created_at, tz)}",
            f"Вид: {w.kind.value if w.kind else '—'}, сфера: {w.scope.value if w.scope else '—'}",
            f"Аннулирована: {voided}",
        ]
        if w.created_kit_id:
            lines.append(f"Созданный комплект (kit id): {w.created_kit_id}")
        return {"ok": True, "heading": f"Работа с товарами #{eid}", "lines": lines}

    if kind == "consultation":
        cons = db.scalar(
            select(Consultation)
            .options(selectinload(Consultation.client), selectinload(Consultation.created_by_user))
            .where(Consultation.id == eid)
        )
        if cons is None:
            return {"ok": False, "error": "Консультация не найдена."}
        cname = (cons.client.name or "").strip() if cons.client else "—"
        phone = (cons.client.phone or "").strip() if cons.client else ""
        master = "—"
        if cons.created_by_user:
            master = (cons.created_by_user.display_name or cons.created_by_user.username or "").strip() or "—"
        lines = [
            f"Клиент: {cname}" + (f", {phone}" if phone else ""),
            f"Дата: {_fmt_dt(cons.consultation_date, tz)}",
            f"Мастер (создал): {master}",
        ]
        if cons.duration_minutes:
            lines.append(f"Длительность: {int(cons.duration_minutes)} мин")
        if (cons.preliminary_cost_text or "").strip():
            lines.append(f"Ориентир: {(cons.preliminary_cost_text or '').strip()}")
        return {"ok": True, "heading": f"Консультация #{eid}", "lines": lines}

    if kind == "hourly_work":
        entry = db.scalar(
            select(HourlyWorkEntry)
            .options(selectinload(HourlyWorkEntry.master_user))
            .where(HourlyWorkEntry.id == eid)
        )
        if entry is None:
            return {"ok": False, "error": "Почасовая работа не найдена."}
        master = "—"
        if entry.master_user:
            master = (entry.master_user.display_name or entry.master_user.username or "").strip() or "—"
        voided = "да" if entry.is_voided else "нет"
        lines = [
            f"Мастер: {master}",
            f"Дата: {_fmt_dt(entry.performed_date, tz)}",
            f"Длительность: {int(entry.duration_minutes or 0)} мин",
            f"Сумма: {float(entry.amount or 0):.0f} ₽",
            f"Аннулирована: {voided}",
        ]
        if entry.work_plan_id:
            lines.append(f"План работ: #{int(entry.work_plan_id)}")
        if (entry.comment or "").strip():
            lines.append(f"Комментарий: {(entry.comment or '').strip()}")
        return {"ok": True, "heading": f"Почасовая работа #{eid}", "lines": lines}

    if kind == "client":
        c = db.get(Client, eid)
        if c is None:
            return {"ok": False, "error": "Клиент не найден."}
        phone = (c.phone or "").strip()
        lines = [
            f"Имя: {(c.name or '').strip() or '—'}" + (f", тел. {phone}" if phone else ""),
        ]
        if (c.telegram or "").strip():
            lines.append(f"Telegram: {(c.telegram or '').strip()}")
        cid = int(c.id)
        v_n = int(db.scalar(select(func.count()).select_from(Visit).where(Visit.client_id == cid)) or 0)
        b_n = int(db.scalar(select(func.count()).select_from(Booking).where(Booking.client_id == cid)) or 0)
        s_n = int(db.scalar(select(func.count()).select_from(ProductSale).where(ProductSale.client_id == cid)) or 0)
        w_n = int(db.scalar(select(func.count()).select_from(WorkForInventory).where(WorkForInventory.client_id == cid)) or 0)
        cons_n = int(db.scalar(select(func.count()).select_from(Consultation).where(Consultation.client_id == cid)) or 0)
        lines.append(
            f"Связано (будет удалено/отвязано): визитов {v_n}, броней {b_n}, продаж {s_n}, работ {w_n}, консультаций {cons_n}"
        )
        return {"ok": True, "heading": f"Клиент #{eid}", "lines": lines}

    if kind == "kit":
        if not ids:
            return {"ok": False, "error": "ID не указан."}
        kits = list(db.scalars(select(Kit).where(Kit.id.in_(ids)).order_by(Kit.id.asc())).all())
        found_ids = {int(k.id) for k in kits}
        missing = [str(i) for i in ids if int(i) not in found_ids]

        blockers: list[str] = []
        for kid in ids[:80]:
            used_v = int(db.scalar(select(func.count()).select_from(VisitKitUsage).where(VisitKitUsage.kit_id == int(kid))) or 0)
            used_s = int(db.scalar(select(func.count()).select_from(ProductSale).where(ProductSale.kit_id == int(kid))) or 0)
            used_w = int(db.scalar(select(func.count()).select_from(WorkForInventory).where(WorkForInventory.created_kit_id == int(kid))) or 0)
            if used_v or used_s or used_w:
                blockers.append(f"{kid} (визиты:{used_v}, продажи:{used_s}, работы:{used_w})")

        lines: list[str] = []
        lines.append(f"ID к удалению: {', '.join(str(x) for x in ids[:40])}" + ("…" if len(ids) > 40 else ""))
        if missing:
            lines.append("Не найдены: " + ", ".join(missing[:40]) + ("…" if len(missing) > 40 else ""))
        if kits:
            sample = kits[:5]
            lines.append("Примеры карточек: " + "; ".join(f"#{int(k.id)} {k.sku} — {k.title}" for k in sample))
        if blockers:
            lines.append("Нельзя удалить (есть использование): " + "; ".join(blockers[:10]) + ("…" if len(blockers) > 10 else ""))
        heading = f"Комплект #{ids[0]}" if len(ids) == 1 else f"Комплекты (x{len(ids)})"
        return {"ok": True, "heading": heading, "lines": lines}

    return {"ok": False, "error": "Неизвестный тип объекта."}


def purge_kits_hard(db: Session, kit_ids: list[int], *, actor_user_id: int | None) -> None:
    ids = [int(x) for x in kit_ids if int(x) > 0]
    if not ids:
        raise ValueError("ID комплекта не указан.")

    bad: list[str] = []
    for kid in ids:
        used_v = int(db.scalar(select(func.count()).select_from(VisitKitUsage).where(VisitKitUsage.kit_id == kid)) or 0)
        used_s = int(db.scalar(select(func.count()).select_from(ProductSale).where(ProductSale.kit_id == kid)) or 0)
        used_w = int(db.scalar(select(func.count()).select_from(WorkForInventory).where(WorkForInventory.created_kit_id == kid)) or 0)
        if used_v or used_s or used_w:
            bad.append(f"{kid} (визиты:{used_v}, продажи:{used_s}, работы:{used_w})")
    if bad:
        raise ValueError("Нельзя удалить: комплект(ы) использованы в данных. " + "; ".join(bad[:12]) + ("…" if len(bad) > 12 else ""))

    kits = list(db.scalars(select(Kit).where(Kit.id.in_(ids)).order_by(Kit.id.asc())).all())
    found_ids = {int(k.id) for k in kits}
    missing = [str(i) for i in ids if int(i) not in found_ids]
    if missing:
        raise ValueError("Не найдены комплекты: " + ", ".join(missing[:40]) + ("…" if len(missing) > 40 else ""))

    for k in kits:
        db.execute(delete(KitReserve).where(KitReserve.kit_id == int(k.id)))
        db.execute(delete(KitAuditLog).where(KitAuditLog.kit_id == int(k.id)))
        db.delete(k)
