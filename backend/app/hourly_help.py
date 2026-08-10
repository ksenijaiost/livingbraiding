"""Почасовая помощь мастеров в визите и работе (1.7)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from fastapi import UploadFile
from sqlalchemy.orm import Session

from app.db.models import User, UserRole, Visit, VisitMastersScope
from app.payroll_fund import money_q2
from app.user_roles import user_has_any_role, user_has_role

_HOURS_RE = re.compile(r"^hourly_help_hours_(\d+)$")
_MINUTES_RE = re.compile(r"^hourly_help_minutes_(\d+)$")
_AMOUNT_RE = re.compile(r"^hourly_help_amount_(\d+)$")


@dataclass(frozen=True)
class HourlyHelpRow:
    master_id: int
    hours: int
    minutes: int
    amount: float


def _form_val(form: Any, key: str) -> str:
    raw = form.get(key)
    if raw is None or isinstance(raw, UploadFile):
        return ""
    if isinstance(raw, (bytes, bytearray)):
        return raw.decode().strip()
    return str(raw).strip()


def _parse_nonneg_int(raw: str, *, field_name: str) -> int:
    s = (raw or "").strip()
    if not s:
        return 0
    try:
        v = int(float(s.replace(",", ".")))
    except ValueError as e:
        raise ValueError(f"Некорректное значение «{field_name}».") from e
    if v < 0:
        raise ValueError(f"Значение «{field_name}» не может быть отрицательным.")
    return v


def _parse_amount(raw: str, *, field_name: str) -> float:
    s = (raw or "").strip()
    if not s:
        return 0.0
    try:
        v = float(s.replace(",", ".").replace(" ", ""))
    except ValueError as e:
        raise ValueError(f"Некорректная сумма «{field_name}».") from e
    if v < 0:
        raise ValueError(f"Сумма «{field_name}» не может быть отрицательной.")
    return money_q2(v)


def discover_hourly_help_master_ids(form: Any) -> list[int]:
    ids: set[int] = set()
    for key in form.keys():
        k = str(key)
        for rx in (_HOURS_RE, _MINUTES_RE, _AMOUNT_RE):
            m = rx.match(k)
            if m:
                ids.add(int(m.group(1)))
    return sorted(ids)


def parse_hourly_help_from_form(form: Any) -> list[HourlyHelpRow]:
    rows: list[HourlyHelpRow] = []
    for mid in discover_hourly_help_master_ids(form):
        hours = _parse_nonneg_int(_form_val(form, f"hourly_help_hours_{mid}"), field_name="часы")
        minutes = _parse_nonneg_int(_form_val(form, f"hourly_help_minutes_{mid}"), field_name="минуты")
        if minutes >= 60:
            raise ValueError("Минуты помощи должны быть меньше 60.")
        amount = _parse_amount(_form_val(form, f"hourly_help_amount_{mid}"), field_name="сумма")
        if hours == 0 and minutes == 0 and amount <= 0:
            continue
        rows.append(HourlyHelpRow(master_id=mid, hours=hours, minutes=minutes, amount=amount))
    return rows


def hourly_help_rows_to_json(rows: list[HourlyHelpRow]) -> str | None:
    if not rows:
        return None
    payload = [
        {
            "master_id": int(r.master_id),
            "hours": int(r.hours),
            "minutes": int(r.minutes),
            "amount": float(r.amount),
        }
        for r in rows
    ]
    return json.dumps(payload, ensure_ascii=False)


def hourly_help_rows_from_json(raw: str | None) -> list[HourlyHelpRow]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except Exception:
        return []
    if not isinstance(parsed, list):
        return []
    rows: list[HourlyHelpRow] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        try:
            mid = int(item.get("master_id") or 0)
        except (TypeError, ValueError):
            continue
        if mid <= 0:
            continue
        try:
            hours = max(0, int(item.get("hours") or 0))
            minutes = max(0, min(59, int(item.get("minutes") or 0)))
            amount = money_q2(float(item.get("amount") or 0))
        except (TypeError, ValueError):
            continue
        if hours == 0 and minutes == 0 and amount <= 0:
            continue
        rows.append(HourlyHelpRow(master_id=mid, hours=hours, minutes=minutes, amount=amount))
    return rows


def hourly_help_total(rows: list[HourlyHelpRow]) -> float:
    return money_q2(sum(float(r.amount or 0) for r in rows))


def format_hourly_help_duration(hours: int, minutes: int) -> str:
    parts: list[str] = []
    if hours > 0:
        parts.append(f"{hours} ч")
    if minutes > 0:
        parts.append(f"{minutes} мин")
    return " ".join(parts) if parts else "0 мин"


def _validate_master(db: Session, master_id: int) -> None:
    u = db.get(User, master_id)
    if not u or not u.is_active:
        raise ValueError("Сотрудник помощи не найден или отключён.")
    if not user_has_any_role(db, master_id, UserRole.MASTER, UserRole.HELPER):
        raise ValueError("Помощь может получить только активный мастер или помощник.")


def validate_hourly_help_rows(
    db: Session,
    rows: list[HourlyHelpRow],
    participant_master_ids: set[int],
) -> None:
    seen: set[int] = set()
    for row in rows:
        if row.master_id in seen:
            raise ValueError("Один мастер не может быть указан в помощи дважды.")
        seen.add(row.master_id)
        _validate_master(db, row.master_id)
        if row.master_id in participant_master_ids:
            raise ValueError("Мастер, участвующий в визите/работе, не может быть помощником.")


def collect_visit_participant_master_ids(
    *,
    masters_scope: VisitMastersScope,
    visit_master_allocations: list[tuple[int, int]],
    line_master_rows: dict[int, list[tuple[int, float]]],
    mix_bonus_master_ids: set[int] | None = None,
    correction_master_ids: set[int] | None = None,
) -> set[int]:
    ids: set[int] = set()
    if masters_scope == VisitMastersScope.VISIT:
        for mid, _ in visit_master_allocations:
            ids.add(int(mid))
    else:
        for rows in line_master_rows.values():
            for mid, _ in rows:
                ids.add(int(mid))
    if mix_bonus_master_ids:
        ids |= {int(x) for x in mix_bonus_master_ids}
    if correction_master_ids:
        ids |= {int(x) for x in correction_master_ids}
    return ids


def apply_hourly_help_to_visit(visit: Visit, rows: list[HourlyHelpRow]) -> float:
    """Уменьшить masters_pool услуг пропорционально сумме помощи. Возвращает итог помощи."""
    help_total = hourly_help_total(rows)
    visit.hourly_help_json = hourly_help_rows_to_json(rows)
    visit.hourly_help_total = help_total

    gross_pool = money_q2(sum(float(s.masters_pool or 0) for s in (visit.services or []) if not s.is_cancelled))
    if gross_pool < -0.01:
        raise ValueError(
            f"Пул ЗП мастеров визита отрицательный ({gross_pool:.0f} ₽): "
            "сумма с клиента меньше себестоимости (услуга + комплект + материал + амортизация). "
            "Проверьте суммы и списание комплекта."
        )
    if help_total > 0 and help_total > gross_pool + 0.01:
        raise ValueError(
            f"Сумма почасовой помощи превышает пул ЗП мастеров визита "
            f"(помощь {help_total:.0f} ₽, пул {gross_pool:.0f} ₽)."
        )

    if help_total <= 0 or gross_pool <= 0:
        visit.masters_pool = gross_pool
        return help_total

    ratio = (gross_pool - help_total) / gross_pool
    for vs in visit.services or []:
        if vs.is_cancelled:
            continue
        vs.masters_pool = money_q2(float(vs.masters_pool or 0) * ratio)
    visit.masters_pool = money_q2(gross_pool - help_total)
    return help_total


def apply_hourly_help_to_staff_profits(
    staff_profits: dict[int, float],
    participant_ids: set[int],
    rows: list[HourlyHelpRow],
) -> tuple[dict[int, float], dict[int, float]]:
    """Уменьшить ЗП участников работы; вернуть (новые доли участников, ЗП помощников)."""
    help_total = hourly_help_total(rows)
    primary_total = money_q2(sum(float(staff_profits.get(pid, 0.0)) for pid in participant_ids))
    if primary_total < -0.01:
        raise ValueError(
            f"ЗП мастеров работы отрицательная ({primary_total:.0f} ₽): "
            "проверьте суммы с клиента и себестоимость."
        )
    if help_total > 0 and help_total > primary_total + 0.01:
        raise ValueError(
            f"Сумма почасовой помощи превышает ЗП мастеров работы "
            f"(помощь {help_total:.0f} ₽, ЗП мастеров {primary_total:.0f} ₽)."
        )

    helper_profits = {int(r.master_id): float(r.amount) for r in rows if float(r.amount) > 0}
    if help_total <= 0 or primary_total <= 0:
        return dict(staff_profits), helper_profits

    ratio = (primary_total - help_total) / primary_total
    new_primary = dict(staff_profits)
    for pid in participant_ids:
        new_primary[pid] = money_q2(float(staff_profits.get(pid, 0.0)) * ratio)
    return new_primary, helper_profits


def hourly_help_prefill_from_rows(rows: list[HourlyHelpRow]) -> dict[str, str]:
    fp: dict[str, str] = {}
    if not rows:
        return fp
    fp["hourly_help_open"] = "on"
    for row in rows:
        mid = int(row.master_id)
        if row.hours:
            fp[f"hourly_help_hours_{mid}"] = str(int(row.hours))
        if row.minutes:
            fp[f"hourly_help_minutes_{mid}"] = str(int(row.minutes))
        if row.amount:
            fp[f"hourly_help_amount_{mid}"] = str(int(round(float(row.amount))))
    return fp


@dataclass(frozen=True)
class HourlyHelpDisplayRow:
    master_id: int
    master_name: str
    duration_text: str
    amount: float


def build_hourly_help_display_rows(rows: list[HourlyHelpRow], db: Session) -> list[HourlyHelpDisplayRow]:
    out: list[HourlyHelpDisplayRow] = []
    for row in rows:
        u = db.get(User, int(row.master_id))
        name = (u.display_name or u.username or "").strip() if u else ""
        if not name:
            name = f"ID {row.master_id}"
        out.append(
            HourlyHelpDisplayRow(
                master_id=int(row.master_id),
                master_name=name,
                duration_text=format_hourly_help_duration(int(row.hours), int(row.minutes)),
                amount=float(row.amount),
            )
        )
    return out


def hourly_help_rows_from_visit(visit: Visit) -> list[HourlyHelpRow]:
    return hourly_help_rows_from_json(getattr(visit, "hourly_help_json", None))


def hourly_help_rows_from_work_details(details: dict[str, Any]) -> list[HourlyHelpRow]:
    raw = details.get("hourly_help")
    if isinstance(raw, list):
        return hourly_help_rows_from_json(json.dumps(raw, ensure_ascii=False))
    if isinstance(raw, str):
        return hourly_help_rows_from_json(raw)
    return []


def master_hourly_help_pay_from_visit(visit: Visit, master_id: int) -> float:
    total = 0.0
    for row in hourly_help_rows_from_visit(visit):
        if int(row.master_id) == int(master_id):
            total = money_q2(total + float(row.amount or 0))
    return total


def visit_hourly_help_master_clause(master_id: int):
    """SQL: визит, где мастер указан в почасовой помощи (hourly_help_json)."""
    from sqlalchemy import or_

    mid = int(master_id)
    # Границы после id, чтобы «5» не ловил «50».
    return or_(
        Visit.hourly_help_json.like(f'%"master_id": {mid},%'),
        Visit.hourly_help_json.like(f'%"master_id": {mid}}}%'),
        Visit.hourly_help_json.like(f'%"master_id":{mid},%'),
        Visit.hourly_help_json.like(f'%"master_id":{mid}}}%'),
    )
