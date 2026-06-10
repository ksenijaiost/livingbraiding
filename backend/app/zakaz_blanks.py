from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

_BLANK_CATALOG_CATEGORY = "Заказ"
_BLANK_CATALOG_SUBCATEGORY = "Заготовки поштучно"


@dataclass(frozen=True)
class ZakazBlankDef:
    key: str | None
    display_name: str
    section: str
    price: float
    work_pay: float
    include_in_kit_form: bool
    ignore_in_client_calc: bool = False
    exclude_from_inventory_piece_count: bool = False
    is_bu: bool = False


_BLANKS: tuple[ZakazBlankDef, ...] = (
    ZakazBlankDef("SE_BRAID_SHORT", "S.E. коса короткая", "SE", 75.0, 12.5, True),
    ZakazBlankDef("SE_BRAID_LONG", "S.E. коса", "SE", 85.0, 15.0, True),
    ZakazBlankDef("SE_BRAID_TRACERY_EASY", "S.E. ажурная коса (простая)", "SE", 150.0, 25.0, True),
    ZakazBlankDef("SE_BRAID_TRACERY_HARD", "S.E. ажурная коса (сложная)", "SE", 200.0, 35.0, True),
    ZakazBlankDef("SE_DREAD", "S.E. дред", "SE", 200.0, 40.0, True),
    ZakazBlankDef("SE_TIP_ADDON", "S.E. доплёт кончиков", "SE", 0.0, 5.0, True, True, False),
    ZakazBlankDef("SE_TRIM_SHORT", "S.E. стрижка короткой косы", "SE", 0.0, 2.0, True, True, True),
    ZakazBlankDef("SE_TRIM_LONG", "S.E. стрижка длинной косы", "SE", 0.0, 2.5, True, True, True),
    ZakazBlankDef("SE_BRAID_USED", "S.E. коса Б/У", "SE", 50.0, 0.0, False, False, False, True),
    ZakazBlankDef("SE_CURL", "S.E. термокудря (свободный кончик)", "SE", 200.0, 25.0, True),
    ZakazBlankDef("SE_CURL_USED", "S.E. термокудря Б/У", "SE", 100.0, 0.0, False, False, False, True),
    ZakazBlankDef("SE_FACTORY", "S.E. фабричные", "SE", 150.0, 25.0, True),
    ZakazBlankDef("DE_BRAID_SHORT", "D.E. коса короткая", "DE", 150.0, 25.0, True),
    ZakazBlankDef("DE_BRAID_LONG", "D.E. коса", "DE", 150.0, 30.0, True),
    ZakazBlankDef("DE_BRAID_USED", "D.E. коса Б/У", "DE", 100.0, 0.0, False, False, False, True),
    ZakazBlankDef("DE_BRAID_TRACERY", "D.E. ажурная коса (устар.)", "DE", 200.0, 35.0, False),
    ZakazBlankDef("DE_BRAID_TRACERY_HALF_EASY", "D.E. ажурка 1/2 простая", "DE", 160.0, 28.0, True),
    ZakazBlankDef("DE_BRAID_TRACERY_HALF_HARD", "D.E. ажурка 1/2 сложная", "DE", 210.0, 38.0, True),
    ZakazBlankDef("DE_BRAID_TRACERY_FULL_EASY", "D.E. ажурка 2/2 простая", "DE", 170.0, 30.0, True),
    ZakazBlankDef("DE_BRAID_TRACERY_FULL_HARD", "D.E. ажурка 2/2 сложная", "DE", 220.0, 40.0, True),
    ZakazBlankDef("DE_CURL", "D.E. термокудря (свободный кончик)", "DE", 200.0, 25.0, True),
    ZakazBlankDef("DE_CURL_DREAD", "D.E. дредокудря", "DE", 90.0, 35.0, True),
    ZakazBlankDef("DE_DREAD_SHORT", "D.E. дред короткий", "DE", 200.0, 40.0, True),
    ZakazBlankDef("DE_DREAD_LONG", "D.E. дред", "DE", 200.0, 50.0, True),
    ZakazBlankDef("DE_TRIM", "D.E. стрижка", "DE", 0.0, 5.0, True, True, True),
    ZakazBlankDef("DE_DREAD_MAX", "D.E. дред max", "DE", 250.0, 50.0, True),
    ZakazBlankDef("DE_CURL_MAX", "D.E. кудря max", "DE", 250.0, 25.0, True),
    ZakazBlankDef("DE_MICRO_BRAIDS_4X", "D.E. микрокосы 4х", "DE", 250.0, 35.0, True),
    ZakazBlankDef("DE_MICRO_BRAID_6X", "D.E. микрокоса 6х", "DE", 300.0, 40.0, True),
    ZakazBlankDef("DE_DREAD_USED", "D.E. дред Б/У", "DE", 150.0, 0.0, False, False, False, True),
)


def zakaz_blank_defs() -> tuple[ZakazBlankDef, ...]:
    return _BLANKS


def kit_form_blank_defs(section: str) -> list[ZakazBlankDef]:
    return [row for row in _BLANKS if row.include_in_kit_form and not row.is_bu and row.section == section]


def section_from_kit_key(kit_key: str) -> str | None:
    """D.E. / S.E. для фильтра состава комплекта: по префиксу ключа DE_ / SE_."""
    k = (kit_key or "").strip().upper()
    if k.startswith("SE_"):
        return "SE"
    if k.startswith("DE_"):
        return "DE"
    return None


def kit_composition_catalog_items(db: Session | None = None) -> list[dict[str, str]]:
    """Список {key, label, section} для выпадающего списка состава комплекта.

  База — встроенный справочник; активные строки прайса «Заготовки поштучно» с kit_key
  дополняют и переопределяют подписи. section для каталога — из префикса ключа.
    """
    by_key: dict[str, dict[str, str]] = {}
    for row in _BLANKS:
        if not row.include_in_kit_form or row.is_bu or not row.key:
            continue
        by_key[row.key] = {"key": row.key, "label": row.display_name, "section": row.section}

    if db is not None:
        from sqlalchemy import select

        from app.db.models import CatalogProduct

        catalog_rows = db.scalars(
            select(CatalogProduct).where(
                CatalogProduct.category_name == _BLANK_CATALOG_CATEGORY,
                CatalogProduct.subcategory_name == _BLANK_CATALOG_SUBCATEGORY,
                CatalogProduct.is_active.is_(True),
            )
        ).all()
        for cat_row in catalog_rows:
            try:
                meta = json.loads(cat_row.meta_json or "{}")
            except Exception:
                meta = {}
            if not isinstance(meta, dict):
                meta = {}
            kk = str(meta.get("kit_key") or "").strip()
            sec = section_from_kit_key(kk)
            if not kk or not sec:
                continue
            by_key[kk] = {
                "key": kk,
                "label": (str(cat_row.name or "").strip() or kk),
                "section": sec,
            }

    out = list(by_key.values())
    out.sort(key=lambda x: (x["section"], str(x["label"]).lower(), x["key"]))
    return out


def zakaz_blank_def_by_key() -> dict[str, ZakazBlankDef]:
    return {row.key: row for row in _BLANKS if row.key}
