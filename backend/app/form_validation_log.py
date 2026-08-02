"""Логирование пользовательских ошибок валидации форм с контекстом полей."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from starlette.requests import Request

MAX_FIELD_LEN = 2000
MAX_SNAPSHOT_FIELDS = 400

_TRUNCATE_ONLY_KEYS = frozenset({
    "stock_kit_lines_json",
    "own_extra_stock_kit_lines_json",
    "planned_service_ids",
})

_MESSAGE_SUBSTR_FIELDS: list[tuple[str, tuple[str, ...]]] = [
    (
        "сумму",
        (
            "amount_from_client",
            "client_payment_kind",
            "client_is_self",
            "client_discount_percent",
            "own_correction",
            "own_corr_use_custom_amount",
            "own_corr_custom_amount",
            "own_corr_client_payment_kind",
            "corr_custom_amount",
            "corr_use_custom_amount",
            "corr_client_payment_kind",
        ),
    ),
    (
        "клиент",
        (
            "existing_client_id",
            "client_mode",
            "client_is_self",
            "draft_client_name",
            "draft_phone",
            "draft_telegram",
        ),
    ),
    ("сложность смешки", ("mix_complexity", "mix_source", "kanekalon_grams", "kudri_grams")),
    (
        "коррекции",
        (
            "own_correction",
            "own_corr_trim_qty",
            "own_corr_hourly_hours",
            "own_corr_wash",
            "own_corr_circle",
            "own_corr_steam",
            "own_corr_kit_description",
            "own_corr_use_custom_amount",
            "own_corr_custom_amount",
            "corr_trim_qty",
            "corr_hourly_hours",
            "corr_wash",
            "corr_circle",
            "corr_steam",
            "kind",
        ),
    ),
    ("комплект", ("kit_kind", "stock_kit_id", "stock_kit_lines_json", "stock_blanks_used")),
    ("услуг", ("service_id", "line_count", "planned_service_ids")),
    ("мастер", ("visit_use_multi_masters", "visit_master_on", "masters_scope")),
    ("дату", ("performed_date", "duration_h", "duration_m")),
    ("брон", ("booking_id",)),
    (
        "почасов",
        (
            "hourly_help_open",
            "masters_scope",
            "visit_master_on",
            "visit_use_multi_masters",
        ),
    ),
    (
        "помощ",
        (
            "hourly_help_open",
            "masters_scope",
            "visit_master_on",
            "visit_use_multi_masters",
        ),
    ),
]

_VISIT_HIGHLIGHT_KEYS = frozenset({
    "booking_id",
    "service_id",
    "amount_from_client",
    "client_payment_kind",
    "client_is_self",
    "client_mode",
    "existing_client_id",
    "client_discount_percent",
    "kit_kind",
    "own_correction",
    "own_corr_use_custom_amount",
    "own_corr_custom_amount",
    "own_corr_client_payment_kind",
    "own_corr_trim_qty",
    "own_corr_hourly_hours",
    "own_corr_wash",
    "own_corr_circle",
    "own_corr_steam",
    "performed_date",
    "duration_h",
    "duration_m",
    "kanekalon_grams",
    "kudri_grams",
    "mix_source",
    "mix_complexity",
    "amortization_level",
    "masters_scope",
    "stock_kit_id",
    "stock_blanks_used",
    "kit_paid_separately",
    "hourly_help_open",
})

_WORK_HIGHLIGHT_KEYS = frozenset({
    "kind",
    "scope",
    "performed_date",
    "amount_from_client",
    "client_payment_kind",
    "corr_use_custom_amount",
    "corr_custom_amount",
    "corr_client_payment_kind",
    "corr_trim_qty",
    "corr_hourly_hours",
    "corr_wash",
    "corr_circle",
    "corr_steam",
    "kanekalon_grams",
    "kudri_grams",
    "mix_source",
    "booking_id",
    "hourly_help_open",
})

_LINE_KEY_RE = re.compile(r"^line_\d+_")
_HOURLY_HELP_AMOUNT_RE = re.compile(r"^hourly_help_amount_(\d+)$")
_HOURLY_HELP_HOURS_RE = re.compile(r"^hourly_help_hours_(\d+)$")
_HOURLY_HELP_MINUTES_RE = re.compile(r"^hourly_help_minutes_(\d+)$")


def _is_upload(v: Any) -> bool:
    return hasattr(v, "read") and callable(getattr(v, "read", None))


def snapshot_form_fields(form: Any) -> dict[str, str]:
    """Снимок полей формы для лога (без содержимого файлов)."""
    if form is None:
        return {}
    out: dict[str, str] = {}
    keys = list(form.keys()) if hasattr(form, "keys") else []
    for key in keys:
        k = str(key)
        if k.startswith("photo_"):
            out[k] = "<file>"
            continue
        vals = form.getlist(k) if hasattr(form, "getlist") else [form.get(k)]
        parts: list[str] = []
        for v in vals:
            if v is None:
                continue
            if _is_upload(v):
                fn = getattr(v, "filename", None) or "?"
                parts.append(f"<file:{fn}>")
                continue
            s = v.decode() if isinstance(v, (bytes, bytearray)) else str(v)
            parts.append(s)
        if not parts:
            continue
        val = ",".join(parts) if k == "visit_master_on" and len(parts) > 1 else parts[-1]
        if k in _TRUNCATE_ONLY_KEYS and len(val) > MAX_FIELD_LEN:
            val = val[:MAX_FIELD_LEN] + f"…<truncated len={len(val)}>"
        elif len(val) > MAX_FIELD_LEN:
            val = val[:MAX_FIELD_LEN] + "…"
        out[k] = val
        if len(out) >= MAX_SNAPSHOT_FIELDS:
            out["__truncated__"] = f"only first {MAX_SNAPSHOT_FIELDS} fields"
            break
    return out


def relevant_fields_for_message(message: str) -> set[str]:
    msg = (message or "").lower()
    fields: set[str] = set()
    for substr, names in _MESSAGE_SUBSTR_FIELDS:
        if substr in msg:
            fields.update(names)
    return fields


def _fields_matching_hint(snapshot: dict[str, str], hint: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in snapshot.items():
        if k == hint or k.endswith(f"_{hint}") or hint in k:
            out[k] = v
    return out


def _line_amount_fields(snapshot: dict[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in snapshot.items():
        if (
            "amount_from_client" in k
            or "own_corr_custom_amount" in k
            or "own_corr_use_custom_amount" in k
        ):
            out[k] = v
    return out


def _parse_money(raw: str) -> float | None:
    s = (raw or "").strip().replace(",", ".")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def hourly_help_summary_from_snapshot(snapshot: dict[str, str]) -> dict[str, Any] | None:
    """Сводка почасовой помощи из полей формы (для лога валидации)."""
    by_master: dict[str, dict[str, Any]] = {}
    for k, v in snapshot.items():
        m_amt = _HOURLY_HELP_AMOUNT_RE.match(k)
        if m_amt:
            mid = m_amt.group(1)
            row = by_master.setdefault(mid, {"master_id": int(mid)})
            amt = _parse_money(v)
            if amt is not None:
                row["amount"] = amt
            continue
        m_h = _HOURLY_HELP_HOURS_RE.match(k)
        if m_h:
            mid = m_h.group(1)
            row = by_master.setdefault(mid, {"master_id": int(mid)})
            try:
                row["hours"] = int(str(v or "0").strip() or "0")
            except ValueError:
                row["hours"] = v
            continue
        m_m = _HOURLY_HELP_MINUTES_RE.match(k)
        if m_m:
            mid = m_m.group(1)
            row = by_master.setdefault(mid, {"master_id": int(mid)})
            try:
                row["minutes"] = int(str(v or "0").strip() or "0")
            except ValueError:
                row["minutes"] = v
    rows = [by_master[k] for k in sorted(by_master.keys(), key=lambda x: int(x))]
    if not rows:
        return None
    total = 0.0
    for r in rows:
        if isinstance(r.get("amount"), (int, float)):
            total += float(r["amount"])
    return {
        "hourly_help_total": round(total, 2),
        "hourly_help_rows": rows,
    }


def build_validation_log_payload(
    *,
    message: str,
    snapshot: dict[str, str],
    context: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    highlights: dict[str, str] = {}
    ctx = (context or "").lower()
    highlight_keys = (
        _VISIT_HIGHLIGHT_KEYS
        if ctx == "visit"
        else _WORK_HIGHLIGHT_KEYS
        if ctx == "work"
        else frozenset()
    )
    for k, v in snapshot.items():
        if k in highlight_keys:
            highlights[k] = v
        elif k.startswith("hourly_help_"):
            highlights[k] = v
        elif _LINE_KEY_RE.match(k) and any(
            part in k for part in ("amount_from_client", "service_id", "own_corr", "kit_kind", "hourly_help")
        ):
            highlights[k] = v

    hints = relevant_fields_for_message(message)
    related: dict[str, str] = {}
    for hint in sorted(hints):
        related.update(_fields_matching_hint(snapshot, hint))
    if "сумму" in (message or "").lower():
        related.update(_line_amount_fields(snapshot))
    msg_l = (message or "").lower()
    if "почасов" in msg_l or "помощ" in msg_l:
        related.update(_fields_matching_hint(snapshot, "hourly_help"))

    payload: dict[str, Any] = {
        "message": message,
        "highlights": highlights,
        "related": related,
        # Полный снимок полей формы (то, что ушло бы в сохранение), без усечения по числу полей.
        "form": snapshot,
    }
    help_sum = hourly_help_summary_from_snapshot(snapshot)
    if help_sum is not None:
        payload["hourly_help"] = help_sum
    if hints:
        payload["hint_fields"] = sorted(hints)
    if extra:
        payload["extra"] = extra
    return payload


def log_user_validation_error(
    logger: logging.Logger,
    *,
    request: Request | None,
    route: str,
    message: str,
    form: Any | None = None,
    user_id: int | None = None,
    username: str | None = None,
    context: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Дублирует пользовательскую ошибку в лог с полями формы (WARNING)."""
    if request is not None:
        request.state.validation_error = message
    snapshot = snapshot_form_fields(form)
    payload = build_validation_log_payload(
        message=message,
        snapshot=snapshot,
        context=context,
        extra=extra,
    )
    if username and user_id is not None:
        user_part = f"{username}(id={user_id})"
    elif username:
        user_part = username
    elif user_id is not None:
        user_part = str(user_id)
    else:
        user_part = "-"
    logger.warning(
        "form validation failed | route=%s user=%s | %s",
        route,
        user_part,
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
    )
