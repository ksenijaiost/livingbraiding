"""
Человекочитаемые подписи для админки (без имён Enum в интерфейсе).
"""

from __future__ import annotations

import json
from typing import Any

from app.db.models import MixComplexity, MixSource, Visit, VisitClientType, VisitPriceType, VisitService


def ru_client_type(ct: VisitClientType | str) -> str:
    v = ct.value if isinstance(ct, VisitClientType) else str(ct)
    return {
        "NEW": "Новый",
        "RETURNING": "Повторный",
        "SELF": "Свой",
    }.get(v, v)


def ru_price_type(pt: VisitPriceType | str) -> str:
    v = pt.value if isinstance(pt, VisitPriceType) else str(pt)
    return {
        "CLIENT": "Клиент",
        "MODEL": "Модель",
    }.get(v, v)


def ru_mix_source(ms: MixSource | str | None) -> str:
    if ms is None:
        return "—"
    v = ms.value if isinstance(ms, MixSource) else str(ms)
    return {
        "FROM_STOCK": "Из наличия",
        "NO_MIX": "Без смешки",
        "SELF_MIXED": "Сама мешала",
    }.get(v, v)


def ru_mix_complexity(c: MixComplexity | str | None) -> str:
    if c is None:
        return "—"
    v = c.value if isinstance(c, MixComplexity) else str(c)
    return {
        "SIMPLE": "Простая",
        "MEDIUM": "Средняя",
        "HARD": "Сложная",
    }.get(v, v)


def ru_kit_kind(kind: str) -> str:
    return {
        "STOCK": "Из наличия",
        "NEW": "Новый",
        "OWN": "Свой",
    }.get(kind, kind)


def ru_own_origin(o: str) -> str:
    return {"STUDIO": "Нашей студии", "FOREIGN": "Чужой"}.get(o, o)


def visit_primary_service_name(visit: Visit) -> str:
    if visit.services:
        return visit.services[0].service_name
    return "—"


def kit_usages_empty_explanation() -> str:
    return (
        "Со склада ничего не списывалось: выбран новый комплект или свой комплект "
        "без дополнительных заготовок из наличия."
    )


def build_service_human_display(vs: VisitService) -> dict[str, Any]:
    """Блоки для карточки услуги без сырого JSON."""
    blocks: list[tuple[str, str]] = [
        ("Категория", vs.category_name),
        ("Подкатегория", vs.subcategory_name),
        ("Услуга", vs.service_name),
    ]
    if not vs.details_json:
        catalog_line = f"{vs.category_name} / {vs.subcategory_name} / {vs.service_name}"
        return {"blocks": blocks, "catalog_line": catalog_line, "detail_blocks": []}

    try:
        data = json.loads(vs.details_json)
    except (json.JSONDecodeError, TypeError):
        err_blocks = blocks + [("Детали", "не удалось разобрать")]
        catalog_line = f"{vs.category_name} / {vs.subcategory_name} / {vs.service_name}"
        return {"blocks": err_blocks, "catalog_line": catalog_line, "detail_blocks": err_blocks[3:]}

    sf = data.get("service_fields") or {}
    if "bases_count" in sf:
        blocks.append(("Количество баз", str(sf["bases_count"])))
    if "blanks_count" in sf:
        blocks.append(("Количество заготовок (в работе)", str(sf["blanks_count"])))
    com = sf.get("service_comment")
    if com:
        blocks.append(("Комментарий по услуге", str(com)))

    ans = data.get("answers") or {}
    labels = data.get("answer_labels") or {}
    displays = data.get("answer_display") or {}
    if isinstance(ans, dict) and ans:
        for ak in sorted(ans.keys()):
            av = ans[ak]
            if isinstance(displays, dict) and ak in displays:
                avs = displays[ak]
            elif isinstance(av, bool):
                avs = "Да" if av else "Нет"
            elif av is None:
                avs = "—"
            else:
                avs = str(av)
            hdr = labels.get(ak) if isinstance(labels, dict) else None
            if hdr:
                blocks.append((hdr, avs))
            else:
                blocks.append((f"Вопрос ({ak})", avs))

    kit = data.get("kit") or {}
    kind = kit.get("kind") or "?"
    blocks.append(("Тип комплекта", ru_kit_kind(kind)))

    if kind == "STOCK":
        fs = kit.get("from_stock") or {}
        sku = fs.get("sku", "—")
        ent = fs.get("use_entire_kit")
        bu = fs.get("blanks_used", 0)
        if ent:
            blocks.append(("Со склада", f"арт. {sku}, весь комплект"))
        else:
            blocks.append(("Со склада", f"арт. {sku}, заготовок: {bu}"))
    elif kind == "NEW":
        nk = kit.get("new_kit") or {}
        blocks.append(("Новый комплект", nk.get("title") or "—"))
        if nk.get("description"):
            blocks.append(("Описание комплекта", str(nk["description"])))
        blocks.append(("Всего заготовок в комплекте", str(nk.get("blanks_total", "—"))))
        if nk.get("sku"):
            blocks.append(("Артикул", str(nk["sku"])))
        blocks.append(
            (
                "Изготовил тот же мастер",
                "Да" if nk.get("made_by_self") else "Нет",
            )
        )
        if nk.get("notes"):
            blocks.append(("Заметки", str(nk["notes"])))
    elif kind == "OWN":
        ow = kit.get("own") or {}
        blocks.append(("Происхождение", ru_own_origin(str(ow.get("origin", "")))))
        blocks.append(("Коррекция", "Да" if ow.get("correction") else "Нет"))
        blocks.append(("Дополнительные заготовки", "Да" if ow.get("extra_blanks") else "Нет"))
        ex = ow.get("extra")
        if ex and isinstance(ex, dict):
            src = ex.get("source")
            if src == "STOCK":
                fs = ex.get("from_stock") or {}
                sku = fs.get("sku", "—")
                if fs.get("use_entire_kit"):
                    blocks.append(("Доп. со склада", f"арт. {sku}, весь комплект"))
                else:
                    blocks.append(("Доп. со склада", f"арт. {sku}, шт: {fs.get('blanks_used', 0)}"))
            elif src == "NEW":
                nk = ex.get("new_kit") or {}
                blocks.append(("Доп. новые заготовки", nk.get("title") or "—"))
                if nk.get("blanks_total") is not None:
                    blocks.append(("Количество доп. заготовок", str(nk["blanks_total"])))

    catalog_line = f"{vs.category_name} / {vs.subcategory_name} / {vs.service_name}"
    detail_blocks = blocks[3:]
    return {"blocks": blocks, "catalog_line": catalog_line, "detail_blocks": detail_blocks}


def visit_services_catalog_line(visit: Visit) -> str:
    """Строка для списка визитов: категория / подкатегория / услуга (все строки визита)."""
    svcs = sorted(visit.services or [], key=lambda s: s.id)
    if not svcs:
        return "—"
    return "; ".join(
        f"{vs.category_name} / {vs.subcategory_name} / {vs.service_name}" for vs in svcs
    )
