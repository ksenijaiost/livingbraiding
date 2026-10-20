"""Запись телефона клиента и проверка, что номер ещё не занят."""

from __future__ import annotations

from markupsafe import Markup, escape
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Client
from app.phone_ru import display_phone, format_phone, normalize_phone, tel_href

__all__ = [
    "DuplicateClientPhone",
    "apply_client_phone",
    "display_phone",
    "find_client_by_normalized_phone",
    "tel_href",
]


class DuplicateClientPhone(ValueError):
    def __init__(self, client: Client) -> None:
        self.client_id = int(client.id)
        self.client_name = (client.name or "").strip() or "—"
        self.phone_label = display_phone(client.phone_normalized, client.phone) or "—"
        super().__init__(
            f"Клиент с этим номером уже есть: {self.client_name}, {self.phone_label} (/clients/{self.client_id})"
        )

    def html(self) -> Markup:
        href = f"/clients/{self.client_id}"
        return Markup(
            "Клиент с этим номером уже есть: "
            f'<a href="{href}">{escape(self.client_name)}, {escape(self.phone_label)}</a>'
        )


def find_client_by_normalized_phone(
    db: Session,
    normalized: str,
    *,
    exclude_id: int | None = None,
) -> Client | None:
    stmt = select(Client).where(Client.phone_normalized == normalized)
    if exclude_id is not None:
        stmt = stmt.where(Client.id != int(exclude_id))
    return db.scalar(stmt.order_by(Client.id.asc()).limit(1))


def apply_client_phone(
    client: Client,
    raw: str | None,
    db: Session,
    *,
    exclude_id: int | None = None,
) -> str | None:
    """Пересчитать phone и phone_normalized. Пусто — оба NULL. Дубль — DuplicateClientPhone."""
    normalized = normalize_phone(raw)
    skip_id = exclude_id if exclude_id is not None else getattr(client, "id", None)
    if normalized:
        other = find_client_by_normalized_phone(db, normalized, exclude_id=skip_id)
        if other is not None:
            raise DuplicateClientPhone(other)
        client.phone = format_phone(normalized)
        client.phone_normalized = normalized
        return normalized
    client.phone = None
    client.phone_normalized = None
    return None
