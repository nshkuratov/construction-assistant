"""Сервер FastMCP: один типизированный инструмент на каждое зарегистрированное имя, все вызываются в
том же процессе через ``cli.dispatch`` под одной блокировкой (вызовы в процессе сервера идут по
очереди, поэтому у файла сессии чата один писатель). Каждый вызов учитывается в ``MCP_MAX_TOOL_CALLS_PER_RUN``.
"""

import asyncio
from typing import Literal

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError as FastMcpToolError

from src.config import Config
from src.mcp import schemas as s
from src.mcp.cli import dispatch
from src.mcp.errors import CIRCUIT_BREAKER, UNKNOWN_TOOL, VALIDATION, error_envelope
from src.mcp.registry import TOOL_REGISTRY


class _EnvelopeMCP(FastMCP):
    """FastMCP отклоняет неверный вызов (нет аргумента или он не того типа) своим текстом ошибки до
    запуска нашего обработчика. Приводим её к единому конверту, чтобы харнесс видел одну форму ошибки."""

    async def call_tool(self, name, arguments):
        try:
            return await super().call_tool(name, arguments)
        except FastMcpToolError as e:
            if name not in TOOL_REGISTRY:
                return error_envelope(UNKNOWN_TOOL, f"Unknown tool: {name}")
            return error_envelope(VALIDATION, str(e))


server = _EnvelopeMCP("construction-assistant")

_lock = asyncio.Lock()
_call_count = 0


def reset_tool_call_count() -> None:
    global _call_count
    _call_count = 0


async def call_tool(tool: str, params: dict) -> dict:
    """Последовательный вызов с ограничением числа вызовов. Всегда возвращает конверт."""
    global _call_count
    async with _lock:
        if _call_count >= Config.MCP_MAX_TOOL_CALLS_PER_RUN:
            return error_envelope(CIRCUIT_BREAKER, f"Tool call limit reached ({Config.MCP_MAX_TOOL_CALLS_PER_RUN})")
        _call_count += 1
        return await asyncio.to_thread(dispatch, tool, params)


@server.tool()
async def ping() -> dict:
    """Проверка работоспособности."""
    return await call_tool("ping", {})


@server.tool()
async def get_session() -> dict:
    """Текущее состояние чата: объект, черновик, уточнение, недавняя история."""
    return await call_tool("get_session", {})


@server.tool()
async def list_sites() -> dict:
    """Объекты, с которыми может работать прораб."""
    return await call_tool("list_sites", {})


@server.tool()
async def select_site(site: str) -> dict:
    """Делает объект активным для этого чата."""
    return await call_tool("select_site", {"site": site})


@server.tool()
async def resolve_date(text: str) -> dict:
    """Превращает русскую фразу о дате в дату ISO (относительно сегодняшнего дня)."""
    return await call_tool("resolve_date", {"text": text})


@server.tool()
async def find_positions(queries: list[str], unit: str | None = None, limit: int = 5) -> dict:
    """Ранжированные позиции-кандидаты для каждого разговорного названия работы."""
    return await call_tool("find_positions", {"queries": queries, "unit": unit, "limit": limit})


@server.tool()
async def get_position(position_id: int) -> dict:
    """Одна позиция с плановым, выполненным и оставшимся объёмом."""
    return await call_tool("get_position", {"position_id": position_id})


@server.tool()
async def get_remaining(
    queries: list[str] | None = None, position_id: int | None = None, position_ids: list[int] | None = None
) -> dict:
    """Остаток объёма (план минус выполнено) для названных работ или позиций."""
    return await call_tool(
        "get_remaining", {"queries": queries, "position_id": position_id, "position_ids": position_ids}
    )


@server.tool()
async def get_entries(day: str) -> dict:
    """Что уже записано за день (дата ISO)."""
    return await call_tool("get_entries", {"day": day})


@server.tool()
async def propose_draft(
    lines: list[s.DraftLineArg],
    day: str | None = None,
    replace: bool = True,
    skipped: list[s.SkippedArg] | None = None,
) -> dict:
    """Сохраняет черновик отчёта для подтверждения прорабом. Никогда не пишет в хранилища.

    ``skipped``: работы из сообщения, которых нет в смете, ``{source_text, reason}``; карточка их показывает.
    """
    return await call_tool(
        "propose_draft",
        {
            "lines": [ln.model_dump() for ln in lines],
            "skipped": [x.model_dump() for x in skipped or []],
            "day": day,
            "replace": replace,
        },
    )


@server.tool()
async def ask_clarification(question: str, options: list[s.ClarificationOptionArg], source_text: str = "") -> dict:
    """Просит прораба выбрать, когда название работы неизвестно или неоднозначно."""
    return await call_tool(
        "ask_clarification",
        {"question": question, "options": [o.model_dump() for o in options], "source_text": source_text},
    )


@server.tool()
async def send_report_form() -> dict:
    """Готовит для прораба образец формы отчёта."""
    return await call_tool("send_report_form", {})


@server.tool()
async def report_friction(summary: str, details: str | None = None) -> dict:
    """Оставляет отзыв для разработчика поддержки."""
    return await call_tool("report_friction", {"summary": summary, "details": details})


@server.tool()
async def finish_turn(outcome: Literal["draft", "question", "answer", "form", "error"], message: str = "") -> dict:
    """Завершает ход ОДНИМ исходом."""
    return await call_tool("finish_turn", {"outcome": outcome, "message": message})
