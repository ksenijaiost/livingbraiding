"""Уведомления сотрудникам о назначении при создании сущности (не брони)."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.db.models import (
    Consultation,
    HourlyWorkEntry,
    NotificationChannel,
    NotificationOutbox,
    NotificationOutboxStatus,
    ProductSale,
    ProductSaleKind,
    User,
    Visit,
    VisitMaster,
    VisitService,
    VisitServiceMaster,
    WorkForInventory,
    WorkForInventoryStaff,
    WorkKind,
    WorkPlan,
    WorkPlanType,
)
from app.display_time import format_naive_utc_datetime, get_display_timezone
from app.hourly_help import hourly_help_rows_from_visit
from app.notifications import process_outbox
from app.notify_prefs import user_wants_staff_assignment
from app.time_utils import utcnow_naive
from app.visit_addon_sales import addon_sales_from_visit_json

logger = logging.getLogger(__name__)

EVENT_STAFF_ASSIGNMENT_CREATED = "staff_assignment_created"

ENTITY_VISIT = "visit"
ENTITY_WORK = "work"
ENTITY_PRODUCT_SALE = "product_sale"
ENTITY_CONSULTATION = "consultation"
ENTITY_WORK_PLAN = "work_plan"
ENTITY_HOURLY_WORK = "hourly_work"

_ENTITY_LABEL_ACCUSATIVE = {
    ENTITY_VISIT: "визит",
    ENTITY_WORK: "работу",
    ENTITY_PRODUCT_SALE: "продажу",
    ENTITY_CONSULTATION: "консультацию",
    ENTITY_WORK_PLAN: "план работ",
    ENTITY_HOURLY_WORK: "почасовую работу",
}

_WORK_KIND_RU = {
    WorkKind.KIT: "Комплект",
    WorkKind.MIX: "Микс",
    WorkKind.RUBBER: "Хвост/резинка",
    WorkKind.KIT_CORRECTION: "Коррекция комплекта",
    WorkKind.OTHER: "Другое",
    WorkKind.HAIR_EXT_PREP: "Подготовка к наращиванию",
}

_SALE_KIND_RU = {
    ProductSaleKind.MATERIAL: "Материал",
    ProductSaleKind.KIT: "Комплект",
    ProductSaleKind.RUBBER: "Хвост/резинка",
    ProductSaleKind.OTHER: "Другое",
}

_CHANNELS = (NotificationChannel.VK, NotificationChannel.MAX, NotificationChannel.TELEGRAM)


def build_staff_assignment_message(
    *,
    entity_type: str,
    entity_id: int,
    when: datetime | None = None,
    client_name: str | None = None,
    summary: str | None = None,
    comment: str | None = None,
    tz_name: str | None = None,
) -> str:
    label = _ENTITY_LABEL_ACCUSATIVE.get(entity_type, entity_type)
    lines = [f"Вас добавили в {label} #{int(entity_id)}"]
    if when is not None:
        when_s = format_naive_utc_datetime(when, tz_name or "UTC", "%d.%m.%Y %H:%M") or "—"
        lines.append(f"Дата/время: {when_s}")
    if client_name:
        lines.append(f"Клиент: {client_name}")
    if summary:
        lines.append(f"Суть: {summary}")
    if comment:
        lines.append(f"Комментарий: {comment}")
    return "\n".join(lines)


def _dedupe_key(entity_type: str, entity_id: int, user_id: int, channel: NotificationChannel) -> str:
    return f"{entity_type}:{int(entity_id)}:{int(user_id)}:{channel.value}"


def _user_channel_targets(user: User) -> list[tuple[NotificationChannel, int]]:
    out: list[tuple[NotificationChannel, int]] = []
    if user.vk_user_id is not None:
        out.append((NotificationChannel.VK, int(user.vk_user_id)))
    if user.max_user_id is not None:
        out.append((NotificationChannel.MAX, int(user.max_user_id)))
    if user.telegram_chat_id is not None:
        out.append((NotificationChannel.TELEGRAM, int(user.telegram_chat_id)))
    return out


def notify_staff_assigned_on_create(
    db: Session,
    *,
    entity_type: str,
    entity_id: int,
    user_ids: Sequence[int],
    when: datetime | None = None,
    client_name: str | None = None,
    summary: str | None = None,
    comment: str | None = None,
) -> list[NotificationOutbox]:
    """Поставить и по возможности отправить уведомления. Ошибки глотает — create не ломает."""
    try:
        unique_ids = sorted({int(x) for x in user_ids if int(x) > 0})
        if not unique_ids:
            return []
        tz_name = get_display_timezone(db)
        text = build_staff_assignment_message(
            entity_type=entity_type,
            entity_id=int(entity_id),
            when=when,
            client_name=(client_name or "").strip() or None,
            summary=(summary or "").strip() or None,
            comment=(comment or "").strip() or None,
            tz_name=tz_name,
        )
        created: list[NotificationOutbox] = []
        for uid in unique_ids:
            user = db.get(User, uid)
            if user is None or not user_wants_staff_assignment(user, entity_type):
                continue
            targets = _user_channel_targets(user)
            if not targets:
                continue
            for channel, target_id in targets:
                key = _dedupe_key(entity_type, int(entity_id), uid, channel)
                exists = db.scalar(
                    select(NotificationOutbox.id).where(NotificationOutbox.dedupe_key == key).limit(1)
                )
                if exists is not None:
                    continue
                payload = {
                    "text": text,
                    "target_id": target_id,
                    "event_type": EVENT_STAFF_ASSIGNMENT_CREATED,
                    "entity_type": entity_type,
                    "entity_id": int(entity_id),
                }
                row = NotificationOutbox(
                    user_id=uid,
                    booking_id=None,
                    channel=channel,
                    event_type=EVENT_STAFF_ASSIGNMENT_CREATED,
                    payload_json=json.dumps(payload, ensure_ascii=False),
                    status=NotificationOutboxStatus.PENDING,
                    error=None,
                    dedupe_key=key,
                    attempt_count=0,
                    created_at=utcnow_naive(),
                    sent_at=None,
                )
                try:
                    with db.begin_nested():
                        db.add(row)
                        db.flush()
                except IntegrityError:
                    continue
                created.append(row)
        if not created:
            return []
        db.commit()
        ids = [int(r.id) for r in created if r.id is not None]
        try:
            process_outbox(db, limit=max(len(ids), 1), only_ids=ids)
            db.commit()
        except Exception:
            logger.exception(
                "staff assignment notify: process_outbox failed entity=%s id=%s",
                entity_type,
                entity_id,
            )
            try:
                db.rollback()
            except Exception:
                pass
        return created
    except Exception:
        logger.exception(
            "staff assignment notify: enqueue failed entity=%s id=%s",
            entity_type,
            entity_id,
        )
        try:
            db.rollback()
        except Exception:
            pass
        return []


def collect_visit_participant_user_ids(db: Session, visit_id: int) -> list[int]:
    """Мастера визита/услуг + почасовая помощь + продавцы допов. created_by не добавляется сам."""
    ids: set[int] = set()
    for mid in db.scalars(select(VisitMaster.master_id).where(VisitMaster.visit_id == int(visit_id))).all():
        if mid is not None:
            ids.add(int(mid))
    svc_ids = list(
        db.scalars(select(VisitService.id).where(VisitService.visit_id == int(visit_id))).all()
    )
    if svc_ids:
        for mid in db.scalars(
            select(VisitServiceMaster.master_id).where(VisitServiceMaster.visit_service_id.in_(svc_ids))
        ).all():
            if mid is not None:
                ids.add(int(mid))
    visit = db.get(Visit, int(visit_id))
    if visit is None:
        return sorted(ids)
    for row in hourly_help_rows_from_visit(visit):
        if row.master_id:
            ids.add(int(row.master_id))
    sales = addon_sales_from_visit_json(getattr(visit, "addons_details_json", None))
    if sales is not None:
        if sales.shared_seller_user_id:
            ids.add(int(sales.shared_seller_user_id))
        for line in sales.lines:
            if line.seller_user_id:
                ids.add(int(line.seller_user_id))
    return sorted(ids)


def _client_name(db: Session, client_id: int | None) -> str | None:
    if not client_id:
        return None
    from app.db.models import Client

    c = db.get(Client, int(client_id))
    if c is None:
        return None
    name = (c.name or "").strip()
    return name or None


def notify_visit_staff_assigned_on_create(db: Session, visit_id: int) -> None:
    try:
        visit = db.scalar(
            select(Visit)
            .where(Visit.id == int(visit_id))
            .options(selectinload(Visit.services), selectinload(Visit.client))
        )
        if visit is None:
            return
        user_ids = collect_visit_participant_user_ids(db, int(visit.id))
        service_names = [
            (s.service_name or "").strip()
            for s in (visit.services or [])
            if not getattr(s, "is_cancelled", False) and (s.service_name or "").strip()
        ]
        summary = ", ".join(service_names) if service_names else None
        client_name = None
        if visit.client is not None:
            client_name = (visit.client.name or "").strip() or None
        elif visit.client_id:
            client_name = _client_name(db, visit.client_id)
        notify_staff_assigned_on_create(
            db,
            entity_type=ENTITY_VISIT,
            entity_id=int(visit.id),
            user_ids=user_ids,
            when=visit.performed_date,
            client_name=client_name,
            summary=summary,
            comment=(visit.comment or "").strip() or None,
        )
    except Exception:
        logger.exception("staff assignment notify: visit #%s failed", visit_id)
        try:
            db.rollback()
        except Exception:
            pass


def notify_work_staff_assigned_on_create(db: Session, work_id: int) -> None:
    try:
        work = db.get(WorkForInventory, int(work_id))
        if work is None:
            return
        staff_ids = list(
            db.scalars(
                select(WorkForInventoryStaff.user_id).where(WorkForInventoryStaff.work_id == int(work_id))
            ).all()
        )
        kind_ru = _WORK_KIND_RU.get(work.kind, work.kind.value if work.kind else "Работа")
        notify_staff_assigned_on_create(
            db,
            entity_type=ENTITY_WORK,
            entity_id=int(work.id),
            user_ids=[int(x) for x in staff_ids if x],
            when=work.performed_date or work.created_at,
            client_name=_client_name(db, work.client_id),
            summary=kind_ru,
            comment=(work.comment or "").strip() or None,
        )
    except Exception:
        logger.exception("staff assignment notify: work #%s failed", work_id)
        try:
            db.rollback()
        except Exception:
            pass


def notify_hourly_work_staff_assigned_on_create(db: Session, entry_id: int) -> None:
    try:
        entry = db.get(HourlyWorkEntry, int(entry_id))
        if entry is None:
            return
        mins = int(entry.duration_minutes or 0)
        summary = f"Почасовая работа, {mins} мин"
        notify_staff_assigned_on_create(
            db,
            entity_type=ENTITY_HOURLY_WORK,
            entity_id=int(entry.id),
            user_ids=[int(entry.master_user_id)],
            when=entry.performed_date,
            client_name=None,
            summary=summary,
            comment=(entry.comment or "").strip() or None,
        )
    except Exception:
        logger.exception("staff assignment notify: hourly_work #%s failed", entry_id)
        try:
            db.rollback()
        except Exception:
            pass


def notify_product_sale_staff_assigned_on_create(db: Session, sale_id: int) -> None:
    try:
        sale = db.get(ProductSale, int(sale_id))
        if sale is None:
            return
        kind_ru = _SALE_KIND_RU.get(sale.kind, sale.kind.value if sale.kind else "Продажа")
        notify_staff_assigned_on_create(
            db,
            entity_type=ENTITY_PRODUCT_SALE,
            entity_id=int(sale.id),
            user_ids=[int(sale.created_by_user_id)],
            when=sale.performed_date,
            client_name=_client_name(db, sale.client_id),
            summary=kind_ru,
            comment=None,
        )
    except Exception:
        logger.exception("staff assignment notify: product_sale #%s failed", sale_id)
        try:
            db.rollback()
        except Exception:
            pass


def notify_consultation_staff_assigned_on_create(db: Session, consultation_id: int) -> None:
    try:
        c = db.get(Consultation, int(consultation_id))
        if c is None:
            return
        notify_staff_assigned_on_create(
            db,
            entity_type=ENTITY_CONSULTATION,
            entity_id=int(c.id),
            user_ids=[int(c.created_by_user_id)],
            when=c.consultation_date,
            client_name=_client_name(db, c.client_id),
            summary="Консультация",
            comment=(c.comment or "").strip() or None,
        )
    except Exception:
        logger.exception("staff assignment notify: consultation #%s failed", consultation_id)
        try:
            db.rollback()
        except Exception:
            pass


def notify_work_plan_staff_assigned_on_create(db: Session, plan_id: int) -> None:
    try:
        plan = db.get(WorkPlan, int(plan_id))
        if plan is None:
            return
        if plan.plan_type == WorkPlanType.HOURLY:
            summary = "Почасовая"
        elif plan.work_kind is not None:
            summary = _WORK_KIND_RU.get(plan.work_kind, plan.work_kind.value)
        else:
            summary = "Работа с товарами"
        notify_staff_assigned_on_create(
            db,
            entity_type=ENTITY_WORK_PLAN,
            entity_id=int(plan.id),
            user_ids=[int(plan.master_id)],
            when=plan.planned_date,
            client_name=None,
            summary=summary,
            comment=(plan.comment or "").strip() or None,
        )
    except Exception:
        logger.exception("staff assignment notify: work_plan #%s failed", plan_id)
        try:
            db.rollback()
        except Exception:
            pass
