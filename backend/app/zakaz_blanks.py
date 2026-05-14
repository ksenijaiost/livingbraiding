from __future__ import annotations

from dataclasses import dataclass


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
    ZakazBlankDef("SE_BRAID_USED", "S.E. коса Б/У", "SE", 50.0, 0.0, True, False, False, True),
    ZakazBlankDef("SE_CURL", "S.E. термокудря (свободный кончик)", "SE", 200.0, 25.0, True),
    ZakazBlankDef("SE_CURL_USED", "S.E. термокудря Б/У", "SE", 100.0, 0.0, True, False, False, True),
    ZakazBlankDef("SE_FACTORY", "S.E. фабричные", "SE", 150.0, 25.0, True),
    ZakazBlankDef("DE_BRAID_SHORT", "D.E. коса короткая", "DE", 150.0, 25.0, True),
    ZakazBlankDef("DE_BRAID_LONG", "D.E. коса", "DE", 150.0, 30.0, True),
    ZakazBlankDef("DE_BRAID_USED", "D.E. коса Б/У", "DE", 100.0, 0.0, True, False, False, True),
    ZakazBlankDef("DE_BRAID_TRACERY", "D.E. ажурная коса", "DE", 200.0, 35.0, True),
    ZakazBlankDef("DE_CURL", "D.E. термокудря (свободный кончик)", "DE", 200.0, 25.0, True),
    ZakazBlankDef("DE_CURL_DREAD", "D.E. дредокудря", "DE", 90.0, 35.0, True),
    ZakazBlankDef("DE_DREAD_SHORT", "D.E. дред короткий", "DE", 200.0, 40.0, True),
    ZakazBlankDef("DE_DREAD_LONG", "D.E. дред", "DE", 200.0, 50.0, True),
    ZakazBlankDef("DE_TRIM", "D.E. стрижка", "DE", 0.0, 5.0, True, True, True),
    ZakazBlankDef("DE_DREAD_MAX", "D.E. дред max", "DE", 250.0, 50.0, True),
    ZakazBlankDef("DE_CURL_MAX", "D.E. кудря max", "DE", 250.0, 25.0, True),
    ZakazBlankDef("DE_MICRO_BRAIDS_4X", "D.E. микрокосы 4х", "DE", 250.0, 35.0, True),
    ZakazBlankDef("DE_MICRO_BRAID_6X", "D.E. микрокоса 6х", "DE", 300.0, 40.0, True),
    ZakazBlankDef("DE_DREAD_USED", "D.E. дред Б/У", "DE", 150.0, 0.0, True, False, False, True),
)


def zakaz_blank_defs() -> tuple[ZakazBlankDef, ...]:
    return _BLANKS


def kit_form_blank_defs(section: str) -> list[ZakazBlankDef]:
    return [row for row in _BLANKS if row.include_in_kit_form and row.section == section]


def zakaz_blank_def_by_key() -> dict[str, ZakazBlankDef]:
    return {row.key: row for row in _BLANKS if row.key}
