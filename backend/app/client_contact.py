"""Контактное лицо клиента: валидация, отображение, резолв каналов уведомлений."""

from __future__ import annotations

from markupsafe import Markup, escape
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.db.models import Client, NotificationChannel
from app.phone_ru import display_phone, format_phone

PENDING_KIND_CHOICE = "await_booking_choice"
PENDING_KIND_SELECTED = "selected_booking"
PENDING_TTL_MINUTES = 30

CLIENT_FOR_LINE_PREFIX = "Запись для: "


def client_has_messenger_channel(client: Client | None) -> bool:
    if client is None:
        return False
    return (
        getattr(client, "vk_user_id", None) is not None
        or getattr(client, "telegram_chat_id", None) is not None
        or getattr(client, "max_user_id", None) is not None
    )


def client_own_channel_targets(client: Client) -> list[tuple[NotificationChannel, int]]:
    out: list[tuple[NotificationChannel, int]] = []
    if client.vk_user_id is not None:
        out.append((NotificationChannel.VK, int(client.vk_user_id)))
    if client.max_user_id is not None:
        out.append((NotificationChannel.MAX, int(client.max_user_id)))
    if client.telegram_chat_id is not None:
        out.append((NotificationChannel.TELEGRAM, int(client.telegram_chat_id)))
    return out


def resolve_client_notify_targets(
    client: Client,
) -> tuple[list[tuple[NotificationChannel, int]], Client | None]:
    """Свои каналы клиента; иначе каналы контактного лица. Второй элемент — via contact или None."""
    own = client_own_channel_targets(client)
    if own:
        return own, None
    contact = getattr(client, "contact_client", None)
    if contact is None and getattr(client, "contact_client_id", None):
        # relationship may be unloaded; caller should eager-load when possible
        return [], None
    if contact is not None:
        via = client_own_channel_targets(contact)
        if via:
            return via, contact
    return [], None


def resolve_client_notify_targets_db(
    db: Session, client: Client
) -> tuple[list[tuple[NotificationChannel, int]], Client | None]:
    own = client_own_channel_targets(client)
    if own:
        return own, None
    cid = getattr(client, "contact_client_id", None)
    if not cid:
        return [], None
    contact = db.get(Client, int(cid))
    if contact is None:
        return [], None
    via = client_own_channel_targets(contact)
    if via:
        return via, contact
    return [], None


def client_for_line(client: Client, *, via_contact: Client | None) -> str:
    if via_contact is None:
        return ""
    name = (client.name or "").strip() or "—"
    return f"{CLIENT_FOR_LINE_PREFIX}{name}"


def parse_contact_relation(raw: object) -> str | None:
    text = str(raw or "").strip()
    if not text:
        return None
    if len(text) > 50:
        raise ValueError("«Кем приходится» — не длиннее 50 символов.")
    return text


def validate_and_apply_contact(
    db: Session,
    client: Client,
    *,
    contact_client_id_raw: object,
    contact_relation_raw: object,
    phone_normalized: str | None,
    is_create: bool,
) -> str | None:
    """Применить контакт к client. Возвращает предупреждение (legacy) или None. Raises ValueError."""
    has_phone = bool(phone_normalized)
    relation = parse_contact_relation(contact_relation_raw)

    raw_id = str(contact_client_id_raw or "").strip()
    contact_id: int | None = None
    if raw_id:
        if not raw_id.isdigit():
            raise ValueError("Некорректное контактное лицо.")
        contact_id = int(raw_id)

    if has_phone:
        client.contact_client_id = None
        client.contact_relation = None
        return None

    # phone empty
    if contact_id is None:
        if client_has_messenger_channel(client):
            client.contact_client_id = None
            client.contact_relation = None
            return None
        # legacy edit: allow without contact
        if not is_create and getattr(client, "id", None):
            existing_contact = getattr(client, "contact_client_id", None)
            if existing_contact is None:
                client.contact_client_id = None
                client.contact_relation = None
                return (
                    "У клиента нет телефона и контактного лица. "
                    "Укажите телефон, контактное лицо или привяжите канал уведомлений."
                )
        raise ValueError(
            "Укажите контактное лицо (телефон пуст) или привяжите канал VK / Telegram / Max."
        )

    if getattr(client, "id", None) and int(contact_id) == int(client.id):
        raise ValueError("Клиент не может быть контактным лицом сам себе.")

    contact = db.get(Client, int(contact_id))
    if contact is None:
        raise ValueError("Контактное лицо не найдено.")
    if not contact.phone_normalized:
        raise ValueError("У контактного лица должен быть свой номер телефона.")
    if getattr(contact, "contact_client_id", None) is not None:
        raise ValueError(
            "Нельзя выбрать контактным лицом клиента, у которого уже указано своё контактное лицо."
        )

    client.contact_client_id = int(contact_id)
    client.contact_relation = relation
    return None


def load_contact_client(db: Session, client: Client) -> Client | None:
    cid = getattr(client, "contact_client_id", None)
    if not cid:
        return None
    return db.get(Client, int(cid))


def contact_dependents(db: Session, contact_client_id: int) -> list[Client]:
    return list(
        db.scalars(
            select(Client)
            .where(Client.contact_client_id == int(contact_client_id))
            .order_by(Client.name.asc(), Client.id.asc())
        ).all()
    )


def contact_circle_client_ids(db: Session, sender: Client) -> list[int]:
    """Отправитель + все, для кого он контактное лицо."""
    ids = [int(sender.id)]
    for dep in contact_dependents(db, int(sender.id)):
        ids.append(int(dep.id))
    return ids


def via_contact_preview(client: Client, contact: Client | None = None) -> str:
    """Текст без HTML: «через: Имя (мама)»."""
    c = contact
    if c is None:
        c = getattr(client, "contact_client", None)
    if c is None:
        return ""
    name = (c.name or "").strip() or "—"
    rel = (getattr(client, "contact_relation", None) or "").strip()
    if rel:
        return f"через: {name} ({rel})"
    return f"через: {name}"


def via_contact_html(client: Client, contact: Client | None = None) -> Markup:
    """HTML: через: <a>Имя</a> (мама), +7…"""
    c = contact
    if c is None:
        c = getattr(client, "contact_client", None)
    if c is None and not getattr(client, "contact_client_id", None):
        return Markup("")
    if c is None:
        return Markup(escape(via_contact_preview(client)))
    name = (c.name or "").strip() or "—"
    rel = (getattr(client, "contact_relation", None) or "").strip()
    phone = display_phone(c.phone_normalized, c.phone) or format_phone(c.phone_normalized or "") or ""
    rel_part = f" ({escape(rel)})" if rel else ""
    phone_part = f", {escape(phone)}" if phone else ""
    href = f"/clients/{int(c.id)}"
    return Markup(
        f'через: <a href="{href}">{escape(name)}</a>{rel_part}{phone_part}'
    )


def client_phone_or_via_html(client: Client) -> Markup:
    own = display_phone(getattr(client, "phone_normalized", None), getattr(client, "phone", None))
    if own:
        return Markup(escape(own))
    html = via_contact_html(client)
    if html:
        return html
    return Markup("—")


def client_contact_preview_text(client: Client) -> str | None:
    own = display_phone(getattr(client, "phone_normalized", None), getattr(client, "phone", None))
    if own:
        return own
    preview = via_contact_preview(client)
    if preview:
        contact = getattr(client, "contact_client", None)
        if contact is not None:
            phone = display_phone(contact.phone_normalized, contact.phone)
            if phone:
                return f"{preview}, {phone}"
        return preview
    for attr in ("telegram", "vk", "instagram", "other_contact"):
        val = (getattr(client, attr, None) or "").strip()
        if val:
            return val
    return None


def expand_phone_search_clients(db: Session, phone_matched: list[Client]) -> list[Client]:
    """Владельцы номера + привязанные к ним (через contact_client_id)."""
    if not phone_matched:
        return []
    owner_ids = [int(c.id) for c in phone_matched]
    deps = list(
        db.scalars(
            select(Client)
            .where(Client.contact_client_id.in_(owner_ids))
            .options(selectinload(Client.contact_client))
            .order_by(Client.name.asc(), Client.id.asc())
        ).all()
    )
    seen: set[int] = set()
    out: list[Client] = []
    for c in phone_matched + deps:
        cid = int(c.id)
        if cid in seen:
            continue
        seen.add(cid)
        out.append(c)
    return out


def suggest_hint_for_client(c: Client) -> str:
    parts: list[str] = []
    shown_phone = display_phone(c.phone_normalized, c.phone)
    if shown_phone:
        parts.append(shown_phone)
    elif getattr(c, "contact_client_id", None):
        contact = getattr(c, "contact_client", None)
        if contact is not None:
            via = via_contact_preview(c, contact)
            phone = display_phone(contact.phone_normalized, contact.phone)
            if via and phone:
                parts.append(f"{via}, {phone}")
            elif via:
                parts.append(via)
        else:
            parts.append(via_contact_preview(c) or "через контакт")
    if c.telegram:
        parts.append(f"TG {c.telegram}")
    if c.vk:
        parts.append(f"VK {c.vk}")
    if c.instagram:
        parts.append(f"IG {c.instagram}")
    if c.other_contact:
        parts.append((c.other_contact or "")[:48])
    return " · ".join(parts) if parts else "без контакта"


def count_contact_for_clients(db: Session, client_id: int) -> int:
    from sqlalchemy import func

    return int(
        db.scalar(
            select(func.count()).select_from(Client).where(Client.contact_client_id == int(client_id))
        )
        or 0
    )


def clients_matching_phone_or_via(
    db: Session, phone_digits: str, *, limit: int = 30, name_needle: str | None = None
) -> list[Client]:
    """Поиск: имя и/или телефон (владельцы + через)."""
    conds = []
    if name_needle:
        conds.append(Client.name.ilike(f"%{name_needle}%"))
    owners: list[Client] = []
    if phone_digits:
        owners = list(
            db.scalars(
                select(Client)
                .where(Client.phone_normalized.like(f"%{phone_digits}%"))
                .order_by(Client.name.asc())
                .limit(limit)
            ).all()
        )
    if name_needle and not phone_digits:
        return list(
            db.scalars(
                select(Client)
                .where(Client.name.ilike(f"%{name_needle}%"))
                .options(selectinload(Client.contact_client))
                .order_by(Client.name.asc())
                .limit(limit)
            ).all()
        )
    if phone_digits:
        expanded = expand_phone_search_clients(db, owners)
        if name_needle:
            needle_l = name_needle.casefold()
            expanded = [
                c
                for c in expanded
                if needle_l in (c.name or "").casefold()
                or c in owners
                or any(
                    needle_l in (getattr(o, "name", "") or "").casefold()
                    for o in owners
                    if int(getattr(c, "contact_client_id", 0) or 0) == int(o.id)
                )
            ]
            # also add name-only matches
            name_rows = list(
                db.scalars(
                    select(Client)
                    .where(Client.name.ilike(f"%{name_needle}%"))
                    .options(selectinload(Client.contact_client))
                    .order_by(Client.name.asc())
                    .limit(limit)
                ).all()
            )
            seen = {int(c.id) for c in expanded}
            for c in name_rows:
                if int(c.id) not in seen:
                    expanded.append(c)
                    seen.add(int(c.id))
        return expanded[:limit]
    return []
