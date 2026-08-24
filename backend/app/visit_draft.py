"""Черновики визита: сохранение формы, предрасчёт без склада, блокировка, финализация."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, selectinload
from starlette.datastructures import UploadFile

from app.auth import AuthUser
from app.client_validation import format_created_by_label
from app.role_access import role_is_admin_staff
from app.db.models import (
    Client,
    Service,
    User,
    UserRole,
    VisitDraft,
    VisitDraftParticipant,
)
from app.time_utils import utcnow_naive
from app.user_roles import user_has_role
from app.visit_multi_service import (
    MultiServiceVisitInput,
    _resolve_client,
    compute_visit_service_line,
    parse_multi_service_visit_form,
    save_visit_with_services,
)

LOCK_TTL = timedelta(minutes=30)


@dataclass(frozen=True)
class DraftLockState:
    readonly: bool
    lock_holder: User | None = None


def collect_form_dict(form: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    for key in form.keys():
        k = str(key)
        if k.startswith("photo_"):
            continue
        vals = form.getlist(key) if hasattr(form, "getlist") else [form.get(key)]
        parts: list[str] = []
        for v in vals:
            if isinstance(v, UploadFile):
                continue
            if isinstance(v, (bytes, bytearray)):
                parts.append(v.decode())
            elif v is not None:
                parts.append(str(v))
        if parts:
            if k == "visit_master_on" and len(parts) > 1:
                out[k] = ",".join(parts)
            else:
                out[k] = parts[-1]
    return out


def form_dict_from_json(raw: str | None) -> dict[str, str]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if v is not None}


def preview_dict_from_json(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def parse_draft_form(form: Any, *, booking_id: int | None = None) -> MultiServiceVisitInput:
    return parse_multi_service_visit_form(
        form,
        single_master_default_id=None,
        booking_id=booking_id,
    )


def _require_performed_date_in_form(form_dict: dict[str, str]) -> None:
    if not (form_dict.get("performed_date") or "").strip():
        raise ValueError("Укажите дату визита для черновика.")


def extract_participant_master_ids(inp: MultiServiceVisitInput) -> list[int]:
    ids: set[int] = set()
    for mid, _ in inp.header.visit_master_allocations:
        if mid > 0:
            ids.add(int(mid))
    for line in inp.lines:
        for mid, _ in line.service_master_allocations:
            if mid > 0:
                ids.add(int(mid))
        if line.mix_bonus_master_id and int(line.mix_bonus_master_id) > 0:
            ids.add(int(line.mix_bonus_master_id))
        if line.own_corr_master_id and int(line.own_corr_master_id) > 0:
            ids.add(int(line.own_corr_master_id))
    return sorted(ids)


def _service_label(db: Session, service_id: int) -> str:
    svc = db.get(Service, service_id)
    if not svc:
        return f"Услуга #{service_id}"
    sub = svc.subcategory
    if sub:
        return f"{sub.name} — {svc.name}"
    return svc.name or f"Услуга #{service_id}"


def compute_draft_preview(db: Session, inp: MultiServiceVisitInput) -> dict[str, Any]:
    line_rows: list[dict[str, Any]] = []
    amount_total = 0.0
    cost_total = 0.0
    masters_pool_total = 0.0
    for line in inp.lines:
        comp = compute_visit_service_line(
            db,
            line,
            inp.header,
            apply_kit_stock=False,
        )
        amount_total += float(comp.amount_from_client or 0)
        cost_total += float(comp.cost_total or 0)
        masters_pool_total += float(comp.masters_pool or 0)
        line_rows.append(
            {
                "service_id": line.service_id,
                "service_label": _service_label(db, line.service_id),
                "amount_from_client": float(comp.amount_from_client or 0),
                "cost_total": float(comp.cost_total or 0),
                "masters_pool": float(comp.masters_pool or 0),
            }
        )
    return {
        "amount_from_client_total": amount_total,
        "cost_total": cost_total,
        "masters_pool_total": masters_pool_total,
        "lines": line_rows,
    }


def draft_participant_ids(db: Session, draft_id: int) -> list[int]:
    return list(
        db.scalars(
            select(VisitDraftParticipant.master_id).where(VisitDraftParticipant.visit_draft_id == draft_id)
        ).all()
    )


def user_is_draft_participant(db: Session, user_id: int, draft_id: int) -> bool:
    return user_id in draft_participant_ids(db, draft_id)


def user_can_view_draft(user: AuthUser, draft: VisitDraft, db: Session) -> bool:
    if draft.finalized_visit_id is not None:
        return False
    if role_is_admin_staff(user.role) or UserRole.ADMIN_SUPER in user.roles:
        return True
    if user.role == UserRole.MASTER:
        return user_is_draft_participant(db, user.id, int(draft.id))
    return False


def user_can_edit_draft(user: AuthUser, draft: VisitDraft, db: Session) -> bool:
    if draft.finalized_visit_id is not None:
        return False
    # Суперадмин может править любой открытый черновик (в т.ч. перехватить замок).
    if UserRole.ADMIN_SUPER in user.roles or user.role == UserRole.ADMIN_SUPER:
        return True
    if user.role != UserRole.MASTER:
        return False
    return user_is_draft_participant(db, user.id, int(draft.id))


def draft_lock_banner_for_holder(holder: User | None) -> dict[str, str] | None:
    """Баннер «сейчас редактирует» — только для чужого держателя замка."""
    if holder is None:
        return None
    from app.ru_labels import ru_user_role

    return {
        "display_name": holder.display_name or holder.username,
        "role": ru_user_role(holder.role),
    }


def _lock_is_active(draft: VisitDraft, now: datetime | None = None) -> bool:
    if not draft.locked_by_user_id or not draft.locked_at:
        return False
    now = now or utcnow_naive()
    return (now - draft.locked_at) < LOCK_TTL


def acquire_draft_lock(db: Session, draft: VisitDraft, user_id: int) -> DraftLockState:
    now = utcnow_naive()
    holder: User | None = None
    if draft.locked_by_user_id and draft.locked_at:
        if _lock_is_active(draft, now) and int(draft.locked_by_user_id) != int(user_id):
            holder = db.get(User, int(draft.locked_by_user_id))
            return DraftLockState(readonly=True, lock_holder=holder)
    draft.locked_by_user_id = int(user_id)
    draft.locked_at = now
    db.flush()
    return DraftLockState(readonly=False, lock_holder=None)


def release_draft_lock(db: Session, draft: VisitDraft, user_id: int) -> None:
    if draft.locked_by_user_id and int(draft.locked_by_user_id) == int(user_id):
        draft.locked_by_user_id = None
        draft.locked_at = None
        db.flush()


def _sync_participants(db: Session, draft_id: int, master_ids: list[int]) -> None:
    db.execute(delete(VisitDraftParticipant).where(VisitDraftParticipant.visit_draft_id == draft_id))
    for mid in master_ids:
        db.add(VisitDraftParticipant(visit_draft_id=draft_id, master_id=int(mid)))
    db.flush()


def _header_client_id_for_save(
    db: Session,
    inp: MultiServiceVisitInput,
    *,
    existing_client_id: int | None,
    created_by_label: str | None,
) -> int:
    if existing_client_id and existing_client_id > 0:
        inp.header.client_mode = "existing"
        inp.header.existing_client_id = int(existing_client_id)
    client = _resolve_client(db, inp.header, created_by_label=created_by_label)
    return int(client.id)


def save_visit_draft(
    db: Session,
    draft_id: int | None,
    inp: MultiServiceVisitInput,
    user_id: int,
    form_dict: dict[str, str],
    *,
    created_by_label: str | None = None,
) -> VisitDraft:
    _require_performed_date_in_form(form_dict)
    if not inp.lines:
        raise ValueError("Добавьте хотя бы одну услугу.")

    now = utcnow_naive()
    performed_dt = datetime.combine(inp.header.performed_date, datetime.min.time())
    master_ids = extract_participant_master_ids(inp)
    if not master_ids:
        raise ValueError("Укажите хотя бы одного мастера в визите или по услугам.")

    if draft_id:
        draft = db.scalar(
            select(VisitDraft)
            .where(VisitDraft.id == int(draft_id), VisitDraft.finalized_visit_id.is_(None))
            .options(selectinload(VisitDraft.participants))
        )
        if not draft:
            raise ValueError("Черновик не найден.")
        client_id = _header_client_id_for_save(
            db,
            inp,
            existing_client_id=int(draft.client_id),
            created_by_label=created_by_label,
        )
        draft.client_id = client_id
        draft.performed_date = performed_dt
        draft.booking_id = inp.header.booking_id
        draft.form_json = json.dumps(form_dict, ensure_ascii=False)
        draft.preview_json = json.dumps(compute_draft_preview(db, inp), ensure_ascii=False)
        draft.updated_at = now
        draft.updated_by_user_id = int(user_id)
        _sync_participants(db, int(draft.id), master_ids)
        db.flush()
        return draft

    client_id = _header_client_id_for_save(db, inp, existing_client_id=None, created_by_label=created_by_label)
    draft = VisitDraft(
        created_at=now,
        created_by_user_id=int(user_id),
        updated_at=now,
        updated_by_user_id=int(user_id),
        performed_date=performed_dt,
        client_id=client_id,
        booking_id=inp.header.booking_id,
        form_json=json.dumps(form_dict, ensure_ascii=False),
        preview_json=json.dumps(compute_draft_preview(db, inp), ensure_ascii=False),
    )
    db.add(draft)
    db.flush()
    _sync_participants(db, int(draft.id), master_ids)
    return draft


def finalize_visit_draft(
    db: Session,
    draft_id: int,
    inp: MultiServiceVisitInput,
    user_id: int,
    *,
    created_by_label: str | None = None,
) -> int:
    draft = db.scalar(
        select(VisitDraft).where(VisitDraft.id == int(draft_id), VisitDraft.finalized_visit_id.is_(None))
    )
    if not draft:
        raise ValueError("Черновик не найден.")
    inp.header.existing_client_id = int(draft.client_id)
    inp.header.client_mode = "existing"
    visit = save_visit_with_services(db, int(user_id), inp, created_by_label=created_by_label)
    draft.finalized_visit_id = int(visit.id)
    release_draft_lock(db, draft, int(user_id))
    db.flush()
    return int(visit.id)


def list_open_drafts_for_master(db: Session, master_id: int) -> list[VisitDraft]:
    return list(
        db.scalars(
            select(VisitDraft)
            .join(VisitDraftParticipant, VisitDraftParticipant.visit_draft_id == VisitDraft.id)
            .where(
                VisitDraft.finalized_visit_id.is_(None),
                VisitDraftParticipant.master_id == int(master_id),
            )
            .options(selectinload(VisitDraft.client))
            .order_by(VisitDraft.updated_at.desc(), VisitDraft.id.desc())
        ).all()
    )


def draft_counts_by_day(
    db: Session,
    *,
    user: AuthUser,
    day_from: date,
    day_to_excl: date,
) -> dict[date, int]:
    """day_to_excl — первый день вне диапазона (как month_end_utc → local date)."""
    from collections import defaultdict
    from zoneinfo import ZoneInfo

    from app.display_time import get_display_timezone

    tz_name = get_display_timezone(db)
    tz = ZoneInfo(tz_name)
    start = datetime.combine(day_from, datetime.min.time())
    end = datetime.combine(day_to_excl, datetime.min.time())
    start_utc = start.replace(tzinfo=tz).astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    end_utc = end.replace(tzinfo=tz).astimezone(ZoneInfo("UTC")).replace(tzinfo=None)

    stmt = select(VisitDraft.performed_date).where(
        VisitDraft.finalized_visit_id.is_(None),
        VisitDraft.performed_date >= start_utc,
        VisitDraft.performed_date < end_utc,
    )
    if user.role == UserRole.MASTER:
        stmt = stmt.where(
            VisitDraft.id.in_(
                select(VisitDraftParticipant.visit_draft_id).where(
                    VisitDraftParticipant.master_id == user.id
                )
            )
        )
    counts: dict[date, int] = defaultdict(int)
    for (dt0,) in db.execute(stmt).all():
        if isinstance(dt0, datetime):
            d_local = dt0.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz).date()
            counts[d_local] += 1
    return dict(counts)


def drafts_for_calendar_day(
    db: Session,
    *,
    user: AuthUser,
    day: date,
) -> list[VisitDraft]:
    from zoneinfo import ZoneInfo

    from app.display_time import get_display_timezone

    tz_name = get_display_timezone(db)
    tz = ZoneInfo(tz_name)
    start = datetime.combine(day, datetime.min.time())
    end = start + timedelta(days=1)
    start_utc = start.replace(tzinfo=tz).astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    end_utc = end.replace(tzinfo=tz).astimezone(ZoneInfo("UTC")).replace(tzinfo=None)

    stmt = (
        select(VisitDraft)
        .options(selectinload(VisitDraft.client))
        .where(
            VisitDraft.finalized_visit_id.is_(None),
            VisitDraft.performed_date >= start_utc,
            VisitDraft.performed_date < end_utc,
        )
        .order_by(VisitDraft.updated_at.desc(), VisitDraft.id.desc())
    )
    if user.role == UserRole.MASTER:
        stmt = stmt.where(
            VisitDraft.id.in_(
                select(VisitDraftParticipant.visit_draft_id).where(
                    VisitDraftParticipant.master_id == user.id
                )
            )
        )
    return list(db.scalars(stmt).all())


def draft_summary_label(preview: dict[str, Any]) -> str:
    lines = preview.get("lines") or []
    if not lines:
        return "—"
    labels = [str(x.get("service_label") or "") for x in lines if x.get("service_label")]
    if not labels:
        return "—"
    if len(labels) <= 2:
        return ", ".join(labels)
    return f"{labels[0]}, {labels[1]} +{len(labels) - 2}"


def ensure_master_participant(db: Session, user_id: int) -> None:
    if not user_has_role(db, user_id, UserRole.MASTER):
        raise ValueError("Только мастер может редактировать черновик.")
