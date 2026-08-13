"""Черновики работы с товарами: форма без склада/ЗП, блокировка, финализация."""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, selectinload
from starlette.datastructures import UploadFile

from app.auth import AuthUser
from app.db.models import (
    Client,
    User,
    UserRole,
    WorkDraft,
    WorkDraftParticipant,
    WorkKind,
    WorkScope,
)
from app.forms_parse import parse_date_iso, parse_int
from app.time_utils import utcnow_naive

LOCK_TTL = timedelta(minutes=30)

_MULTI_VALUE_KEYS = frozenset({"kit_master_on"})


@dataclass(frozen=True)
class DraftLockState:
    readonly: bool
    lock_holder: User | None = None


def collect_form_dict(form: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    for key in form.keys():
        k = str(key)
        if k in ("draft_id",):
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
            if k in _MULTI_VALUE_KEYS and len(parts) > 1:
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


def _truthy(raw: str | None) -> bool:
    return (raw or "").strip().lower() in ("1", "on", "true", "yes")


def _read_master_ids_from_form_dict(form_dict: dict[str, str], key: str = "kit_master_on") -> list[int]:
    raw = (form_dict.get(key) or "").strip()
    if not raw:
        return []
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    seen: set[int] = set()
    out: list[int] = []
    for p in parts:
        try:
            i = int(p)
        except ValueError:
            continue
        if i <= 0 or i in seen:
            continue
        seen.add(i)
        out.append(i)
    return out


def kit_master_on_ids_from_fp(fp: dict[str, str]) -> list[int]:
    return _read_master_ids_from_form_dict(fp, "kit_master_on")


def extract_participant_master_ids(
    form_dict: dict[str, str],
    *,
    current_user_id: int,
    kind: WorkKind | None,
) -> list[int]:
    ids: set[int] = set()
    if kind == WorkKind.KIT and _truthy(form_dict.get("kit_use_multi_masters")):
        for mid in _read_master_ids_from_form_dict(form_dict):
            ids.add(mid)
    if int(current_user_id) > 0:
        ids.add(int(current_user_id))
    return sorted(ids)


def _kind_label(k: WorkKind) -> str:
    return {
        WorkKind.KIT: "Комплект/Заготовки (поштучно)",
        WorkKind.MIX: "Смешка",
        WorkKind.RUBBER: "Хвосты/резинки",
        WorkKind.KIT_CORRECTION: "Коррекция комплекта",
        WorkKind.OTHER: "Другое",
        WorkKind.HAIR_EXT_PREP: "Подготовка к наращиванию волос (заглушка)",
    }.get(k, k.value)


def _scope_label(s: WorkScope) -> str:
    return "В наличие" if s == WorkScope.IN_STOCK else "На заказ"


def compute_draft_preview(
    db: Session,
    form_dict: dict[str, str],
    *,
    kind: WorkKind | None,
    scope: WorkScope | None,
    client_id: int | None,
) -> dict[str, Any]:
    client_name = None
    if client_id:
        cl = db.get(Client, int(client_id))
        client_name = cl.name if cl else None
    amount: float | None = None
    afc = (form_dict.get("amount_from_client") or "").strip()
    if afc:
        try:
            amount = float(afc.replace(",", "."))
        except ValueError:
            amount = None
    return {
        "kind": kind.value if kind else None,
        "kind_label": _kind_label(kind) if kind else "—",
        "scope": scope.value if scope else None,
        "scope_label": _scope_label(scope) if scope else "—",
        "client_name": client_name,
        "amount_from_client": amount,
    }


def draft_summary_label(preview: dict[str, Any]) -> str:
    kind_label = str(preview.get("kind_label") or "").strip()
    scope_label = str(preview.get("scope_label") or "").strip()
    parts = [p for p in (kind_label, scope_label) if p and p != "—"]
    return " · ".join(parts) if parts else "Работа"


def draft_participant_ids(db: Session, draft_id: int) -> list[int]:
    return list(
        db.scalars(
            select(WorkDraftParticipant.master_id).where(WorkDraftParticipant.work_draft_id == draft_id)
        ).all()
    )


def user_is_draft_participant(db: Session, user_id: int, draft_id: int) -> bool:
    return user_id in draft_participant_ids(db, draft_id)


def user_can_view_draft(user: AuthUser, draft: WorkDraft, db: Session) -> bool:
    if draft.finalized_work_id is not None:
        return False
    if user.role in (UserRole.ADMIN, UserRole.ADMIN_SUPER) or UserRole.ADMIN_SUPER in user.roles:
        return True
    if user.role == UserRole.MASTER:
        return user_is_draft_participant(db, user.id, int(draft.id))
    return False


def user_can_edit_draft(user: AuthUser, draft: WorkDraft, db: Session) -> bool:
    if draft.finalized_work_id is not None:
        return False
    if UserRole.ADMIN_SUPER in user.roles or user.role == UserRole.ADMIN_SUPER:
        return True
    if user.role != UserRole.MASTER:
        return False
    return user_is_draft_participant(db, user.id, int(draft.id))


def _lock_is_active(draft: WorkDraft, now: datetime | None = None) -> bool:
    if not draft.locked_by_user_id or not draft.locked_at:
        return False
    now = now or utcnow_naive()
    return (now - draft.locked_at) < LOCK_TTL


def acquire_draft_lock(db: Session, draft: WorkDraft, user_id: int) -> DraftLockState:
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


def release_draft_lock(db: Session, draft: WorkDraft, user_id: int) -> None:
    if draft.locked_by_user_id and int(draft.locked_by_user_id) == int(user_id):
        draft.locked_by_user_id = None
        draft.locked_at = None
        db.flush()


def _sync_participants(db: Session, draft_id: int, master_ids: list[int]) -> None:
    db.execute(delete(WorkDraftParticipant).where(WorkDraftParticipant.work_draft_id == draft_id))
    for mid in master_ids:
        db.add(WorkDraftParticipant(work_draft_id=draft_id, master_id=int(mid)))
    db.flush()


def _parse_optional_id(raw: str | None, *, field_name: str) -> int | None:
    s = (raw or "").strip()
    if not s:
        return None
    try:
        return parse_int(s, min=1, field_name=field_name)
    except ValueError:
        return None


def save_work_draft(
    db: Session,
    draft_id: int | None,
    form_dict: dict[str, str],
    user_id: int,
) -> WorkDraft:
    pd_raw = (form_dict.get("performed_date") or "").strip()
    if not pd_raw:
        raise ValueError("Укажите дату работы для черновика.")
    try:
        performed_dt = datetime.combine(parse_date_iso(pd_raw, field_name="performed_date"), datetime.min.time())
    except ValueError as exc:
        raise ValueError("Некорректная дата работы.") from exc

    kind_raw = (form_dict.get("kind") or "").strip()
    if not kind_raw:
        raise ValueError("Выберите вид работы.")
    try:
        kind = WorkKind(kind_raw)
    except ValueError as exc:
        raise ValueError("Выберите вид работы.") from exc

    scope_raw = (form_dict.get("scope") or "").strip()
    if not scope_raw:
        raise ValueError("Выберите режим: в наличие или на заказ.")
    try:
        scope = WorkScope(scope_raw)
    except ValueError as exc:
        raise ValueError("Выберите режим: в наличие или на заказ.") from exc

    client_id = _parse_optional_id(form_dict.get("client_id"), field_name="client_id")
    if scope == WorkScope.CUSTOM_ORDER:
        if not client_id:
            raise ValueError("Для режима «на заказ» выберите клиента.")
        if not db.get(Client, int(client_id)):
            raise ValueError("Клиент не найден.")
    elif client_id and not db.get(Client, int(client_id)):
        raise ValueError("Клиент не найден.")

    booking_id = _parse_optional_id(form_dict.get("booking_id"), field_name="booking_id")
    work_plan_id = _parse_optional_id(form_dict.get("work_plan_id"), field_name="work_plan_id")

    master_ids = extract_participant_master_ids(form_dict, current_user_id=user_id, kind=kind)
    if kind == WorkKind.KIT and _truthy(form_dict.get("kit_use_multi_masters")):
        if not _read_master_ids_from_form_dict(form_dict):
            raise ValueError("Отметьте хотя бы одного мастера в комплекте.")
    if not master_ids:
        raise ValueError("Укажите хотя бы одного мастера.")

    now = utcnow_naive()
    preview = compute_draft_preview(db, form_dict, kind=kind, scope=scope, client_id=client_id)
    form_json = json.dumps(form_dict, ensure_ascii=False)
    preview_json = json.dumps(preview, ensure_ascii=False)

    if draft_id:
        draft = db.scalar(
            select(WorkDraft)
            .where(WorkDraft.id == int(draft_id), WorkDraft.finalized_work_id.is_(None))
            .options(selectinload(WorkDraft.participants))
        )
        if not draft:
            raise ValueError("Черновик не найден.")
        draft.performed_date = performed_dt
        draft.client_id = client_id
        draft.booking_id = booking_id
        draft.work_plan_id = work_plan_id
        draft.kind = kind
        draft.scope = scope
        draft.form_json = form_json
        draft.preview_json = preview_json
        draft.updated_at = now
        draft.updated_by_user_id = int(user_id)
        _sync_participants(db, int(draft.id), master_ids)
        db.flush()
        return draft

    draft = WorkDraft(
        created_at=now,
        created_by_user_id=int(user_id),
        updated_at=now,
        updated_by_user_id=int(user_id),
        performed_date=performed_dt,
        client_id=client_id,
        booking_id=booking_id,
        work_plan_id=work_plan_id,
        kind=kind,
        scope=scope,
        form_json=form_json,
        preview_json=preview_json,
    )
    db.add(draft)
    db.flush()
    _sync_participants(db, int(draft.id), master_ids)
    return draft


def link_finalized_work(db: Session, draft_id: int, work_id: int, user_id: int) -> None:
    draft = db.scalar(
        select(WorkDraft).where(WorkDraft.id == int(draft_id), WorkDraft.finalized_work_id.is_(None))
    )
    if not draft:
        raise ValueError("Черновик не найден.")
    draft.finalized_work_id = int(work_id)
    release_draft_lock(db, draft, int(user_id))
    db.flush()


def list_open_drafts_for_master(db: Session, master_id: int) -> list[WorkDraft]:
    return list(
        db.scalars(
            select(WorkDraft)
            .join(WorkDraftParticipant, WorkDraftParticipant.work_draft_id == WorkDraft.id)
            .where(
                WorkDraft.finalized_work_id.is_(None),
                WorkDraftParticipant.master_id == int(master_id),
            )
            .options(selectinload(WorkDraft.client))
            .order_by(WorkDraft.updated_at.desc(), WorkDraft.id.desc())
        ).all()
    )


def draft_counts_by_day(
    db: Session,
    *,
    user: AuthUser,
    day_from: date,
    day_to_excl: date,
) -> dict[date, int]:
    from zoneinfo import ZoneInfo

    from app.display_time import get_display_timezone

    tz_name = get_display_timezone(db)
    tz = ZoneInfo(tz_name)
    start = datetime.combine(day_from, datetime.min.time())
    end = datetime.combine(day_to_excl, datetime.min.time())
    start_utc = start.replace(tzinfo=tz).astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    end_utc = end.replace(tzinfo=tz).astimezone(ZoneInfo("UTC")).replace(tzinfo=None)

    stmt = select(WorkDraft.performed_date).where(
        WorkDraft.finalized_work_id.is_(None),
        WorkDraft.performed_date >= start_utc,
        WorkDraft.performed_date < end_utc,
    )
    if user.role == UserRole.MASTER:
        stmt = stmt.where(
            WorkDraft.id.in_(
                select(WorkDraftParticipant.work_draft_id).where(
                    WorkDraftParticipant.master_id == user.id
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
) -> list[WorkDraft]:
    from zoneinfo import ZoneInfo

    from app.display_time import get_display_timezone

    tz_name = get_display_timezone(db)
    tz = ZoneInfo(tz_name)
    start = datetime.combine(day, datetime.min.time())
    end = start + timedelta(days=1)
    start_utc = start.replace(tzinfo=tz).astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    end_utc = end.replace(tzinfo=tz).astimezone(ZoneInfo("UTC")).replace(tzinfo=None)

    stmt = (
        select(WorkDraft)
        .options(selectinload(WorkDraft.client))
        .where(
            WorkDraft.finalized_work_id.is_(None),
            WorkDraft.performed_date >= start_utc,
            WorkDraft.performed_date < end_utc,
        )
        .order_by(WorkDraft.updated_at.desc(), WorkDraft.id.desc())
    )
    if user.role == UserRole.MASTER:
        stmt = stmt.where(
            WorkDraft.id.in_(
                select(WorkDraftParticipant.work_draft_id).where(
                    WorkDraftParticipant.master_id == user.id
                )
            )
        )
    return list(db.scalars(stmt).all())
