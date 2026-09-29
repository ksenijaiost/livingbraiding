"""Отчёт «Возвращаемость»: посетившие / новые / вернувшиеся / потерянные."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Client, User, UserRole, Visit, VisitMaster, VisitService, VisitServiceMaster
from app.operational_report import period_bounds
from app.user_roles import select_users_with_any_role


@dataclass(frozen=True)
class RetentionSlice:
    visited_total: int
    visited_new: int
    returned_total: int
    returned_new: int
    lost_total: int
    lost_new: int

    @property
    def returned_total_pct(self) -> float:
        return _pct(self.returned_total, self.visited_total)

    @property
    def returned_new_pct(self) -> float:
        return _pct(self.returned_new, self.visited_new)

    @property
    def lost_total_pct(self) -> float:
        return _pct(self.lost_total, self.visited_total)

    @property
    def lost_new_pct(self) -> float:
        return _pct(self.lost_new, self.visited_new)

    @property
    def visited_new_pct(self) -> float:
        return _pct(self.visited_new, self.visited_total)


@dataclass(frozen=True)
class MasterRetentionRow:
    master_id: int
    display_name: str
    slice: RetentionSlice
    lost_total_ids: tuple[int, ...]
    lost_new_ids: tuple[int, ...]


@dataclass(frozen=True)
class RetentionReport:
    visit_from: date
    visit_to: date
    return_from: date
    return_to: date
    company: RetentionSlice
    company_lost_total_ids: tuple[int, ...]
    company_lost_new_ids: tuple[int, ...]
    by_master: tuple[MasterRetentionRow, ...]
    filter_master_id: int | None = None


def default_retention_periods(*, today: date | None = None) -> tuple[date, date, date, date]:
    """Период возврата = текущий месяц до today; период посещения = предыдущий календарный месяц."""
    day = today or date.today()
    ret_from = day.replace(day=1)
    ret_to = day
    visit_to = ret_from - timedelta(days=1)
    visit_from = visit_to.replace(day=1)
    return visit_from, visit_to, ret_from, ret_to


def compute_retention_slice(
    *,
    visited_ids: Iterable[int],
    new_ids: Iterable[int],
    returned_ids: Iterable[int],
) -> tuple[RetentionSlice, tuple[int, ...], tuple[int, ...]]:
    visited = {int(x) for x in visited_ids}
    new = {int(x) for x in new_ids} & visited
    returned = {int(x) for x in returned_ids} & visited
    returned_new = returned & new
    lost = visited - returned
    lost_new = lost & new
    slice_ = RetentionSlice(
        visited_total=len(visited),
        visited_new=len(new),
        returned_total=len(returned),
        returned_new=len(returned_new),
        lost_total=len(lost),
        lost_new=len(lost_new),
    )
    return slice_, tuple(sorted(lost)), tuple(sorted(lost_new))


def is_new_in_visit_period(
    first_visit_date: date | None,
    *,
    visit_from: date,
    visit_to: date,
) -> bool:
    """Новый = первый неотменённый визит в истории попал в период посещения."""
    if first_visit_date is None:
        return False
    return visit_from <= first_visit_date <= visit_to


def build_retention_report(
    db: Session,
    *,
    visit_from: date,
    visit_to: date,
    return_from: date,
    return_to: date,
    filter_master_id: int | None = None,
) -> RetentionReport:
    v_start, v_end = period_bounds(visit_from, visit_to)
    r_start, r_end = period_bounds(return_from, return_to)

    visit_rows = _load_visit_client_master_rows(db, v_start, v_end)
    return_rows = _load_visit_client_master_rows(db, r_start, r_end)

    company_visited = {cid for cid, _mids in visit_rows}
    if filter_master_id is not None:
        mid = int(filter_master_id)
        company_visited = {cid for cid, mids in visit_rows if mid in mids}

    first_dates = _first_visit_dates(db, company_visited)
    company_new = {
        cid
        for cid in company_visited
        if is_new_in_visit_period(first_dates.get(cid), visit_from=visit_from, visit_to=visit_to)
    }
    company_returned = {cid for cid, _mids in return_rows} & company_visited
    if filter_master_id is not None:
        mid = int(filter_master_id)
        company_returned = {cid for cid, mids in return_rows if mid in mids} & company_visited

    company_slice, lost_total_ids, lost_new_ids = compute_retention_slice(
        visited_ids=company_visited,
        new_ids=company_new,
        returned_ids=company_returned,
    )

    master_ids_in_visit: set[int] = set()
    for cid, mids in visit_rows:
        if filter_master_id is not None and cid not in company_visited:
            continue
        for mid in mids:
            if filter_master_id is None or mid == int(filter_master_id):
                master_ids_in_visit.add(mid)

    # Первые визиты для всех клиентов, попадающих в разрез по мастерам
    all_master_client_ids: set[int] = set()
    for mid in master_ids_in_visit:
        all_master_client_ids.update(cid for cid, mids in visit_rows if mid in mids)
    missing_first = all_master_client_ids - set(first_dates)
    if missing_first:
        first_dates = {**first_dates, **_first_visit_dates(db, missing_first)}

    names = _master_names(db, master_ids_in_visit)
    by_master: list[MasterRetentionRow] = []
    for mid in sorted(master_ids_in_visit, key=lambda i: (names.get(i, "").lower(), i)):
        m_visited = {cid for cid, mids in visit_rows if mid in mids}
        m_new = {
            cid
            for cid in m_visited
            if is_new_in_visit_period(first_dates.get(cid), visit_from=visit_from, visit_to=visit_to)
        }
        # Вернулись к этому мастеру в периоде возврата
        m_returned = {cid for cid, mids in return_rows if mid in mids} & m_visited
        m_slice, m_lost_t, m_lost_n = compute_retention_slice(
            visited_ids=m_visited,
            new_ids=m_new,
            returned_ids=m_returned,
        )
        if m_slice.visited_total == 0:
            continue
        by_master.append(
            MasterRetentionRow(
                master_id=mid,
                display_name=names.get(mid, f"#{mid}"),
                slice=m_slice,
                lost_total_ids=m_lost_t,
                lost_new_ids=m_lost_n,
            )
        )

    return RetentionReport(
        visit_from=visit_from,
        visit_to=visit_to,
        return_from=return_from,
        return_to=return_to,
        company=company_slice,
        company_lost_total_ids=lost_total_ids,
        company_lost_new_ids=lost_new_ids,
        by_master=tuple(by_master),
        filter_master_id=filter_master_id,
    )


def list_active_masters(db: Session) -> list[User]:
    return list(
        db.scalars(
            select_users_with_any_role(UserRole.MASTER)
            .where(User.is_active.is_(True))
            .order_by(User.display_name.asc())
        ).all()
    )


def load_clients_brief(db: Session, client_ids: Iterable[int]) -> list[Client]:
    ids = sorted({int(x) for x in client_ids if int(x) > 0})
    if not ids:
        return []
    return list(db.scalars(select(Client).where(Client.id.in_(ids)).order_by(Client.name.asc())).all())


def _pct(num: int, den: int) -> float:
    if den <= 0:
        return 0.0
    return round(100.0 * float(num) / float(den), 1)


def _load_visit_client_master_rows(
    db: Session,
    start: datetime,
    end_excl: datetime,
) -> list[tuple[int, frozenset[int]]]:
    """Уникальные клиенты за период и множество мастеров на их визитах."""
    visits = list(
        db.scalars(
            select(Visit).where(
                Visit.performed_date >= start,
                Visit.performed_date < end_excl,
                Visit.is_cancelled.is_(False),
            )
        ).all()
    )
    if not visits:
        return []

    visit_ids = [int(v.id) for v in visits]
    client_by_visit = {int(v.id): int(v.client_id) for v in visits}

    masters_by_visit: dict[int, set[int]] = {vid: set() for vid in visit_ids}
    for vid, mid in db.execute(
        select(VisitMaster.visit_id, VisitMaster.master_id).where(VisitMaster.visit_id.in_(visit_ids))
    ).all():
        masters_by_visit.setdefault(int(vid), set()).add(int(mid))
    for vid, mid in db.execute(
        select(VisitService.visit_id, VisitServiceMaster.master_id)
        .join(VisitServiceMaster, VisitServiceMaster.visit_service_id == VisitService.id)
        .where(
            VisitService.visit_id.in_(visit_ids),
            VisitService.is_cancelled.is_(False),
        )
    ).all():
        masters_by_visit.setdefault(int(vid), set()).add(int(mid))

    by_client: dict[int, set[int]] = {}
    for vid, cid in client_by_visit.items():
        by_client.setdefault(cid, set()).update(masters_by_visit.get(vid, set()))
    return [(cid, frozenset(mids)) for cid, mids in by_client.items()]


def _first_visit_dates(db: Session, client_ids: Iterable[int]) -> dict[int, date]:
    ids = sorted({int(x) for x in client_ids if int(x) > 0})
    if not ids:
        return {}
    out: dict[int, date] = {}
    for cid, dt in db.execute(
        select(Visit.client_id, Visit.performed_date)
        .where(Visit.client_id.in_(ids), Visit.is_cancelled.is_(False))
        .order_by(Visit.client_id.asc(), Visit.performed_date.asc(), Visit.id.asc())
    ).all():
        cid_i = int(cid)
        if cid_i in out:
            continue
        d = dt.date() if isinstance(dt, datetime) else dt
        out[cid_i] = d
    return out


def _master_names(db: Session, master_ids: Iterable[int]) -> dict[int, str]:
    ids = sorted({int(x) for x in master_ids if int(x) > 0})
    if not ids:
        return {}
    return {
        int(u.id): str(u.display_name or f"#{u.id}")
        for u in db.scalars(select(User).where(User.id.in_(ids))).all()
    }


