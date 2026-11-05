"""Типизированные модели аргументов, по одной на инструмент. Сервер отдаёт их как входную схему MCP,
а диспетчер проверяет по ним каждый вызов."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NoArgs(_Args):
    pass


class SelectSiteArgs(_Args):
    site: str = Field(description="Site name or the site number from list_sites")


class ResolveDateArgs(_Args):
    text: str = Field(description='Russian date phrase: "вчера", "02.11", "2 ноября", a weekday')


class FindPositionsArgs(_Args):
    queries: list[str] = Field(min_length=1, description="One colloquial work name per item")
    unit: str | None = None
    limit: int = Field(default=5, ge=1, le=20)


class GetPositionArgs(_Args):
    position_id: int


class GetRemainingArgs(_Args):
    queries: list[str] | None = None
    position_id: int | None = None
    position_ids: list[int] | None = None

    @model_validator(mode="after")
    def _one_of(self):
        given = [bool(self.queries), self.position_id is not None, bool(self.position_ids)]
        if sum(given) != 1:
            raise ValueError("give exactly one of queries, position_id, position_ids")
        return self


class GetEntriesArgs(_Args):
    day: str = Field(description="ISO date YYYY-MM-DD")


class DraftLineArg(_Args):
    """У определённой строки есть ``position_id``. У неопределённой есть ``candidates`` и нет
    ``position_id``; прораб выбирает вариант в карточке."""

    position_id: int | None = None
    mode: Literal["add", "set", "clear"] = "add"
    value: float | None = None
    unit: str = ""
    source_text: str = ""
    candidates: list[int] = Field(default_factory=list)


class SkippedArg(_Args):
    source_text: str
    reason: str = ""


class ProposeDraftArgs(_Args):
    lines: list[DraftLineArg]
    skipped: list[SkippedArg] = Field(
        default_factory=list,
        description="Works of the message that are not in the estimate: shown on the card, never written",
    )
    day: str | None = Field(default=None, description="ISO date; omit for the default day")
    replace: bool = True


class ClarificationOptionArg(_Args):
    label: str
    position_id: int | None = None
    site_id: str | None = None
    day: str | None = None


class AskClarificationArgs(_Args):
    question: str
    options: list[ClarificationOptionArg] = Field(default_factory=list)
    source_text: str = ""


class ReportFrictionArgs(_Args):
    summary: str
    details: str | None = None


class FinishTurnArgs(_Args):
    outcome: Literal["draft", "question", "answer", "form", "error"]
    message: str = ""


TOOL_ARGS: dict[str, type[_Args]] = {
    "ping": NoArgs,
    "get_session": NoArgs,
    "list_sites": NoArgs,
    "select_site": SelectSiteArgs,
    "resolve_date": ResolveDateArgs,
    "find_positions": FindPositionsArgs,
    "get_position": GetPositionArgs,
    "get_remaining": GetRemainingArgs,
    "get_entries": GetEntriesArgs,
    "propose_draft": ProposeDraftArgs,
    "ask_clarification": AskClarificationArgs,
    "send_report_form": NoArgs,
    "report_friction": ReportFrictionArgs,
    "finish_turn": FinishTurnArgs,
}
