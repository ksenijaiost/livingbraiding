"""История изменений графика мастера: группировка audit-логов и человекочитаемый вывод."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.db.models import MasterScheduleAuditLog, User
from app.display_time import format_naive_utc_datetime, get_display_timezone

_WD_SHORT = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")

_FIELD_ALIASES = {
    "status": "status",
    "статус": "status",
    "time_from": "time_from",
    "начало работы": "time_from",
    "time_to": "time_to",
    "конец работы": "time_to",
    "break_from": "break_from",
    "перерыв с": "break_from",
    "break_to": "break_to",
    "перерыв до": "break_to",
}


def _norm_field(name: str) -> str:
    key = str(name or "").strip().lower()
    return _FIELD_ALIASES.get(key, key)


def _empty(v: str | None) -> bool:
    return v is None or str(v).strip() == "" or str(v).strip().lower() == "none"


def format_schedule_status(raw: str | None) -> str | None:
    if _empty(raw):
        return None
    s = str(raw).strip().upper()
    if s in ("WORKING", "РАБОЧИЙ"):
        return "рабочий"
    if s in ("DAY_OFF", "ВЫХОДНОЙ"):
        return "выходной"
    return str(raw).strip()


def format_schedule_time(raw: str | None) -> str | None:
    if _empty(raw):
        return None
    s = str(raw).strip()
    # "10:00:00" / "10:00" / "10:00:00.000"
    if len(s) >= 5 and s[2] == ":":
        return s[:5]
    return s


def format_work_date_human(d: date) -> str:
    return f"{d.strftime('%d.%m.%Y')}, {_WD_SHORT[d.weekday()]}"


def _arrow_line(*, label: str, old: str | None, new: str | None, kind: str) -> str | None:
    """kind: create|delete|change"""
    if _empty(old) and _empty(new):
        return None
    if kind == "create" or (_empty(old) and not _empty(new)):
        return f"{label}: {new}"
    if kind == "delete" or (not _empty(old) and _empty(new)):
        return f"{label}: было {old}"
    if old == new:
        return None
    return f"{label}: {old} → {new}"


def _day_field_map(rows: list[MasterScheduleAuditLog]) -> dict[str, tuple[str | None, str | None]]:
    """Поле → (old, new); при нескольких записях берём первый old и последний new."""
    out: dict[str, tuple[str | None, str | None]] = {}
    for r in rows:
        key = _norm_field(r.field_name)
        old0, new0 = out.get(key, (None, None))
        old_v = r.old_value if old0 is None else old0
        new_v = r.new_value if r.new_value is not None else new0
        if key not in out:
            out[key] = (r.old_value, r.new_value)
        else:
            out[key] = (old_v, new_v)
    return out


def format_day_change_lines(rows: list[MasterScheduleAuditLog], *, change_kind: str) -> list[str]:
    """Человекочитаемые строки изменений для одного work_date."""
    fmap = _day_field_map(rows)
    lines: list[str] = []

    if "status" in fmap:
        old_s = format_schedule_status(fmap["status"][0])
        new_s = format_schedule_status(fmap["status"][1])
        line = _arrow_line(label="Статус", old=old_s, new=new_s, kind=change_kind)
        if line:
            lines.append(line)

    tf_old = format_schedule_time(fmap.get("time_from", (None, None))[0]) if "time_from" in fmap else None
    tf_new = format_schedule_time(fmap.get("time_from", (None, None))[1]) if "time_from" in fmap else None
    tt_old = format_schedule_time(fmap.get("time_to", (None, None))[0]) if "time_to" in fmap else None
    tt_new = format_schedule_time(fmap.get("time_to", (None, None))[1]) if "time_to" in fmap else None
    if "time_from" in fmap or "time_to" in fmap:
        # Подтянуть соседнее поле из того же дня, если не менялось — не показываем пару с дыркой.
        old_pair = None
        new_pair = None
        if not _empty(tf_old) or not _empty(tt_old):
            if not _empty(tf_old) and not _empty(tt_old):
                old_pair = f"{tf_old}–{tt_old}"
            elif not _empty(tf_old):
                old_pair = f"с {tf_old}"
            elif not _empty(tt_old):
                old_pair = f"до {tt_old}"
        if not _empty(tf_new) or not _empty(tt_new):
            if not _empty(tf_new) and not _empty(tt_new):
                new_pair = f"{tf_new}–{tt_new}"
            elif not _empty(tf_new):
                new_pair = f"с {tf_new}"
            elif not _empty(tt_new):
                new_pair = f"до {tt_new}"
        line = _arrow_line(label="Время работы", old=old_pair, new=new_pair, kind=change_kind)
        if line:
            lines.append(line)

    bf_old = format_schedule_time(fmap.get("break_from", (None, None))[0]) if "break_from" in fmap else None
    bf_new = format_schedule_time(fmap.get("break_from", (None, None))[1]) if "break_from" in fmap else None
    bt_old = format_schedule_time(fmap.get("break_to", (None, None))[0]) if "break_to" in fmap else None
    bt_new = format_schedule_time(fmap.get("break_to", (None, None))[1]) if "break_to" in fmap else None
    if "break_from" in fmap or "break_to" in fmap:
        old_pair = None
        new_pair = None
        if not _empty(bf_old) and not _empty(bt_old):
            old_pair = f"{bf_old}–{bt_old}"
        elif not _empty(bf_old) or not _empty(bt_old):
            old_pair = f"{bf_old or '—'}–{bt_old or '—'}"
        if not _empty(bf_new) and not _empty(bt_new):
            new_pair = f"{bf_new}–{bt_new}"
        elif not _empty(bf_new) or not _empty(bt_new):
            new_pair = f"{bf_new or '—'}–{bt_new or '—'}"
        line = _arrow_line(label="Перерыв", old=old_pair, new=new_pair, kind=change_kind)
        if line:
            lines.append(line)

    return lines


def detect_change_kind(rows: list[MasterScheduleAuditLog]) -> str:
    """create | delete | change"""
    if not rows:
        return "change"
    all_old_empty = all(_empty(r.old_value) for r in rows)
    all_new_empty = all(_empty(r.new_value) for r in rows)
    if all_old_empty and not all_new_empty:
        return "create"
    if all_new_empty and not all_old_empty:
        return "delete"
    return "change"


def change_type_label(*, kind: str, is_bulk: bool) -> str:
    if is_bulk:
        if kind == "create":
            return "Массовое создание"
        return "Массовое изменение"
    if kind == "create":
        return "Создание"
    if kind == "delete":
        return "Удаление"
    return "Изменение"


@dataclass
class ScheduleHistoryGroup:
    change_type: str
    changed_at: datetime
    work_dates: list[date]
    change_lines: list[str]
    actor_name: str | None
    actor_is_self: bool
    batch_id: str | None
    source: str
    work_dates_extra: int = 0
    change_lines_detail: list[str] = field(default_factory=list)

    def to_dict(self, *, tz_name: str, dates_preview: int = 10) -> dict[str, Any]:
        dates_sorted = sorted(self.work_dates)
        preview = dates_sorted[:dates_preview]
        extra = max(0, len(dates_sorted) - len(preview))
        return {
            "change_type": self.change_type,
            "changed_at": format_naive_utc_datetime(self.changed_at, tz_name, "%d.%m.%Y %H:%M"),
            "work_dates": [format_work_date_human(d) for d in preview],
            "work_dates_all": [format_work_date_human(d) for d in dates_sorted],
            "work_dates_extra": extra,
            "change_lines": self.change_lines,
            "change_lines_detail": self.change_lines_detail,
            "actor_name": self.actor_name or "система",
            "actor_is_self": bool(self.actor_is_self),
            "source": self.source,
            "batch_id": self.batch_id,
        }


def _group_key(row: MasterScheduleAuditLog) -> tuple:
    if row.batch_id:
        return ("batch", str(row.batch_id))
    return ("at", int(row.master_id), row.changed_at)


def _summarize_bulk_lines(
    by_date: dict[date, list[MasterScheduleAuditLog]],
    *,
    change_kind: str,
) -> tuple[list[str], list[str]]:
    """Вернуть (краткие строки, детальные строки)."""
    per_date_lines: dict[date, list[str]] = {}
    for d, rows in by_date.items():
        per_date_lines[d] = format_day_change_lines(rows, change_kind=change_kind)

    # Группировка одинаковых наборов строк по датам.
    signature_to_dates: dict[tuple[str, ...], list[date]] = {}
    for d, lines in sorted(per_date_lines.items(), key=lambda x: x[0]):
        sig = tuple(lines)
        signature_to_dates.setdefault(sig, []).append(d)

    if len(signature_to_dates) == 1:
        only_sig = next(iter(signature_to_dates.keys()))
        return list(only_sig), []

    short: list[str] = []
    detail: list[str] = []
    for sig, dates in signature_to_dates.items():
        wd_labels = ", ".join(_WD_SHORT[d.weekday()] for d in dates)
        if not sig:
            continue
        joined = "; ".join(sig)
        short.append(f"{wd_labels}: {joined}")
        for d in dates:
            detail.append(f"{format_work_date_human(d)} — {joined}")
    if len(short) > 4:
        return ["разные значения"], detail
    return short, detail


def build_schedule_history_groups(
    rows: list[MasterScheduleAuditLog],
    *,
    master_id: int,
) -> list[ScheduleHistoryGroup]:
    buckets: dict[tuple, list[MasterScheduleAuditLog]] = {}
    order: list[tuple] = []
    for r in rows:
        key = _group_key(r)
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(r)

    groups: list[ScheduleHistoryGroup] = []
    for key in order:
        items = buckets[key]
        by_date: dict[date, list[MasterScheduleAuditLog]] = {}
        for r in items:
            by_date.setdefault(r.work_date, []).append(r)
        work_dates = sorted(by_date.keys())
        is_bulk = len(work_dates) > 1 or str(items[0].source or "").startswith("bulk")
        # Тип по совокупности строк группы.
        kind = detect_change_kind(items)
        # Если в bulk смесь create/change — считаем «изменение».
        if is_bulk:
            kinds = {detect_change_kind(v) for v in by_date.values()}
            if kinds == {"create"}:
                kind = "create"
            elif kinds == {"delete"}:
                kind = "delete"
            else:
                kind = "change"

        if is_bulk:
            lines, detail = _summarize_bulk_lines(by_date, change_kind=kind)
        else:
            lines = format_day_change_lines(items, change_kind=kind)
            detail = []

        actor = items[0].changed_by_user
        actor_id = items[0].changed_by_user_id
        actor_name = None
        if actor is not None:
            actor_name = (actor.display_name or actor.username or "").strip() or None
        groups.append(
            ScheduleHistoryGroup(
                change_type=change_type_label(kind=kind, is_bulk=is_bulk),
                changed_at=items[0].changed_at,
                work_dates=work_dates,
                change_lines=lines,
                actor_name=actor_name,
                actor_is_self=bool(actor_id is not None and int(actor_id) == int(master_id)),
                batch_id=items[0].batch_id,
                source=str(items[0].source or "single"),
                change_lines_detail=detail,
            )
        )

    # Новые сверху
    groups.sort(key=lambda g: g.changed_at, reverse=True)
    return groups


def load_master_schedule_history(
    db: Session,
    *,
    master_id: int,
    changed_from: datetime | None = None,
    changed_to: datetime | None = None,
    offset: int = 0,
    limit: int = 20,
) -> dict[str, Any]:
    """Загрузить сгруппированную историю. По умолчанию — изменения за последние 30 дней."""
    tz_name = get_display_timezone(db)
    if changed_from is None:
        changed_from = datetime.utcnow() - timedelta(days=30)
    stmt = (
        select(MasterScheduleAuditLog)
        .options(selectinload(MasterScheduleAuditLog.changed_by_user))
        .where(MasterScheduleAuditLog.master_id == int(master_id))
        .where(MasterScheduleAuditLog.changed_at >= changed_from)
        .order_by(MasterScheduleAuditLog.changed_at.desc(), MasterScheduleAuditLog.id.desc())
    )
    if changed_to is not None:
        stmt = stmt.where(MasterScheduleAuditLog.changed_at < changed_to)
    rows = list(db.scalars(stmt).all())
    groups = build_schedule_history_groups(rows, master_id=int(master_id))
    total = len(groups)
    slice_g = groups[max(0, int(offset)) : max(0, int(offset)) + max(1, int(limit))]
    master = db.get(User, int(master_id))
    master_name = ""
    if master is not None:
        master_name = (master.display_name or master.username or "").strip()
    return {
        "master_id": int(master_id),
        "master_name": master_name,
        "tz": tz_name,
        "total_groups": total,
        "offset": int(offset),
        "limit": int(limit),
        "has_more": int(offset) + len(slice_g) < total,
        "groups": [g.to_dict(tz_name=tz_name) for g in slice_g],
    }


def parse_history_date_bound(raw: str | None, *, tz_name: str, end_of_day: bool = False) -> datetime | None:
    """Дата фильтра (YYYY-MM-DD) в локали студии → naive UTC."""
    s = (raw or "").strip()
    if not s:
        return None
    y, m, d = (int(x) for x in s.split("-", 2))
    if end_of_day:
        local = datetime(y, m, d, 23, 59, 59, 999999, tzinfo=ZoneInfo(tz_name))
    else:
        local = datetime(y, m, d, 0, 0, 0, tzinfo=ZoneInfo(tz_name))
    return local.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
