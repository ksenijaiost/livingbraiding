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
    Kit,
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


def _list_masters_for_work_form(db: Session) -> list[User]:
    return list(
        db.scalars(
            select(User)
            .where(User.is_active.is_(True), User.role == UserRole.MASTER)
            .order_by(User.display_name.asc(), User.id.asc())
        ).all()
    )


def _read_kit_master_on_ids(form: Any) -> list[int]:
    raw: list[Any] = []
    if hasattr(form, "getlist"):
        raw = list(form.getlist("kit_master_on"))
    else:
        v = form.get("kit_master_on")
        if v is not None:
            raw = [v]

    seen: set[int] = set()
    out: list[int] = []
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
        out.append(i)
    return out


def _kit_qty_prefill_from_form(form: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    for k in form.keys():
        if not isinstance(k, str) or not k.startswith("kit_qty_"):
            continue
        out[k] = _g_str(form, k, "0")
    return out


def _kit_table_state_json(
    current_user: AuthUser, masters: list[User], kit_qty_prefill: dict[str, str]
) -> str:
    return json.dumps(
        {
            "currentUserId": current_user.id,
            "masters": [{"id": u.id, "name": u.display_name} for u in masters],
            "seItems": [{"key": k, "label": lbl} for k, lbl in _kit_se_items()],
            "deItems": [{"key": k, "label": lbl} for k, lbl in _kit_de_items()],
            "prefill": kit_qty_prefill,
        },
        ensure_ascii=False,
    )


def _alloc_equal_shares_for_masters(db: Session, user_ids: list[int]) -> list[tuple[int, float]]:
    """Доли 1/n для строк work_for_inventory_staff; сумма ≈ 1.00. ЗП по заготовкам — в master_profit_amount."""
    if not user_ids:
        raise ValueError("Нет мастеров для записи работы.")
    seen: set[int] = set()
    ordered: list[int] = []
    for uid in user_ids:
        if uid in seen:
            continue
        seen.add(uid)
        ordered.append(uid)
        u = db.get(User, uid)
        if not u or not u.is_active:
            raise ValueError(f"Мастер (ID {uid}) не найден или отключён.")
        if u.role != UserRole.MASTER:
            raise ValueError("В комплекте участвуют только мастера.")
    n = len(ordered)
    if n == 1:
        return [(ordered[0], 1.0)]
    base = round(1.0 / n, 2)
    shares: list[float] = []
    acc = 0.0
    for _ in range(n - 1):
        shares.append(base)
        acc += base
    last = round(1.0 - acc, 2)
    if last < 0:
        last = 0.0
    shares.append(last)
    return [(ordered[i], shares[i]) for i in range(n)]


def _studio_share_snapshot(db: Session) -> float:
    r = db.scalar(select(WorkRate).where(WorkRate.key == "studio_share", WorkRate.is_active.is_(True)))
    if not r:
        return 0.30
    try:
        return float(json.loads(r.value_json))
    except Exception:
        return 0.30


def _wr_float(db: Session, key: str, default: float) -> float:
    r = db.scalar(select(WorkRate).where(WorkRate.key == key, WorkRate.is_active.is_(True)))
    if not r:
        return default
    try:
        return float(json.loads(r.value_json))
    except Exception:
        return default


def _rubber_type_items() -> list[tuple[str, str]]:
    return [
        ("TAIL_ELASTIC", "Хвост на резинке"),
        ("TAIL_CRAB", "Хвост на крабе"),
        ("TAIL_NET", "Хвост на сетке"),
        ("BRAIDS_ELASTIC", "Косы на резинке"),
    ]


def _rubber_variant_items() -> list[tuple[str, str]]:
    return [
        ("KIDS_SHORT", "Детская короткая"),
        ("KIDS_LONG", "Детская длинная"),
        ("SHOULDER_SOLID", "До плеч однотонная"),
        ("SHOULDER_OMBRE", "До плеч омбре"),
        ("WAIST_SOLID", "До талии однотон"),
        ("WAIST_OMBRE", "До талии омбре"),
        ("BUTT_SOLID", "До попы однотонная"),
        ("BUTT_OMBRE", "До попы омбре"),
        ("LOWER", "Ниже"),
    ]


def _rubber_variant_multiplier_defaults() -> dict[str, float]:
    # Conservative defaults: keep 1.0 everywhere, can be overridden via WorkRate key
    # "rubber_variant_multipliers" (JSON object).
    return {k: 1.0 for k, _ in _rubber_variant_items()}


def _rubber_variant_multiplier(db: Session, variant_key: str) -> float:
    r = db.scalar(select(WorkRate).where(WorkRate.key == "rubber_variant_multipliers", WorkRate.is_active.is_(True)))
    multipliers = _rubber_variant_multiplier_defaults()
    if r:
        try:
            payload = json.loads(r.value_json)
            if isinstance(payload, dict):
                for k, v in payload.items():
                    try:
                        multipliers[str(k)] = float(v)
                    except Exception:
                        continue
        except Exception:
            pass
    m = float(multipliers.get(variant_key, 1.0))
    if m <= 0:
        return 1.0
    return m


def _kind_label(k: WorkKind) -> str:
    return {
        WorkKind.KIT: "Комплект/Заготовки (поштучно)",
        WorkKind.MIX: "Смешка",
        WorkKind.RUBBER: "Хвосты/резинки",
        WorkKind.KIT_CORRECTION: "Коррекция комплекта",
        WorkKind.HAIR_EXT_PREP: "Подготовка к наращиванию волос (заглушка)",
    }[k]


def _kit_se_items() -> list[tuple[str, str]]:
    return [
        ("SE_BRAID_SHORT", "SE: коса короткая"),
        ("SE_BRAID_LONG", "SE: коса длинная"),
        ("SE_BRAID_FREE_TIP", "SE: коса свободный кончик"),
        ("SE_TIP_ADDON", "SE: доплёт кончиков"),
        ("SE_TRIM_SHORT", "SE: стрижка короткой косы"),
        ("SE_TRIM_LONG", "SE: стрижка длинной косы"),
    ]


def _kit_de_items() -> list[tuple[str, str]]:
    return [
        ("DE_BRAID_SHORT", "DE: коса короткая"),
        ("DE_BRAID_LONG", "DE: коса длинная"),
        ("DE_BRAID_NEW_FMT", "DE: коса новый формат"),
        ("DE_CURL", "DE: кудря"),
        ("DE_DREAD_FREE_TIP", "DE: дред свободный кончик"),
        ("DE_DREAD_SHORT", "DE: дред короткий"),
        ("DE_DREAD_LONG", "DE: дред длинный"),
        ("DE_TRIM", "DE: стрижка"),
    ]


def _kit_rates_defaults() -> dict[str, dict[str, Any]]:
    """
    Ставки ЗП за 1 шт по видам (по умолчанию). Пороговые цены — только для конкретного вида.
    Значения можно переопределить позже через work_rates (следующий шаг расширит настройки).
    """
    return {
        "SE": {
            "SE_BRAID_SHORT": {"base": 12.5, "threshold_qty": 140, "threshold_rate": 11.0},
            "SE_BRAID_LONG": {"base": 15.0, "threshold_qty": 120, "threshold_rate": 13.5},
            "SE_BRAID_FREE_TIP": {"base": 11.0},
            "SE_TIP_ADDON": {"base": 5.0},
            "SE_TRIM_SHORT": {"base": 2.0},
            "SE_TRIM_LONG": {"base": 2.5},
        },
        "DE": {
            "DE_BRAID_SHORT": {"base": 25.0},
            "DE_BRAID_LONG": {"base": 30.0},
            "DE_BRAID_NEW_FMT": {"base": 35.0},
            "DE_CURL": {"base": 25.0},
            "DE_DREAD_FREE_TIP": {"base": 35.0},
            "DE_DREAD_SHORT": {"base": 40.0},
            "DE_DREAD_LONG": {"base": 50.0},
            "DE_TRIM": {"base": 5.0},
        },
    }


def _kit_rate_for_item(rates: dict[str, dict[str, Any]], item_key: str, qty_total: int) -> float:
    for group in ("SE", "DE"):
        if item_key in rates.get(group, {}):
            cfg = rates[group][item_key]
            base = float(cfg.get("base") or 0.0)
            th_qty = int(cfg.get("threshold_qty") or 0)
            th_rate = float(cfg.get("threshold_rate") or 0.0)
            if th_qty > 0 and qty_total >= th_qty and th_rate > 0:
                return th_rate
            return base
    return 0.0


@router.get("/new", response_class=HTMLResponse)
def work_new_get(
    request: Request,
    current_user: AuthUser = _STAFF,
    db: Session = Depends(get_db),
):
    masters = _list_masters_for_work_form(db)
    return templates.TemplateResponse(
        "work_products_new.html",
        _ctx(
            request,
            current_user=current_user,
            error=None,
            fp={},
            masters=masters,
            kit_master_on_ids=[],
            kit_table_state_json=_kit_table_state_json(current_user, masters, {}),
            default_date=date.today().isoformat(),
            kinds=[{"value": k.value, "label": _kind_label(k)} for k in WorkKind],
            scopes=[{"value": s.value, "label": ("В наличие" if s == WorkScope.IN_STOCK else "На заказ")} for s in WorkScope],
            kit_se_items=_kit_se_items(),
            kit_de_items=_kit_de_items(),
            rubber_types=[{"value": v, "label": l} for v, l in _rubber_type_items()],
            rubber_variants=[{"value": v, "label": l} for v, l in _rubber_variant_items()],
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

        # snapshots for materials
        mat_cost, k_snap, ku_snap = _materials_cost_and_snapshot(
            db, kanekalon_grams=kanek, kudri_grams=kudri
        )

        details: dict[str, Any] = {}
        if mix_complexity is not None:
            details["mix_complexity"] = mix_complexity.value

        extra_costs_amount = max(0.0, _g_float(form, "extra_costs_amount", 0.0))

        alloc: list[tuple[int, float]]
        kit_staff_ids: list[int] = []
        rubber_type = ""
        rubber_variant = ""
        rubber_qty = 1
        # KIT: parse blanks + compute master/studio profit
        kit_rates = _kit_rates_defaults()
        kit_totals: dict[str, int] = {}
        kit_by_staff: dict[int, dict[str, int]] = {}
        kit_pieces_total = 0
        kit_blank_type_se = _g_bool(form, "kit_type_se")
        kit_blank_type_de = _g_bool(form, "kit_type_de")
        kit_use_multi_masters = _g_bool(form, "kit_use_multi_masters")
        if kind == WorkKind.KIT:
            if not kit_blank_type_se and not kit_blank_type_de:
                raise ValueError("Для комплекта выберите тип заготовок: SE и/или DE.")
            if kit_use_multi_masters:
                kit_staff_ids = _read_kit_master_on_ids(form)
                if not kit_staff_ids:
                    raise ValueError(
                        "Для таблицы комплекта отметьте мастеров или снимите «Несколько мастеров (комплект)»."
                    )
            else:
                kit_staff_ids = [current_user.id]
                cu = db.get(User, current_user.id)
                if not cu or cu.role != UserRole.MASTER:
                    raise ValueError(
                        "Режим одной колонки доступен только при входе под мастером; "
                        "иначе отметьте «Несколько мастеров (комплект)» и выберите мастеров."
                    )
            all_items = []
            if kit_blank_type_se:
                all_items.extend(_kit_se_items())
            if kit_blank_type_de:
                all_items.extend(_kit_de_items())
            any_qty = False
            for uid in kit_staff_ids:
                per: dict[str, int] = {}
                for item_key, _ in all_items:
                    raw = _g_str(form, f"kit_qty_{uid}_{item_key}", "0")
                    try:
                        q = int(raw or "0")
                    except ValueError:
                        q = 0
                    q = max(0, q)
                    if q > 0:
                        any_qty = True
                    per[item_key] = q
                    kit_totals[item_key] = kit_totals.get(item_key, 0) + q
                kit_by_staff[uid] = per
            if not any_qty:
                raise ValueError("Для комплекта укажите хотя бы одно количество заготовок.")
            kit_pieces_total = sum(kit_totals.values())
            details["kit"] = {
                "blank_type_se": kit_blank_type_se,
                "blank_type_de": kit_blank_type_de,
                "totals": kit_totals,
                "by_staff": kit_by_staff,
            }
            alloc = _alloc_equal_shares_for_masters(db, kit_staff_ids)
        elif kind == WorkKind.RUBBER:
            rubber_type = (_g_str(form, "rubber_type", "") or "").strip()
            if rubber_type not in {k for k, _ in _rubber_type_items()}:
                raise ValueError("Для «Хвосты/резинки» выберите тип.")
            rubber_variant = (_g_str(form, "rubber_variant", "") or "").strip()
            if rubber_variant not in {k for k, _ in _rubber_variant_items()}:
                raise ValueError("Для «Хвосты/резинки» выберите вариант длины/цвета.")

            if rubber_type == "TAIL_ELASTIC":
                rubber_qty = int(_g_float(form, "rubber_attach_qty", 0))
                if rubber_qty <= 0:
                    raise ValueError("Укажите количество креплений для хвоста на резинке (целое число).")
            elif rubber_type == "BRAIDS_ELASTIC":
                rubber_qty = int(_g_float(form, "rubber_braids_qty", 0))
                if rubber_qty <= 0:
                    raise ValueError("Укажите количество кос для кос на резинке (целое число).")
            else:
                rubber_qty = 1

            details["rubber"] = {
                "type": rubber_type,
                "variant": rubber_variant,
                "qty": rubber_qty,
            }
            alloc = [(current_user.id, 1.0)]
        else:
            alloc = [(current_user.id, 1.0)]

        ready_date_raw = _g_str(form, "ready_date", "")
        ready_dt = None
        if ready_date_raw:
            try:
                ready_dt = datetime.combine(date.fromisoformat(ready_date_raw), datetime.min.time())
            except ValueError:
                raise ValueError("Некорректная дата готовности (ожидается YYYY-MM-DD).")

        # Compute profits (MVP for 6.3.3: KIT uses piece rates; other kinds stay 0 for now)
        staff_master_profit: dict[int, float] = {uid: 0.0 for uid, _ in alloc}
        if kind == WorkKind.KIT:
            for item_key, total_qty in kit_totals.items():
                rate = _kit_rate_for_item(kit_rates, item_key, total_qty)
                if rate <= 0:
                    continue
                for uid in kit_staff_ids:
                    q = int(kit_by_staff.get(uid, {}).get(item_key, 0))
                    if q > 0:
                        staff_master_profit[uid] += rate * q
        elif kind == WorkKind.RUBBER:
            m = _rubber_variant_multiplier(db, rubber_variant)
            if rubber_type == "TAIL_ELASTIC":
                per = _wr_float(db, "rubber_tail_elastic_per_attach", 10.0)
                staff_master_profit[current_user.id] = float(per) * float(rubber_qty) * m
            elif rubber_type == "TAIL_CRAB":
                base = _wr_float(db, "rubber_tail_crab", 500.0)
                staff_master_profit[current_user.id] = float(base) * m
            elif rubber_type == "TAIL_NET":
                base = _wr_float(db, "rubber_tail_net", 550.0)
                staff_master_profit[current_user.id] = float(base) * m
            elif rubber_type == "BRAIDS_ELASTIC":
                per = _wr_float(db, "rubber_braids_elastic_per_braid", 15.0)
                staff_master_profit[current_user.id] = float(per) * float(rubber_qty) * m

        master_total = float(sum(staff_master_profit.values()))
        studio_share = _studio_share_snapshot(db)
        studio_total = 0.0
        if studio_share > 0 and studio_share < 1 and master_total > 0:
            studio_total = master_total * (studio_share / (1.0 - studio_share))
        profit_total = master_total + studio_total
        cost_total_amount = mat_cost + extra_costs_amount

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
            studio_share_snapshot=studio_share,
            rates_snapshot_json=None,
            details_json=json.dumps(details, ensure_ascii=False) if details else None,
            extra_costs_amount=extra_costs_amount,
            cost_total_amount=cost_total_amount,
            master_profit_amount=master_total,
            studio_profit_amount=studio_total,
            profit_total_amount=profit_total,
        )
        db.add(work)
        db.flush()

        for uid, share in alloc:
            db.add(
                WorkForInventoryStaff(
                    work_id=work.id,
                    user_id=uid,
                    share=share,
                    master_profit_amount=float(staff_master_profit.get(uid, 0.0)),
                    details_json=None,
                )
            )

        # Create Kit in stock for IN_STOCK + KIT
        if kind == WorkKind.KIT and scope == WorkScope.IN_STOCK:
            sku = (_g_str(form, "kit_sku", "") or "").strip()
            title = (_g_str(form, "kit_title", "") or "").strip()
            stock_price_total = max(0.0, _g_float(form, "kit_stock_price_total", 0.0))
            if not sku:
                raise ValueError("Для «в наличие» укажите артикул комплекта.")
            if not title:
                raise ValueError("Для «в наличие» укажите название комплекта.")
            if stock_price_total <= 0:
                raise ValueError("Для «в наличие» укажите цену комплекта на складе (всего), ₽.")
            if db.scalar(select(Kit.id).where(Kit.sku == sku)):
                raise ValueError("Комплект с таким артикулом уже есть — укажите другой.")
            kit = Kit(
                sku=sku[:80],
                title=title[:200],
                description=None,
                is_active=True,
                pieces_total=kit_pieces_total,
                pieces_available=kit_pieces_total,
                blank_type_se=kit_blank_type_se,
                blank_type_de=kit_blank_type_de,
                weight_grams=None,
                length_cm=None,
                has_decorations=False,
                materials_text=None,
                color_text=None,
                blanks_kinds_text=None,
                notes=None,
                stock_price_total=stock_price_total,
                cost_total=cost_total_amount,
                author_cost_total=master_total,
                created_at=datetime.utcnow(),
                is_in_stock=True,
                is_archived=False,
            )
            db.add(kit)

        db.commit()
        return RedirectResponse(url="/sales/work?msg=saved", status_code=303)
    except ValueError as exc:
        masters = _list_masters_for_work_form(db)
        kit_master_on_ids = _read_kit_master_on_ids(form)
        kit_prefill = _kit_qty_prefill_from_form(form)
        return templates.TemplateResponse(
            "work_products_new.html",
            _ctx(
                request,
                current_user=current_user,
                error=str(exc),
                fp=fp,
                masters=masters,
                kit_master_on_ids=kit_master_on_ids,
                kit_table_state_json=_kit_table_state_json(current_user, masters, kit_prefill),
                default_date=date.today().isoformat(),
                kinds=[{"value": k.value, "label": _kind_label(k)} for k in WorkKind],
                scopes=[
                    {"value": s.value, "label": ("В наличие" if s == WorkScope.IN_STOCK else "На заказ")}
                    for s in WorkScope
                ],
                kit_se_items=_kit_se_items(),
                kit_de_items=_kit_de_items(),
                rubber_types=[{"value": v, "label": l} for v, l in _rubber_type_items()],
                rubber_variants=[{"value": v, "label": l} for v, l in _rubber_variant_items()],
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

