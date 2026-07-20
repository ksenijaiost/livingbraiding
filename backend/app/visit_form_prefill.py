"""Преобразование сохранённого визита в поля формы master_visit_step1."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.db.models import (
    Kit,
    Visit,
    VisitClientType,
    VisitKitUsage,
    VisitMaster,
    VisitMastersScope,
    VisitService,
    VisitServiceMaster,
)
from app.questionnaire.schemas import VisitServiceDetailsPayload, parse_visit_service_details


def _set(fp: dict[str, str], key: str, val: Any) -> None:
    if val is None:
        return
    if isinstance(val, bool):
        fp[key] = "on" if val else ""
    else:
        fp[key] = str(val)


def _kit_usages_for_service(db: Session, visit: Visit, vs: VisitService) -> list[VisitKitUsage]:
    return [
        u
        for u in (visit.kit_usages or [])
        if u.visit_service_id == vs.id
    ]


def _stock_lines_from_usages(db: Session, usages: list[VisitKitUsage]) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    for u in usages:
        if int(u.pieces_used or 0) <= 0:
            continue
        bd = None
        raw = getattr(u, "usage_breakdown_json", None)
        if raw:
            try:
                bd = json.loads(raw)
                if not isinstance(bd, dict):
                    bd = None
            except Exception:
                bd = None
        kit = db.get(Kit, u.kit_id)
        use_entire = False
        if kit and int(u.pieces_used or 0) > 0:
            used = int(u.pieces_used or 0)
            total = int(kit.pieces_total or 0)
            avail = int(kit.pieces_available or 0)
            use_entire = used >= total or (total > 0 and avail + used >= total)
        lines.append(
            {
                "kit_id": int(u.kit_id),
                "use_entire": use_entire,
                "blanks_used": int(u.pieces_used or 0),
                "breakdown": bd,
            }
        )
    return lines


def _apply_kit_to_line_fp(
    fp: dict[str, str],
    prefix: str,
    payload: VisitServiceDetailsPayload,
    usages: list[VisitKitUsage],
    db: Session,
) -> None:
    kit = payload.kit
    if kit is None:
        return
    p = f"{prefix}" if prefix else ""
    _set(fp, f"{p}kit_kind", kit.kind)
    if kit.kind == "STOCK":
        stocks = list(kit.from_stocks or [])
        if not stocks and kit.from_stock:
            stocks = [kit.from_stock]
        lines: list[dict[str, Any]] = []
        usage_by_kid = {int(u.kit_id): u for u in usages}
        for fs in stocks:
            kid = None
            if fs.sku:
                k = db.scalar(select(Kit.id).where(Kit.sku == fs.sku).limit(1))
                if k:
                    kid = int(k)
            if kid is None and usage_by_kid:
                kid = next(iter(usage_by_kid.keys()), None)
            bd = fs.usage_by_key
            if kid and kid in usage_by_kid:
                u = usage_by_kid[kid]
                if not bd and u.usage_breakdown_json:
                    try:
                        bd = json.loads(u.usage_breakdown_json)
                    except Exception:
                        bd = None
            lines.append(
                {
                    "kit_id": kid,
                    "use_entire": bool(fs.use_entire_kit),
                    "blanks_used": int(fs.blanks_used or 0),
                    "breakdown": bd,
                }
            )
        if not lines:
            lines = _stock_lines_from_usages(db, usages)
        if lines:
            _set(fp, f"{p}stock_kit_lines_json", json.dumps(lines, ensure_ascii=False))
            first = lines[0]
            if first.get("kit_id"):
                _set(fp, f"{p}stock_kit_id", first["kit_id"])
            if first.get("breakdown"):
                _set(fp, f"{p}stock_breakdown_json", json.dumps(first["breakdown"], ensure_ascii=False))
            _set(fp, f"{p}stock_blanks_used", first.get("blanks_used", 0))
            if first.get("use_entire"):
                fp[f"{p}stock_use_entire"] = "on"
    elif kit.kind == "OWN" and kit.own:
        own = kit.own
        _set(fp, f"{p}own_origin", own.origin)
        if own.correction:
            fp[f"{p}own_correction"] = "on"
        cd = own.correction_details
        if cd:
            _set(fp, f"{p}own_corr_trim_qty", cd.trim_qty)
            _set(fp, f"{p}own_corr_hourly_hours", cd.hourly_hours)
            _set(fp, f"{p}own_corr_kit_description", cd.kit_description)
            if cd.kit_blanks_count is not None:
                _set(fp, f"{p}own_corr_kit_blanks_count", cd.kit_blanks_count)
            if cd.wash:
                fp[f"{p}own_corr_wash"] = "on"
            if cd.circle:
                fp[f"{p}own_corr_circle"] = "on"
            if cd.steam:
                fp[f"{p}own_corr_steam"] = "on"
            if getattr(cd, "use_custom_amount", False):
                fp[f"{p}own_corr_use_custom_amount"] = "1"
                if cd.custom_amount is not None:
                    _set(fp, f"{p}own_corr_custom_amount", cd.custom_amount)
            if getattr(cd, "master_id", None):
                _set(fp, f"{p}own_corr_master_id", cd.master_id)
        if own.extra_blanks and own.extra:
            fp[f"{p}own_extra_blanks"] = "on"
            if own.extra.source == "STOCK":
                extra_stocks = list(own.extra.from_stocks or [])
                if not extra_stocks and own.extra.from_stock:
                    extra_stocks = [own.extra.from_stock]
                extra_lines: list[dict[str, Any]] = []
                for fs in extra_stocks:
                    kid = None
                    if fs.sku:
                        k = db.scalar(select(Kit.id).where(Kit.sku == fs.sku).limit(1))
                        if k:
                            kid = int(k)
                    extra_lines.append(
                        {
                            "kit_id": kid,
                            "use_entire": bool(fs.use_entire_kit),
                            "blanks_used": int(fs.blanks_used or 0),
                            "breakdown": fs.usage_by_key,
                        }
                    )
                if extra_lines:
                    _set(fp, f"{p}own_extra_stock_kit_lines_json", json.dumps(extra_lines, ensure_ascii=False))
                    if extra_lines[0].get("kit_id"):
                        _set(fp, f"{p}own_extra_stock_kit_id", extra_lines[0]["kit_id"])


def _apply_thermo_to_fp(fp: dict[str, str], prefix: str, payload: VisitServiceDetailsPayload) -> None:
    thermo = payload.thermo
    if thermo is None:
        return
    p = prefix
    _set(fp, f"{p}thermo_curls", thermo.curls_material)
    _set(fp, f"{p}thermo_length", thermo.material_length)
    _set(fp, f"{p}thermo_shade", thermo.shade)
    _set(fp, f"{p}thermo_bases_total", thermo.bases_total)
    _set(fp, f"{p}thermo_weight", thermo.weight_with_margin)
    _set(fp, f"{p}thermo_template_mode", thermo.template_mode)
    if thermo.old_template_id:
        _set(fp, f"{p}thermo_old_template_id", thermo.old_template_id)
    if thermo.algorithm_changes:
        _set(fp, f"{p}thermo_algorithm_changes", thermo.algorithm_changes)
    tpl = thermo.filled_template or thermo.saved_template_snapshot
    if tpl:
        _set(fp, f"{p}thermo_tpl_strand_weight_avg", tpl.strand_weight_avg)
        _set(fp, f"{p}thermo_tpl_row_1", tpl.row_1)
        _set(fp, f"{p}thermo_tpl_row_2", tpl.row_2)
        _set(fp, f"{p}thermo_tpl_row_3", tpl.row_3)
        _set(fp, f"{p}thermo_tpl_other_rows", tpl.other_rows_text)
        _set(fp, f"{p}thermo_tpl_temples", tpl.temples)
        _set(fp, f"{p}thermo_tpl_triangles", tpl.triangles)
        _set(fp, f"{p}thermo_tpl_bird", tpl.bird)
        _set(fp, f"{p}thermo_tpl_square", tpl.square)
        _set(fp, f"{p}thermo_tpl_comment", tpl.comment)


def _apply_questionnaire_to_fp(fp: dict[str, str], prefix: str, payload: VisitServiceDetailsPayload) -> None:
    for k, v in (payload.answers or {}).items():
        key = k if str(k).startswith("q_") else f"q_{k}"
        if prefix:
            fp[f"{prefix}{key}"] = str(v) if v is not None else ""
        else:
            fp[key] = str(v) if v is not None else ""


def _apply_service_line_to_fp(
    db: Session,
    visit: Visit,
    vs: VisitService,
    idx: int,
    fp: dict[str, str],
) -> None:
    prefix = f"line_{idx}_" if idx > 0 else ""
    p = prefix

    _set(fp, f"{p}visit_service_id", vs.id)
    if idx == 0:
        _set(fp, "service_id", vs.service_id)
    else:
        _set(fp, f"{p}service_id", vs.service_id)

    _set(fp, f"{p}amount_from_client", int(vs.amount_from_client or 0))
    if vs.client_payment_kind:
        _set(fp, f"{p}client_payment_kind", vs.client_payment_kind.value)
    _set(fp, f"{p}client_discount_percent", vs.client_discount_percent or 0)
    _set(fp, f"{p}kanekalon_grams", vs.kanekalon_grams or 0)
    _set(fp, f"{p}kudri_grams", vs.kudri_grams or 0)
    if vs.mix_source:
        _set(fp, f"{p}mix_source", vs.mix_source.value)
    if vs.mix_complexity:
        _set(fp, f"{p}mix_complexity", vs.mix_complexity.value)
    if vs.mix_bonus_master_id:
        _set(fp, f"{p}mix_bonus_master_id", vs.mix_bonus_master_id)
    if vs.correction_master_id:
        _set(fp, f"{p}own_corr_master_id", vs.correction_master_id)
    if vs.amortization_level:
        _set(fp, f"{p}amortization_level", vs.amortization_level.value)
    elif idx == 0:
        fp["amortization_level"] = "NONE"
    if vs.kit_paid_separately:
        fp[f"{p}kit_paid_separately"] = "on"
    if vs.comment:
        _set(fp, f"{p}comment", vs.comment)
    if vs.started_at:
        _set(fp, f"{p}started_time", vs.started_at.strftime("%H:%M"))

    if idx == 0:
        _set(fp, "amount_from_client", int(vs.amount_from_client or 0))
        if vs.client_payment_kind:
            _set(fp, "client_payment_kind", vs.client_payment_kind.value)
        _set(fp, "client_discount_percent", vs.client_discount_percent or 0)
        _set(fp, "kanekalon_grams", vs.kanekalon_grams or 0)
        _set(fp, "kudri_grams", vs.kudri_grams or 0)
        if vs.mix_source:
            _set(fp, "mix_source", vs.mix_source.value)
        if vs.mix_complexity:
            _set(fp, "mix_complexity", vs.mix_complexity.value)
        if vs.mix_bonus_master_id:
            _set(fp, "mix_bonus_master_id", vs.mix_bonus_master_id)
        if vs.correction_master_id:
            _set(fp, "own_corr_master_id", vs.correction_master_id)
        if vs.amortization_level:
            _set(fp, "amortization_level", vs.amortization_level.value)

    if vs.addons_total and float(vs.addons_total) > 0:
        _set(fp, f"{p}addon_sales_amount", vs.addons_total)
        if vs.addons_details_json:
            try:
                ad = json.loads(vs.addons_details_json)
                if isinstance(ad, dict) and ad.get("description"):
                    _set(fp, f"{p}addon_sales_description", ad["description"])
            except Exception:
                pass

    try:
        raw = json.loads(vs.details_json or "{}")
        payload = parse_visit_service_details(raw)
    except Exception:
        payload = VisitServiceDetailsPayload()

    usages = _kit_usages_for_service(db, visit, vs)
    _apply_kit_to_line_fp(fp, prefix, payload, usages, db)
    if fp.get(f"{p}own_corr_use_custom_amount") and vs.client_payment_kind:
        _set(fp, f"{p}own_corr_client_payment_kind", vs.client_payment_kind.value)
    _apply_questionnaire_to_fp(fp, prefix if idx > 0 else "", payload)
    _apply_thermo_to_fp(fp, prefix, payload)

    if visit.masters_scope == VisitMastersScope.PER_SERVICE:
        masters = list(
            db.scalars(
                select(VisitServiceMaster).where(VisitServiceMaster.visit_service_id == vs.id)
            ).all()
        )
        for vm in masters:
            _set(fp, f"line_{idx}_service_master_pct_{int(vm.master_id)}", int(vm.percent or 0))


def visit_to_form_prefill(
    db: Session,
    visit: Visit,
) -> tuple[dict[str, str], list[int], dict[int, str], list[dict[str, Any]]]:
    """
    Визит → form_prefill, visit_master_on_ids, visit_master_pct_str, extra_lines для JS.
    """
    fp: dict[str, str] = {}
    active = sorted(
        [s for s in (visit.services or []) if not s.is_cancelled],
        key=lambda s: (int(s.sort_order or 0), int(s.id or 0)),
    )

    _set(fp, "existing_client_id", visit.client_id)
    fp["client_mode"] = "existing"
    if visit.client_type == VisitClientType.SELF:
        fp["client_is_self"] = "on"
    if visit.performed_date:
        _set(fp, "performed_date", visit.performed_date.date().isoformat())
    dm = int(visit.duration_minutes or 0)
    _set(fp, "duration_h", dm // 60)
    _set(fp, "duration_m", dm % 60)
    _set(fp, "masters_scope", (visit.masters_scope or VisitMastersScope.VISIT).value)
    if visit.same_master_shares_all_services:
        fp["same_master_shares_all_services"] = "on"
    if visit.booking_id:
        _set(fp, "booking_id", visit.booking_id)

    vm_on_ids: list[int] = []
    vm_pct_str: dict[int, str] = {}
    visit_masters = list(
        db.scalars(select(VisitMaster).where(VisitMaster.visit_id == visit.id).order_by(VisitMaster.id)).all()
    )
    for vm in visit_masters:
        vm_on_ids.append(int(vm.master_id))
        vm_pct_str[int(vm.master_id)] = str(int(vm.percent or 0))
    if len(vm_on_ids) > 1:
        fp["visit_use_multi_masters"] = "on"
    fp["visit_master_on"] = ",".join(str(x) for x in vm_on_ids)

    extra_lines: list[dict[str, Any]] = []
    for i, vs in enumerate(active):
        _apply_service_line_to_fp(db, visit, vs, i, fp)
        if i >= 1:
            line_fp: dict[str, str] = {}
            prefix = f"line_{i}_"
            for k, v in fp.items():
                if k.startswith(prefix):
                    line_fp[k[len(prefix) :]] = v
            line_fp["visit_service_id"] = str(vs.id)
            line_fp["service_id"] = str(vs.service_id)
            extra_lines.append({"idx": i, "fp": line_fp})

    if len(active) > 1:
        _set(fp, "line_count", len(active) - 1)

    return fp, vm_on_ids, vm_pct_str, extra_lines
