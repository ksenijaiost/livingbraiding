"""Статусы клиентов: постоянные / спящие / пропавшие / не посещали / новые / чс."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Booking, BookingStatus, Client, Visit
from app.time_utils import utcnow_naive


class ClientStatus(str, Enum):
    REGULAR = "REGULAR"
    SLEEPING = "SLEEPING"
    LOST = "LOST"
    NEVER = "NEVER"
    NEW = "NEW"
    BLOCKED = "BLOCKED"
    RECENT = "RECENT"  # есть недавняя активность, но не «постоянный» и не «новый»


CLIENT_STATUS_LABELS: dict[ClientStatus, str] = {
    ClientStatus.REGULAR: "Постоянные",
    ClientStatus.SLEEPING: "Спящие",
    ClientStatus.LOST: "Пропавшие",
    ClientStatus.NEVER: "Не посещали",
    ClientStatus.NEW: "Новые",
    ClientStatus.BLOCKED: "Заблокированы",
    ClientStatus.RECENT: "Недавние",
}

CLIENT_STATUS_HINTS: dict[ClientStatus, str] = {
    ClientStatus.REGULAR: "2 и более записи за 3 месяца",
    ClientStatus.SLEEPING: "Не записывались более 3 месяцев",
    ClientStatus.LOST: "Не записывались более 6 месяцев",
    ClientStatus.NEVER: "Нет записей у этих клиентов",
    ClientStatus.NEW: "Услуги оказаны впервые",
    ClientStatus.BLOCKED: "В чёрном списке — вручную",
    ClientStatus.RECENT: "Есть запись за 3 месяца, но меньше двух",
}

CLIENT_STATUS_COLORS: dict[ClientStatus, str] = {
    ClientStatus.REGULAR: "#16a34a",
    ClientStatus.SLEEPING: "#ea580c",
    ClientStatus.LOST: "#dc2626",
    ClientStatus.NEVER: "#6b7280",
    ClientStatus.NEW: "#4b5563",
    ClientStatus.BLOCKED: "#dc2626",
    ClientStatus.RECENT: "#2563eb",
}

# Карточки статистики (порядок как на экране)
CLIENT_STATS_CARD_STATUSES: tuple[ClientStatus, ...] = (
    ClientStatus.REGULAR,
    ClientStatus.SLEEPING,
    ClientStatus.LOST,
    ClientStatus.NEVER,
    ClientStatus.BLOCKED,
    ClientStatus.NEW,
)

# Фильтр списка (без RECENT — он служебный в карточке)
CLIENT_STATUS_FILTER_OPTIONS: tuple[ClientStatus, ...] = (
    ClientStatus.REGULAR,
    ClientStatus.SLEEPING,
    ClientStatus.LOST,
    ClientStatus.NEVER,
    ClientStatus.NEW,
    ClientStatus.BLOCKED,
)


@dataclass(frozen=True)
class ClientActivity:
    dates: tuple[date, ...]

    @property
    def count(self) -> int:
        return len(self.dates)

    @property
    def last(self) -> date | None:
        return self.dates[-1] if self.dates else None


def parse_client_status(raw: str | None) -> ClientStatus | None:
    s = (raw or "").strip().upper()
    if not s:
        return None
    try:
        return ClientStatus(s)
    except ValueError:
        return None


def status_label(status: ClientStatus) -> str:
    return CLIENT_STATUS_LABELS.get(status, status.value)


def status_hint(status: ClientStatus) -> str:
    return CLIENT_STATUS_HINTS.get(status, "")


def compute_activity_status(activity: ClientActivity, *, today: date | None = None) -> ClientStatus:
    """Статус по истории записей (без учёта ЧС)."""
    day = today or utcnow_naive().date()
    if activity.count == 0:
        return ClientStatus.NEVER
    last = activity.last
    assert last is not None
    days_since = (day - last).days
    in_3m = _count_since(activity.dates, day - timedelta(days=90))
    if in_3m >= 2:
        return ClientStatus.REGULAR
    if activity.count == 1 and days_since <= 90:
        return ClientStatus.NEW
    if days_since <= 90:
        return ClientStatus.RECENT
    if days_since <= 180:
        return ClientStatus.SLEEPING
    return ClientStatus.LOST


def display_status(*, is_blacklisted: bool, activity: ClientActivity, today: date | None = None) -> ClientStatus:
    if is_blacklisted:
        return ClientStatus.BLOCKED
    return compute_activity_status(activity, today=today)


def stats_bucket(*, is_blacklisted: bool, activity: ClientActivity, today: date | None = None) -> ClientStatus | None:
    """Карточка статистики. ЧС отдельно; RECENT не попадает ни в одну карточку."""
    if is_blacklisted:
        return ClientStatus.BLOCKED
    st = compute_activity_status(activity, today=today)
    if st == ClientStatus.RECENT:
        return None
    return st


def load_client_activities(db: Session, client_ids: Iterable[int]) -> dict[int, ClientActivity]:
    ids = sorted({int(x) for x in client_ids if int(x) > 0})
    out: dict[int, list[date]] = {i: [] for i in ids}
    if not ids:
        return {}
    for cid, dt in db.execute(
        select(Visit.client_id, Visit.performed_date).where(
            Visit.client_id.in_(ids),
            Visit.is_cancelled.is_(False),
        )
    ).all():
        if dt is None:
            continue
        d = dt.date() if isinstance(dt, datetime) else dt
        out.setdefault(int(cid), []).append(d)
    for cid, dt in db.execute(
        select(Booking.client_id, Booking.planned_date).where(
            Booking.client_id.in_(ids),
            Booking.status != BookingStatus.CANCELLED,
        )
    ).all():
        if dt is None:
            continue
        d = dt.date() if isinstance(dt, datetime) else dt
        out.setdefault(int(cid), []).append(d)
    return {cid: ClientActivity(dates=tuple(sorted(dates))) for cid, dates in out.items()}


def activity_for_client(db: Session, client_id: int) -> ClientActivity:
    return load_client_activities(db, [client_id]).get(int(client_id), ClientActivity(dates=()))


def client_display_status(db: Session, client: Client, *, today: date | None = None) -> ClientStatus:
    act = activity_for_client(db, int(client.id))
    return display_status(is_blacklisted=bool(getattr(client, "is_blacklisted", False)), activity=act, today=today)


def is_first_non_cancelled_booking(db: Session, booking: Booking) -> bool:
    """Первая неотменённая бронь клиента — значок «новый» в расписании."""
    if booking.status == BookingStatus.CANCELLED:
        return False
    earlier = db.scalar(
        select(Booking.id)
        .where(
            Booking.client_id == booking.client_id,
            Booking.status != BookingStatus.CANCELLED,
            Booking.id != booking.id,
            (
                (Booking.planned_date < booking.planned_date)
                | ((Booking.planned_date == booking.planned_date) & (Booking.id < booking.id))
            ),
        )
        .limit(1)
    )
    if earlier is not None:
        return False
    # До этой брони уже были визиты — клиент не «новый»
    prior_visit = db.scalar(
        select(Visit.id)
        .where(
            Visit.client_id == booking.client_id,
            Visit.is_cancelled.is_(False),
            Visit.performed_date < (booking.planned_date or datetime.max),
        )
        .limit(1)
    )
    return prior_visit is None


def aggregate_client_status_counts(db: Session, *, today: date | None = None) -> dict[ClientStatus, int]:
    rows = list(db.execute(select(Client.id, Client.is_blacklisted)).all())
    acts = load_client_activities(db, [int(r[0]) for r in rows])
    counts = {st: 0 for st in CLIENT_STATS_CARD_STATUSES}
    day = today or utcnow_naive().date()
    for cid, blocked in rows:
        act = acts.get(int(cid), ClientActivity(dates=()))
        bucket = stats_bucket(is_blacklisted=bool(blocked), activity=act, today=day)
        if bucket is not None and bucket in counts:
            counts[bucket] += 1
    return counts


def filter_client_ids_by_status(
    db: Session,
    status: ClientStatus,
    *,
    today: date | None = None,
) -> set[int]:
    rows = list(db.execute(select(Client.id, Client.is_blacklisted)).all())
    acts = load_client_activities(db, [int(r[0]) for r in rows])
    day = today or utcnow_naive().date()
    out: set[int] = set()
    for cid, blocked in rows:
        act = acts.get(int(cid), ClientActivity(dates=()))
        if status == ClientStatus.BLOCKED:
            if blocked:
                out.add(int(cid))
            continue
        if blocked:
            continue
        if compute_activity_status(act, today=day) == status:
            out.add(int(cid))
    return out


def _count_since(dates: Iterable[date], since: date) -> int:
    return sum(1 for d in dates if d >= since)
