"""Контракты доменных данных (pydantic). Каждая модель запрещает лишние ключи, поэтому опечатка харнесса отклоняется.

Нечёткую работу делает харнесс, а Python проверяет и считает. ``Position.done_total`` и
``Position.left`` считаются здесь, а не LLM.
"""

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator, model_validator


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Site(_Strict):
    site_id: str
    name: str
    sheet_tab: str
    crm_value_id: str


class Position(_Strict):
    id: int  # номер строки листа с единицы
    number: int | None = None
    section: str = ""
    group: str | None = None
    name: str
    unit: str
    unit_norm: str
    planned: float | None = None
    unit_price: float | None = None
    values: dict[str, float] = Field(default_factory=dict)  # дата ISO -> объём

    @model_validator(mode="before")
    @classmethod
    def _drop_computed(cls, data):
        """Выгруженная позиция содержит ``done_total`` и ``left``; их пересчитывают, а не берут на веру."""
        if isinstance(data, dict):
            data = {k: v for k, v in data.items() if k not in ("done_total", "left")}
        return data

    @computed_field
    @property
    def done_total(self) -> float:
        return round(sum(self.values.values()), 6)

    @computed_field
    @property
    def left(self) -> float | None:
        if self.planned is None:
            return None
        return round(self.planned - self.done_total, 6)


class CatalogSnapshot(_Strict):
    site_id: str
    sheet_url: str = ""
    taken_at: datetime
    date_columns: dict[str, str] = Field(default_factory=dict)  # дата ISO -> буква столбца
    positions: list[Position] = Field(default_factory=list)


class DraftLine(_Strict):
    line_id: int
    position_id: int | None = None
    mode: Literal["add", "set", "clear"]
    value: float | None = None
    unit: str = ""
    source_text: str = ""
    warnings: list[str] = Field(default_factory=list)
    # Непустой список означает неразрешённую строку: карточка просит прораба выбрать одного.
    candidates: list[int] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_value(self):
        if self.mode == "clear":
            if self.value is not None:
                raise ValueError("a clear line has no value")
        elif self.value is None or self.value <= 0:
            raise ValueError("an add or set line needs a value above 0")
        return self


class SkippedItem(_Strict):
    """Работа, названная прорабом, которую нельзя записать (например, её нет в смете)."""

    source_text: str
    reason: str = ""


class Draft(_Strict):
    draft_id: str
    version: int = 1
    site_id: str
    day: date
    day_source: Literal["default", "message", "user"] = "default"
    kind: Literal["new", "edit_saved"] = "new"
    lines: list[DraftLine] = Field(default_factory=list)
    status: Literal["pending", "committed"] = "pending"
    skipped: list[SkippedItem] = Field(default_factory=list)

    @field_validator("version")
    @classmethod
    def _version_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError("version starts at 1")
        return v


class ClarificationOption(_Strict):
    label: str
    position_id: int | None = None
    site_id: str | None = None
    day: date | None = None


class Clarification(_Strict):
    question: str
    options: list[ClarificationOption] = Field(default_factory=list)
    source_text: str = ""


class UiState(_Strict):
    view: Literal["card", "line_edit", "add_browse", "add_search", "date_pick", "site_pick", "saved"] = "card"
    line_id: int | None = None
    browse_path: list[str] = Field(default_factory=list)
    page: int = 0
    awaiting_input: Literal["number", "search", "date"] | None = None


class EngineOutcome(_Strict):
    outcome: Literal["draft", "question", "answer", "form", "error"]
    message: str = ""


class CommitResult(_Strict):
    draft_id: str
    sheets_ok: bool
    crm_ok: bool
    written: list[dict] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    crm_pending: list[dict] = Field(default_factory=list)


class ChatSession(_Strict):
    chat_id: int
    site_id: str | None = None
    draft: Draft | None = None
    clarification: Clarification | None = None
    ui: UiState = Field(default_factory=UiState)
    card_message_id: int | None = None
    history: list[dict] = Field(default_factory=list)
    last_commit: CommitResult | None = None
    turn_id: str | None = None
    turn_outcome: EngineOutcome | None = None


class CrmSite(_Strict):
    value_id: str
    name: str


class SiteListing(_Strict):
    sites: list[Site] = Field(default_factory=list)
    mismatches: list[str] = Field(default_factory=list)


class CellWrite(_Strict):
    position_id: int
    expected_name: str
    value: float | None = None  # None очищает ячейку


class CrmEntry(_Strict):
    site_name: str
    crm_value_id: str
    day: date
    position_key: str  # "<номер>|<название>" или "row<id>|<название>", если номера нет
    position_name: str
    section: str = ""
    unit: str = ""
    value: float
    unit_price: float | None = None
