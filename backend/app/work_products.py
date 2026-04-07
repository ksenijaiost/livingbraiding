"""
Работа с товарами: единая форма (в наличие / на заказ) + запись в work_for_inventory.
Этап 6.3.2: каркас, без детальных расчётов по видам.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload
from starlette.datastructures import UploadFile

from app.auth import AuthUser, require_role
from app.client_validation import format_created_by_label
from app.db.models import (
    Client,
    MixComplexity,
    MixSource,
    User,
    UserRole,
    WorkForInventory,
    WorkForInventoryStaff,
    WorkKind,
    WorkRate,
    WorkScope,
)
from app.db.session import get_db
from app.kit_inlay_visit import _materials_cost_and_snapshot

templates = Jinja2Templates(directory="app/templates")
router = APIRouter(prefix="/sales/work", tags=["work-products"])
_STAFF = Depends(require_role(UserRole.MASTER, UserRole.ADMIN, UserRole.ADMIN_SUPER))


def _ctx(request: Request, current_user: AuthUser, **kwargs):
    return {"request": request, "current_user": current_user, **kwargs}


def _g_str(form: Any, name: str, default: str = "") -> str:
    v = form.get(name)
    if v is None or isinstance(v, UploadFile):
        return default
    if isinstance(v, (bytes, bytearray)):
        return v.decode().strip()
    return str(v).strip()


def _g_float(form: Any, name: str, default: float = 0.0) -> float:
    try:
        return float((_g_str(form, name, str(default)) or str(default)).replace(",", "."))
    except ValueError:
        return default


def _g_bool(form: Any, name: str) -> bool:
    v = form.get(name)
    if v is None or isinstance(v, UploadFile):
        return False
    s = v.decode() if isinstance(v, (bytes, bytearray)) else str(v)
    return s.lower() in ("on", "true", "1", "yes")


def _list_staff_for_work_form(db: Session) -> list[User]:
    return list(
        db.scalars(
            select(User)
            .where(
                User.is_active.is_(True),
                User.role.in_((UserRole.MASTER, UserRole.ADMIN, UserRole.ADMIN_SUPER)),
            )
            .order_by(User.display_name.asc(), User.id.asc())
        ).all()
    )


def _read_staff_form_state(form: Any) -> tuple[list[int], dict[int, str]]:
    raw: list[Any] = []
    if hasattr(form, "getlist"):
        raw = list(form.getlist("work_staff_on"))
    else:
        v = form.get("work_staff_on")
        if v is not None:
            raw = [v]

    seen: set[int] = set()
    active_ids: list[int] = []
    for x in raw:
        if isinstance(x, UploadFile):
            continue
        try:
            s = x.decode().strip() if isinstance(x, (bytes, bytearray)) else str(x).strip()
            i = int(s)
        except (ValueError, AttributeError):
            continue
        if i <= 0 or i in seen:
            continue
        seen.add(i)
        active_ids.append(i)

    share_str: dict[int, str] = {}
    for uid in active_ids:
        share_str[uid] = _g_str(form, f"work_staff_share_{uid}", "")
    return active_ids, share_str


def _parse_staff_allocations(form: Any) -> list[tuple[int, float]]:
    """Чекбоксы + доли 0..1 (до сотых). Для одного сотрудника пусто => 1.00."""
    active_ids, share_str = _read_staff_form_state(form)
    if not active_ids:
        raise ValueError("Отметьте хотя бы одного сотрудника и укажите доли (0..1).")
    if len(active_ids) == 1:
        uid = active_ids[0]
        s = (share_str.get(uid) or "").strip()
        if not s:
            return [(uid, 1.0)]
        try:
            v = float(s.replace(",", "."))
        except ValueError:
            raise ValueError("Доли сотрудников должны быть числами (0..1).")
        return [(uid, v)]

    out: list[tuple[int, float]] = []
    for uid in active_ids:
        s = (share_str.get(uid) or "").strip()
        if not s:
            raise ValueError("Для каждого отмеченного сотрудника укажите долю (0..1).")
        try:
            v = float(s.replace(",", "."))
        except ValueError:
            raise ValueError("Доли сотрудников должны быть числами (0..1).")
        out.append((uid, v))
    return out


def _resolve_staff_allocations(db: Session, allocations: list[tuple[int, float]]) -> list[tuple[int, float]]:
    total = sum(v for _, v in allocations)
    # allow tiny float noise from form; stored as Numeric(3,2) anyway
    if abs(total - 1.0) > 0.01:
        raise ValueError("Сумма долей сотрудников должна быть ровно 1.00.")
    allowed = (UserRole.MASTER, UserRole.ADMIN, UserRole.ADMIN_SUPER)
    seen: set[int] = set()
    out: list[tuple[int, float]] = []
    for uid, v in allocations:
        if v < 0 or v > 1:
            raise ValueError("Доля каждого сотрудника должна быть в диапазоне 0..1.")
        if uid in seen:
            continue
        u = db.get(User, uid)
        if not u or not u.is_active:
            raise ValueError(f"Сотрудник (ID {uid}) не найден или отключён.")
        if u.role not in allowed:
            raise ValueError("Можно выбирать только мастеров и админов студии.")
        seen.add(uid)
        out.append((uid, float(round(v, 2))))
    if len(out) != len(allocations):
        raise ValueError("Дублирование сотрудника в списке долей не допускается.")
    return out


def _studio_share_snapshot(db: Session) -> float:
    r = db.scalar(select(WorkRate).where(WorkRate.key == "studio_share", WorkRate.is_active.is_(True)))
    if not r:
        return 0.30
    try:
        return float(json.loads(r.value_json))
    except Exception:
        return 0.30


def _kind_label(k: WorkKind) -> str:
    return {
        WorkKind.KIT: "Комплект/Заготовки (поштучно)",
        WorkKind.MIX: "Смешка",
        WorkKind.RUBBER: "Хвосты/резинки",
        WorkKind.KIT_CORRECTION: "Коррекция комплекта",
        WorkKind.HAIR_EXT_PREP: "Подготовка к наращиванию волос (заглушка)",
    }[k]


@router.get("/new", response_class=HTMLResponse)
def work_new_get(
    request: Request,
    current_user: AuthUser = _STAFF,
    db: Session = Depends(get_db),
):
    return templates.TemplateResponse(
        "work_products_new.html",
        _ctx(
            request,
            current_user=current_user,
            error=None,
            fp={},
            staff=_list_staff_for_work_form(db),
            staff_on_ids=[current_user.id],
            staff_share_str={},
            default_date=date.today().isoformat(),
            kinds=[{"value": k.value, "label": _kind_label(k)} for k in WorkKind],
            scopes=[{"value": s.value, "label": ("В наличие" if s == WorkScope.IN_STOCK else "На заказ")} for s in WorkScope],
        ),
    )


@router.post("/new")
async def work_new_post(
    request: Request,
    current_user: AuthUser = _STAFF,
    db: Session = Depends(get_db),
):
    form = await request.form()
    fp = {k: _g_str(form, k) for k in form.keys() if isinstance(k, str)}
    try:
        scope_raw = (_g_str(form, "scope", "") or "").strip()
        try:
            scope = WorkScope(scope_raw)
        except ValueError:
            raise ValueError("Выберите режим: в наличие или на заказ.")

        kind_raw = (_g_str(form, "kind", "") or "").strip()
        try:
            kind = WorkKind(kind_raw)
        except ValueError:
            raise ValueError("Выберите вид работы.")

        client_id: int | None = None
        if scope == WorkScope.CUSTOM_ORDER:
            cid_raw = (_g_str(form, "client_id", "") or "").strip()
            if not cid_raw.isdigit():
                raise ValueError("Для режима «на заказ» выберите клиента.")
            client_id = int(cid_raw)
            if not db.get(Client, client_id):
                raise ValueError("Клиент не найден.")

        kanek = max(0.0, _g_float(form, "kanekalon_grams", 0.0))
        kudri = max(0.0, _g_float(form, "kudri_grams", 0.0))
        grams_total = kanek + kudri
        mix_raw = _g_str(form, "mix_source", "")
        if grams_total <= 0:
            mix_source = MixSource.NO_MIX
        else:
            try:
                mix_source = MixSource(mix_raw) if mix_raw else MixSource.NO_MIX
            except ValueError:
                mix_source = MixSource.NO_MIX

        mix_complexity: MixComplexity | None = None
        if grams_total > 0 and mix_source != MixSource.NO_MIX:
            mc_raw = _g_str(form, "mix_complexity", "")
            try:
                mix_complexity = MixComplexity(mc_raw) if mc_raw else None
            except ValueError:
                mix_complexity = None

        # participants
        if _g_bool(form, "work_use_multi_staff"):
            alloc = _resolve_staff_allocations(db, _parse_staff_allocations(form))
        else:
            alloc = [(current_user.id, 1.0)]

        # snapshots for materials
        mat_cost, k_snap, ku_snap = _materials_cost_and_snapshot(
            db, kanekalon_grams=kanek, kudri_grams=kudri
        )

        details: dict[str, Any] = {}
        if mix_complexity is not None:
            details["mix_complexity"] = mix_complexity.value

        ready_date_raw = _g_str(form, "ready_date", "")
        ready_dt = None
        if ready_date_raw:
            try:
                ready_dt = datetime.combine(date.fromisoformat(ready_date_raw), datetime.min.time())
            except ValueError:
                raise ValueError("Некорректная дата готовности (ожидается YYYY-MM-DD).")

        work = WorkForInventory(
            created_by_user_id=current_user.id,
            kind=kind,
            scope=scope,
            client_id=client_id,
            ready_date=ready_dt,
            comment=(_g_str(form, "comment", "") or "").strip() or None,
            kanekalon_grams=kanek,
            kudri_grams=kudri,
            mix_source=mix_source,
            kanekalon_price_per_gram_at_time=k_snap,
            kudri_price_per_gram_at_time=ku_snap,
            materials_cost_total=mat_cost,
            studio_share_snapshot=_studio_share_snapshot(db),
            rates_snapshot_json=None,
            details_json=json.dumps(details, ensure_ascii=False) if details else None,
            # деньги пока нули — считаем в следующих подшагах
            extra_costs_amount=0.0,
            cost_total_amount=0.0,
            master_profit_amount=0.0,
            studio_profit_amount=0.0,
            profit_total_amount=0.0,
        )
        db.add(work)
        db.flush()

        for uid, share in alloc:
            db.add(
                WorkForInventoryStaff(
                    work_id=work.id,
                    user_id=uid,
                    share=share,
                    master_profit_amount=0.0,
                    details_json=None,
                )
            )

        db.commit()
        return RedirectResponse(url="/sales/work?msg=saved", status_code=303)
    except ValueError as exc:
        staff = _list_staff_for_work_form(db)
        on_ids, share_str = _read_staff_form_state(form)
        if not on_ids:
            on_ids = [current_user.id]
        return templates.TemplateResponse(
            "work_products_new.html",
            _ctx(
                request,
                current_user=current_user,
                error=str(exc),
                fp=fp,
                staff=staff,
                staff_on_ids=on_ids,
                staff_share_str=share_str,
                default_date=date.today().isoformat(),
                kinds=[{"value": k.value, "label": _kind_label(k)} for k in WorkKind],
                scopes=[
                    {"value": s.value, "label": ("В наличие" if s == WorkScope.IN_STOCK else "На заказ")}
                    for s in WorkScope
                ],
            ),
            status_code=400,
        )


@router.get("", response_class=HTMLResponse)
def work_list(
    request: Request,
    current_user: AuthUser = _STAFF,
    db: Session = Depends(get_db),
):
    msg = request.query_params.get("msg")
    stmt = (
        select(WorkForInventory)
        .options(
            selectinload(WorkForInventory.client),
            selectinload(WorkForInventory.staff_rows).selectinload(WorkForInventoryStaff.user),
        )
        .order_by(WorkForInventory.id.desc())
        .limit(100)
    )
    rows = list(db.scalars(stmt).all())
    return templates.TemplateResponse(
        "work_products_list.html",
        _ctx(request, current_user=current_user, rows=rows, msg=msg),
    )

