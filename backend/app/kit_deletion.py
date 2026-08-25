"""Безвозвратное удаление комплекта: проверки и запись в super_admin_purge_logs."""

from __future__ import annotations

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from app.db.models import (
    Kit,
    KitAuditLog,
    KitReserve,
    ProductSale,
    SuperAdminPurgeLog,
    VisitKitUsage,
    WorkForInventory,
)
from app.time_utils import utcnow_naive

KIT_DELETE_SOURCE_CARD = "kit_card"
KIT_DELETE_SOURCE_PURGE = "super_purge"


def kit_deletion_details_text(kit: Kit) -> str:
    lines = [
        f"ID: {kit.id}",
        f"Артикул: {kit.sku}",
        f"Название: {kit.title}",
        f"Актуален: {'да' if kit.is_active else 'нет'}",
        f"Остаток: {kit.pieces_available} / {kit.pieces_total}",
        f"Цена продажи: {kit.stock_price_total if kit.stock_price_total is not None else '—'}",
        f"Скидка: {kit.discount_percent}%",
        f"Себестоимость: {kit.cost_total if kit.cost_total is not None else '—'}",
    ]
    if kit.description:
        lines.append(f"Описание: {kit.description}")
    if kit.composition_json:
        lines.append(f"composition_json: {kit.composition_json}")
    if kit.stock_price_snapshot_text:
        lines.append("stock_price_snapshot_text:")
        lines.append(kit.stock_price_snapshot_text)
    if kit.cost_snapshot_text:
        lines.append("cost_snapshot_text:")
        lines.append(kit.cost_snapshot_text)
    if kit.materials_text:
        lines.append(f"Материалы: {kit.materials_text}")
    if kit.notes:
        lines.append(f"Заметки: {kit.notes}")
    if kit.photo_1:
        lines.append(f"Фото: {kit.photo_1}")
    return "\n".join(lines)


def write_kit_deletion_log(
    db: Session,
    kit: Kit,
    *,
    actor_user_id: int | None,
    source: str,
) -> None:
    details = f"Источник: {source}\n{kit_deletion_details_text(kit)}"
    db.add(
        SuperAdminPurgeLog(
            purged_at=utcnow_naive(),
            actor_user_id=actor_user_id,
            entity_kind="kit",
            entity_ids_text=str(int(kit.id)),
            heading=f"Комплект #{kit.id} {kit.sku}"[:240],
            details_text=details or None,
        )
    )


def kit_hard_delete_error(db: Session, kit: Kit) -> str | None:
    if kit.is_active:
        return (
            "Удалять можно только неактуальные комплекты "
            "(кнопка «Сделать неактуальным» или галочка в редактировании)."
        )
    kid = int(kit.id)
    used_v = int(
        db.scalar(select(func.count()).select_from(VisitKitUsage).where(VisitKitUsage.kit_id == kid)) or 0
    )
    used_s = int(
        db.scalar(select(func.count()).select_from(ProductSale).where(ProductSale.kit_id == kid)) or 0
    )
    if used_v or used_s:
        return (
            f"Нельзя удалить: комплект использован в данных "
            f"(визиты:{used_v}, продажи:{used_s})."
        )
    reserves = int(
        db.scalar(
            select(func.count())
            .select_from(KitReserve)
            .where(KitReserve.kit_id == kid, KitReserve.pieces_reserved > 0)
        )
        or 0
    )
    if reserves:
        return "Снимите все резервы перед удалением комплекта."
    return None


def hard_delete_kit(
    db: Session,
    kit: Kit,
    *,
    actor_user_id: int | None,
    source: str,
) -> None:
    err = kit_hard_delete_error(db, kit)
    if err:
        raise ValueError(err)
    write_kit_deletion_log(db, kit, actor_user_id=actor_user_id, source=source)
    kid = int(kit.id)
    # Работа, создавшая комплект, остаётся в истории — только отвязываем указатель.
    db.execute(
        update(WorkForInventory)
        .where(WorkForInventory.created_kit_id == kid)
        .values(created_kit_id=None)
    )
    db.execute(delete(KitReserve).where(KitReserve.kit_id == kid))
    db.execute(delete(KitAuditLog).where(KitAuditLog.kit_id == kid))
    db.delete(kit)
