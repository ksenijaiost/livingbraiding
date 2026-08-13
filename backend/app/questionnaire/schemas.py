"""
Pydantic-схемы для `details_json` в `visit_services`.

Правило: сохранённый в БД JSON должен парситься в эти модели (или расширенные версии),
чтобы анкета и отчёты были предсказуемыми.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class KitFromStock(BaseModel):
    """Комплект из наличия: списание по артикулу и количеству заготовок."""

    model_config = ConfigDict(extra="forbid")

    sku: str = Field(..., description="Артикул комплекта на складе")
    blanks_used: int = Field(0, ge=0, description="Сколько заготовок списать")
    use_entire_kit: bool = Field(
        False,
        description="Если true — списать все доступные заготовки (blanks_used может быть 0)",
    )
    usage_by_key: dict[str, int] | None = Field(
        default=None,
        description="Списание по ключам состава (kit_key); None — прежняя скалярная модель",
    )


class KitNew(BaseModel):
    """Новый комплект (внесение в каталог)."""

    model_config = ConfigDict(extra="forbid")

    title: str
    description: str | None = None
    blanks_total: int = Field(..., ge=0)
    sku: str | None = Field(None, description="Необязателен, если использованы все заготовки")
    made_by_self: bool = Field(
        True,
        description="True, если изготовил тот же мастер, что заполняет анкету",
    )
    notes: str | None = None


class KitOwnExtra(BaseModel):
    """Доп. заготовки для «своего» комплекта: из наличия или новые."""

    model_config = ConfigDict(extra="forbid")

    source: Literal["STOCK", "NEW"]
    from_stock: KitFromStock | None = None
    from_stocks: list[KitFromStock] = Field(default_factory=list)
    new_kit: KitNew | None = None

    @model_validator(mode="before")
    @classmethod
    def _coerce_legacy_single_stock(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        if data.get("source") != "STOCK":
            return data
        if data.get("from_stocks"):
            return data
        fs = data.get("from_stock")
        if fs is not None:
            return {**data, "from_stocks": [fs]}
        return data

    @field_validator("from_stocks", mode="before")
    @classmethod
    def _from_stocks_none_to_empty(cls, v: Any) -> Any:
        if v is None:
            return []
        return v

    @model_validator(mode="after")
    def _validate_source(self):
        if self.source == "STOCK":
            stocks = [x for x in (self.from_stocks or []) if x is not None]
            if not stocks and self.from_stock is not None:
                stocks = [self.from_stock]
            if not stocks:
                raise ValueError("Для source=STOCK нужен from_stock или непустой from_stocks")
            return self.model_copy(update={"from_stocks": stocks, "from_stock": stocks[0]})
        if self.source == "NEW" and self.new_kit is None:
            raise ValueError("Для source=NEW нужен new_kit")
        return self


class KitOwnCorrectionDetails(BaseModel):
    """Детали коррекции для своего комплекта («Новый визит»), как в работе «Коррекция комплекта»."""

    model_config = ConfigDict(extra="ignore")

    trim_qty: int = Field(0, ge=0)
    hourly_hours: float = Field(0, ge=0)
    kit_description: str = ""
    kit_blanks_count: int | None = Field(default=None, ge=0)
    wash: bool = False
    circle: bool = False
    steam: bool = False
    use_custom_amount: bool = False
    custom_amount: float | None = Field(default=None, ge=0)
    master_id: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _validate_wash_circle(self):
        if self.wash and self.circle:
            raise ValueError("Если выбрана «Стирка», то «Одевание на круг» недоступно.")
        return self


class KitOwn(BaseModel):
    """Свой комплект (клиента/студии)."""

    model_config = ConfigDict(extra="forbid")

    origin: Literal["STUDIO", "FOREIGN"] = Field(
        ...,
        description="STUDIO — нашей студии, FOREIGN — чужой",
    )
    correction: bool
    correction_details: KitOwnCorrectionDetails | None = Field(default=None)
    extra_blanks: bool
    extra: KitOwnExtra | None = None

    @model_validator(mode="after")
    def _validate_extra(self):
        if self.extra_blanks and self.extra is None:
            raise ValueError("При extra_blanks=true нужен блок extra")
        if not self.extra_blanks and self.extra is not None:
            raise ValueError("При extra_blanks=false блок extra должен отсутствовать")
        return self

    @model_validator(mode="after")
    def _validate_correction(self):
        if not self.correction and self.correction_details is not None:
            raise ValueError("Блок correction_details допустим только при correction=true.")
        return self


class KitBlock(BaseModel):
    """
    Общий блок «комплект» для любой услуги, где он встречается.

    kind:
      STOCK — из наличия
      NEW — новый (данные как при внесении в таблицу комплектов)
      OWN — свой (не из ассортимента студии как обычная продажа)

    Для STOCK: в новых визитах задаётся ``from_stocks`` (несколько комплектов).
    Старое сохранённое ``from_stock`` (один комплект) при разборе дублируется в ``from_stocks``.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["STOCK", "NEW", "OWN"]
    from_stock: KitFromStock | None = None
    from_stocks: list[KitFromStock] = Field(default_factory=list)
    new_kit: KitNew | None = None
    own: KitOwn | None = None

    @model_validator(mode="before")
    @classmethod
    def _stock_coerce_legacy_single(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        if data.get("kind") != "STOCK":
            return data
        if data.get("from_stocks"):
            return data
        fs = data.get("from_stock")
        if fs is not None:
            return {**data, "from_stocks": [fs]}
        return data

    @field_validator("from_stocks", mode="before")
    @classmethod
    def _from_stocks_none_to_empty(cls, v: Any) -> Any:
        if v is None:
            return []
        return v

    @model_validator(mode="after")
    def _validate_kind(self):
        if self.kind == "STOCK":
            stocks = [x for x in (self.from_stocks or []) if x is not None]
            if not stocks and self.from_stock is not None:
                stocks = [self.from_stock]
            if len(stocks) < 1:
                raise ValueError("Для kind=STOCK нужен from_stock или непустой from_stocks")
            return self.model_copy(update={"from_stocks": stocks, "from_stock": stocks[0]})
        if self.kind == "NEW":
            if self.new_kit is None:
                raise ValueError("Для kind=NEW нужен new_kit")
        elif self.kind == "OWN":
            if self.own is None:
                raise ValueError("Для kind=OWN нужен own")
        return self


class ThermoTemplateNumbers(BaseModel):
    """Числовой шаблон термозамещения (пустые поля формы → 0 при сохранении)."""

    model_config = ConfigDict(extra="forbid")

    # Допускает десятичные; остальные поля шабона — только целые.
    strand_weight_avg: float = 0.0
    row_1: int = 0
    row_2: int = 0
    row_3: int = 0
    other_rows_text: str = ""
    temples: int = 0
    triangles: int = 0
    bird: int = 0
    square: int = 0
    comment: str = ""


class ThermoVisitDetails(BaseModel):
    """Основной блок термозамещения + либо новый заполненный шаблон, либо выбор сохранённого."""

    model_config = ConfigDict(extra="forbid")

    curls_material: str = ""
    material_length: str = ""
    shade: str = ""
    bases_total: int = 0
    weight_with_margin: float = 0.0
    template_mode: Literal["NEW", "OLD"]
    old_template_id: int | None = None
    algorithm_changes: str | None = None
    filled_template: ThermoTemplateNumbers | None = None
    saved_template_snapshot: ThermoTemplateNumbers | None = None


class VisitServiceDetailsPayload(BaseModel):
    """
    Полный `details_json` для строки услуги в визите.

    service_fields — устаревший контейнер (старые визиты с bases_count в JSON); новые — пустой {}.
    answers — универсальная анкета (вплетение и др.) по ключам полей из БД.
    answer_labels / answer_display — снимки для отчётов.
    """

    model_config = ConfigDict(extra="forbid")

    service_fields: dict[str, Any] = Field(default_factory=dict)
    # None — услуги без блока комплекта (не «Вплетение комплекта»).
    kit: KitBlock | None = None
    answers: dict[str, Any] = Field(default_factory=dict)
    answer_labels: dict[str, str] = Field(default_factory=dict)
    answer_display: dict[str, str] = Field(default_factory=dict)
    thermo: ThermoVisitDetails | None = None
    # Количество шт для категории «Работа по фикс цене»; для обычных услуг — None.
    fixed_price_qty: int | None = None


def parse_visit_service_details(data: object) -> VisitServiceDetailsPayload:
    """Разбор `details_json` (dict или JSON-совместимая структура) для строки услуги."""
    return VisitServiceDetailsPayload.model_validate(data)
