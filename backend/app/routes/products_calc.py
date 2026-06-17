from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.orm import Session

from app.auth import AuthUser, require_role
from app.db.models import (
    BookingKind,
    MixComplexity,
    MixSource,
    ProductSaleKind,
    Service,
    UserRole,
    WorkKind,
    WorkScope,
    MaterialPriceCurrent,
    MaterialType,
)
from app.db.session import get_db
from app.kit_inlay_visit import list_master_visit_services_catalog
from app.work_products import _kit_client_stock_price_total, _rubber_type_items, _zakaz_subcategory_services_map
from app.work_products import _rubber_service_name
from app.work_products import _kit_se_items, _kit_de_items
from app.work_products_compute import (
    CORR_SVC_HOURLY,
    CORR_SVC_TRIM,
    CORR_SVC_WASH_WITH,
    corr_hourly_pay_units,
    corr_wash_catalog_name,
    compute_work_financials,
)
from app.zakaz_blanks import zakaz_blank_def_by_key
from app.webui import templates, ctx as _ctx


router = APIRouter()


def _material_cost_total(db: Session, *, kanekalon_grams: float, kudri_grams: float) -> float:
    pk = db.get(MaterialPriceCurrent, MaterialType.KANEKALON)
    pku = db.get(MaterialPriceCurrent, MaterialType.KUDRI)
    kpg = float(pk.price_per_gram) if pk else 0.0
    kupg = float(pku.price_per_gram) if pku else 0.0
    kan = max(0.0, float(kanekalon_grams or 0.0))
    ku = max(0.0, float(kudri_grams or 0.0))
    return float(kan) * float(kpg) + float(ku) * float(kupg)


@router.get("/products-calc", response_class=HTMLResponse)
def products_calc_view(
    request: Request,
    current_user: AuthUser = Depends(require_role(UserRole.MASTER, UserRole.ADMIN, UserRole.ADMIN_SUPER)),
    db: Session = Depends(get_db),
):
    service_catalog = list_master_visit_services_catalog(db)
    kit_services: list[dict[str, Any]] = []
    tail_attach_services: list[dict[str, Any]] = []
    service_price_meta: dict[int, dict[str, float]] = {}

    def _service_price_range(svc: dict[str, Any]) -> tuple[float | None, float | None]:
        vals: list[tuple[float | None, float | None]] = [
            (svc.get("price_junior_from"), svc.get("price_junior_to")),
            (svc.get("price_middle_from"), svc.get("price_middle_to")),
            (svc.get("price_senior_from"), svc.get("price_senior_to")),
        ]
        lows: list[float] = []
        highs: list[float] = []
        for fr, to in vals:
            if fr is not None:
                lows.append(float(fr))
            if to is not None:
                highs.append(float(to))
        if not highs and lows:
            highs = list(lows)
        if not lows and highs:
            lows = list(highs)
        return (min(lows) if lows else None, max(highs) if highs else None)

    for c in service_catalog:
        for sc in c.get("subcategories") or []:
            for s in sc.get("services") or []:
                sid = int(s.get("id") or 0)
                if sid <= 0:
                    continue
                label = f"{c.get('name')} → {sc.get('name')} → {s.get('name')}"
                lo, hi = _service_price_range(s)
                if lo is not None or hi is not None:
                    service_price_meta[sid] = {"min": float(lo if lo is not None else hi or 0.0), "max": float(hi if hi is not None else lo or 0.0)}
                if bool(s.get("requires_kit_block")):
                    kit_services.append({"id": sid, "label": label})
                if bool(s.get("requires_tail_block")):
                    tail_attach_services.append({"id": sid, "label": label})
    kit_services = sorted(kit_services, key=lambda x: x["label"])
    tail_attach_services = sorted(tail_attach_services, key=lambda x: x["label"])

    return templates.TemplateResponse(
        "products_calc.html",
        _ctx(
            request,
            current_user=current_user,
            rubber_types=[{"value": v, "label": l} for v, l in _rubber_type_items()],
            visit_services_with_kit=kit_services,
            visit_services_tail_attach=tail_attach_services,
            kit_se_items=_kit_se_items(),
            kit_de_items=_kit_de_items(),
            service_price_meta_json=json.dumps(service_price_meta, ensure_ascii=False),
        ),
    )


@router.post("/api/products-calc")
async def api_products_calc(
    request: Request,
    current_user: AuthUser = Depends(require_role(UserRole.MASTER, UserRole.ADMIN, UserRole.ADMIN_SUPER)),
    db: Session = Depends(get_db),
):
    blank_by_key = zakaz_blank_def_by_key()
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "Некорректный JSON."}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"error": "Некорректный формат запроса."}, status_code=400)

    kind_raw = str(payload.get("kind") or "").strip().upper() or "KIT"
    if kind_raw not in ("KIT", "KIT_CORRECTION", "RUBBER"):
        return JSONResponse({"error": "Некорректный тип расчёта."}, status_code=400)

    def _f(name: str, d: float = 0.0) -> float:
        try:
            return float(payload.get(name) if payload.get(name) is not None else d)
        except Exception:
            return d

    def _i(name: str, d: int = 0) -> int:
        try:
            return int(float(payload.get(name) if payload.get(name) is not None else d))
        except Exception:
            return d

    def _b(name: str) -> bool:
        v = payload.get(name)
        if isinstance(v, bool):
            return v
        s = str(v or "").strip().lower()
        return s in ("1", "true", "on", "yes")

    kan = max(0.0, _f("kanekalon_grams", 0.0))
    kud = max(0.0, _f("kudri_grams", 0.0))
    grams_total = float(kan) + float(kud)
    mat_cost = _material_cost_total(db, kanekalon_grams=kan, kudri_grams=kud)

    mix_source_raw = str(payload.get("mix_source") or "").strip().upper()
    mix_complexity_raw = str(payload.get("mix_complexity") or "").strip().upper()
    if mix_source_raw in ("", "NO_MIX") or grams_total <= 0:
        mix_source = MixSource.NO_MIX
        mix_complexity = None
    else:
        try:
            mix_source = MixSource(mix_source_raw)
        except ValueError:
            mix_source = MixSource.NO_MIX
        mix_complexity_raw = {"SIMPLE": "STANDARD", "MEDIUM": "KANEK", "HARD": "THERMO"}.get(mix_complexity_raw, mix_complexity_raw)
        try:
            mix_complexity = MixComplexity(mix_complexity_raw) if mix_complexity_raw else None
        except ValueError:
            mix_complexity = None

    extra_costs_amount = max(0.0, _f("extra_costs_amount", 0.0))

    def _service_price_range_row(svc: Service) -> tuple[float | None, float | None]:
        vals: list[tuple[float | None, float | None]] = [
            (svc.price_junior_from, svc.price_junior_to),
            (svc.price_middle_from, svc.price_middle_to),
            (svc.price_senior_from, svc.price_senior_to),
        ]
        lows: list[float] = []
        highs: list[float] = []
        for fr, to in vals:
            if fr is not None:
                lows.append(float(fr))
            if to is not None:
                highs.append(float(to))
        if not highs and lows:
            highs = list(lows)
        if not lows and highs:
            lows = list(highs)
        return (min(lows) if lows else None, max(highs) if highs else None)

    try:
        alloc = [(int(current_user.id), 1.0)]
        scope = WorkScope.IN_STOCK
        kit_totals: dict[str, int] = {}
        client_min: float | None = None
        client_max: float | None = None

        if kind_raw == "KIT":
            tt = payload.get("kit_totals") or {}
            if isinstance(tt, dict):
                for k, v in tt.items():
                    try:
                        qv = int(v)
                    except Exception:
                        qv = 0
                    if qv > 0:
                        kit_totals[str(k)] = qv
            kit_staff_ids = [int(current_user.id)]
            kit_by_staff = {int(current_user.id): dict(kit_totals)}
            fin = compute_work_financials(
                db,
                kind=WorkKind.KIT,
                scope=scope,
                alloc=alloc,
                current_user_id=int(current_user.id),
                mat_cost=float(mat_cost),
                kit_totals=kit_totals,
                kit_staff_ids=kit_staff_ids,
                kit_by_staff=kit_by_staff,
                mix_source=mix_source,
                mix_complexity=mix_complexity,
                grams_total=float(grams_total),
                rubber_type="",
                rubber_qty=1,
                other_catalog_product_id=0,
                other_qty=1,
                corr_trim_qty=0,
                corr_hourly_hours=0.0,
                corr_hourly_avg=False,
                corr_wash=False,
                corr_circle=False,
                corr_steam=False,
            )
            try:
                client_total = _kit_client_stock_price_total(db, kit_totals=kit_totals, extra_costs_amount=float(extra_costs_amount))
            except Exception:
                client_total = None
            if client_total is not None:
                client_min = float(client_total)
                client_max = float(client_total)
            quoted = f"{client_total:.0f}" if client_total is not None else ""
            client_hint = f"Цена для клиента (по прайсу): {client_total:.0f} ₽" if client_total is not None else "Цена для клиента: —"

            client_piece_totals: dict[str, int] = {}
            tech_totals: dict[str, int] = {}
            for k, q in kit_totals.items():
                q = int(q or 0)
                if q <= 0:
                    continue
                d = blank_by_key.get(str(k))
                if d and bool(d.exclude_from_inventory_piece_count):
                    tech_totals[str(k)] = q
                else:
                    client_piece_totals[str(k)] = q
            pieces = int(sum(int(v) for v in client_piece_totals.values()))

            def _fmt_row(key: str, qty: int) -> str:
                d = blank_by_key.get(key)
                title = d.display_name if d else key
                return f"{title} — {int(qty)} шт"

            parts: list[str] = [f"Комплект на заказ: {pieces} шт"]
            if client_piece_totals:
                items = [_fmt_row(key, int(client_piece_totals[key])) for key in sorted(client_piece_totals.keys())]
                parts.append("Состав: " + ", ".join(items))
            if tech_totals:
                items = [_fmt_row(key, int(tech_totals[key])) for key in sorted(tech_totals.keys())]
                parts.append("Техн (не влияет на цену для клиента): " + ", ".join(items))
            desc = "; ".join([p for p in parts if str(p).strip()])[:400]
            prefill_sale = {
                "kind": BookingKind.PRODUCT_SALE.value,
                "product_kind": ProductSaleKind.KIT.value,
                "sale_kit_mode": "ORDER",
                "sale_order_blanks_qty": str(pieces or ""),
                "sale_order_blanks_desc": desc,
            }
            svc_id = str(payload.get("visit_service_id") or "").strip()
            prefill_visit = {
                "kind": BookingKind.VISIT.value,
                "service_id": svc_id,
                "visit_kit_mode": "ORDER",
                "visit_order_blanks_qty": str(pieces or ""),
                "visit_order_blanks_desc": desc,
            }

        elif kind_raw == "KIT_CORRECTION":
            tqty = max(0, _i("corr_trim_qty", 0))
            hh_raw = max(0.0, _f("corr_hourly_hours", 0.0))
            h_avg = _b("corr_hourly_avg")
            if h_avg and hh_raw > 0:
                h_avg = False
            fin = compute_work_financials(
                db,
                kind=WorkKind.KIT_CORRECTION,
                scope=scope,
                alloc=alloc,
                current_user_id=int(current_user.id),
                mat_cost=float(mat_cost),
                kit_totals={},
                kit_staff_ids=[],
                kit_by_staff={},
                mix_source=MixSource.NO_MIX,
                mix_complexity=None,
                grams_total=float(grams_total),
                rubber_type="",
                rubber_qty=1,
                other_catalog_product_id=0,
                other_qty=1,
                corr_trim_qty=tqty,
                corr_hourly_hours=hh_raw,
                corr_hourly_avg=h_avg,
                corr_wash=_b("corr_wash"),
                corr_circle=_b("corr_circle"),
                corr_steam=_b("corr_steam"),
            )
            corr_map = _zakaz_subcategory_services_map(db, "Коррекция комплекта")

            def _cp(name: str) -> float:
                row = corr_map.get(name) or {}
                v = row.get("client_price")
                return float(v) if v is not None else 0.0

            wash_nm = corr_wash_catalog_name(trim_qty=tqty, hourly_hours=hh_raw, hourly_avg=h_avg)
            wash_hint = ""
            if _b("corr_wash"):
                wash_hint = "Стирка: выбран вариант «с коррекцией»." if wash_nm == CORR_SVC_WASH_WITH else "Стирка: выбран вариант «без коррекции»."

            client_min = 0.0
            client_max = 0.0
            any_price = False
            if tqty:
                p = _cp(CORR_SVC_TRIM) * float(tqty)
                client_min += p
                client_max += p
                any_price = True
            hpu = corr_hourly_pay_units(hourly_hours=hh_raw, hourly_avg=h_avg)
            cp_h = _cp(CORR_SVC_HOURLY)
            if h_avg and hh_raw <= 0 and cp_h > 0:
                client_min += cp_h * 1.0
                client_max += cp_h * 4.0
                any_price = True
            elif hpu > 0 and cp_h > 0:
                both = cp_h * hpu
                client_min += both
                client_max += both
                any_price = True
            if _b("corr_circle"):
                p = _cp("Одевание на круг")
                client_min += p
                client_max += p
                any_price = True
            if _b("corr_wash"):
                p = _cp(wash_nm)
                client_min += p
                client_max += p
                any_price = True
            if _b("corr_steam"):
                p = _cp("Отпаривание")
                client_min += p
                client_max += p
                any_price = True

            if any_price and client_min > 0:
                suffix = (" " + wash_hint) if wash_hint else ""
                if abs(client_min - client_max) < 0.0001:
                    quoted = f"{client_min:.0f}"
                    client_hint = f"Цена для клиента (по прайсу): {client_min:.0f} ₽.{suffix}"
                else:
                    quoted = f"{client_min:.0f}–{client_max:.0f}"
                    client_hint = f"Цена для клиента (по прайсу): {client_min:.0f}–{client_max:.0f} ₽.{suffix}"
            else:
                quoted = ""
                client_hint = ("Цена для клиента: — " + wash_hint) if wash_hint else "Цена для клиента: —"

            svc_id = str(payload.get("visit_service_id") or "").strip()
            prefill_sale = {"kind": BookingKind.PRODUCT_SALE.value, "product_kind": ProductSaleKind.OTHER.value, "sale_rubber_desc": "Коррекция комплекта"}
            prefill_visit = {
                "kind": BookingKind.VISIT.value,
                "service_id": svc_id,
                "visit_kit_mode": "OWN",
                "visit_own_need_correction": "1",
                "corr_trim_qty": str(tqty),
                "corr_hourly_hours": str(hh_raw) if hh_raw > 0 else "",
                "corr_wash": "1" if _b("corr_wash") else "",
                "corr_steam": "1" if _b("corr_steam") else "",
                "corr_circle": "1" if _b("corr_circle") else "",
            }
            if any_price:
                client_min = float(client_min)
                client_max = float(client_max)
            else:
                client_min = None
                client_max = None

        else:
            rubber_type = str(payload.get("rubber_type") or "TAIL_ELASTIC").strip().upper()
            rubber_allowed = {k for k, _ in _rubber_type_items()}
            if rubber_type not in rubber_allowed:
                rubber_type = "TAIL_ELASTIC"
            qty = 1
            if rubber_type == "TAIL_ELASTIC":
                qty = max(1, _i("rubber_attach_qty", 1))
            elif rubber_type == "BRAIDS_ELASTIC":
                qty = max(1, _i("rubber_braids_qty", 1))
            fin = compute_work_financials(
                db,
                kind=WorkKind.RUBBER,
                scope=scope,
                alloc=alloc,
                current_user_id=int(current_user.id),
                mat_cost=float(mat_cost),
                kit_totals={},
                kit_staff_ids=[],
                kit_by_staff={},
                mix_source=MixSource.NO_MIX,
                mix_complexity=None,
                grams_total=float(grams_total),
                rubber_type=rubber_type,
                rubber_qty=int(qty),
                other_catalog_product_id=0,
                other_qty=1,
                corr_trim_qty=0,
                corr_hourly_hours=0.0,
                corr_hourly_avg=False,
                corr_wash=False,
                corr_circle=False,
                corr_steam=False,
            )
            rub_map = _zakaz_subcategory_services_map(db, "Хвосты/резинки")
            svc_name = _rubber_service_name(rubber_type)
            row = rub_map.get(svc_name) or {}
            cp = row.get("client_price")
            if cp is not None:
                client_total = float(cp) * float(qty)
                client_min = float(client_total)
                client_max = float(client_total)
                quoted = f"{client_total:.0f}"
                client_hint = f"Цена для клиента (по прайсу): {client_total:.0f} ₽"
            else:
                quoted = ""
                client_hint = "Цена для клиента: —"
            prefill_sale = {
                "kind": BookingKind.PRODUCT_SALE.value,
                "product_kind": ProductSaleKind.RUBBER.value,
                "sale_rubber_mode": "ORDER",
                "sale_rubber_type": rubber_type,
                "sale_rubber_attach_qty": str(qty) if rubber_type == "TAIL_ELASTIC" else "",
                "sale_rubber_braids_qty": str(qty) if rubber_type == "BRAIDS_ELASTIC" else "",
            }
            svc_id = str(payload.get("visit_service_id") or "").strip()
            prefill_visit = {"kind": BookingKind.VISIT.value, "service_id": svc_id}

    except Exception as e:
        msg = str(e).strip() or "Ошибка расчёта."
        return JSONResponse({"error": msg}, status_code=400)

    svc_id_raw = str(payload.get("visit_service_id") or "").strip()
    svc_min: float | None = None
    svc_max: float | None = None
    if svc_id_raw.isdigit():
        svc = db.get(Service, int(svc_id_raw))
        if svc:
            svc_min, svc_max = _service_price_range_row(svc)
    prod_min = client_min
    prod_max = client_max
    calc_payload = {
        "calc_product_min": "" if prod_min is None else f"{float(prod_min):.0f}",
        "calc_product_max": "" if prod_max is None else f"{float(prod_max):.0f}",
        "calc_service_min": "" if svc_min is None else f"{float(svc_min):.0f}",
        "calc_service_max": "" if svc_max is None else f"{float(svc_max):.0f}",
    }
    try:
        if isinstance(prefill_sale, dict):
            prefill_sale.update(calc_payload)
        if isinstance(prefill_visit, dict):
            prefill_visit.update(calc_payload)
    except Exception:
        pass

    cost_hint = f"Себестоимость (материал + доп. расходы): {float(fin.cost_total_amount):.2f} ₽"
    pay_hint = f"ЗП мастера (итого): {float(fin.master_total):.2f} ₽"
    resp = {
        "client_hint": client_hint,
        "cost_hint": cost_hint,
        "pay_hint": pay_hint,
        "quoted_price_text": quoted,
        "client_min": client_min,
        "client_max": client_max,
        "prefill_sale": prefill_sale,
        "prefill_visit": prefill_visit,
    }
    return JSONResponse(resp)

