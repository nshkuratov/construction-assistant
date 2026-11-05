"""Реестр инструментов, заранее заполненный ВСЕМИ именами инструментов.

``TOOL_PHASES`` — единый список имён инструментов (все без ограничений, значение ``None``; имя
оставлено, чтобы тест инвариантов и обёртки сохраняли один импорт). Каждый модуль-обёртка привязывает
через ``register_tool`` только свои обработчики, поэтому этот словарь никто не правит. Тест инвариантов
проверяет, что заготовка один в один совпадает с зарегистрированными обработчиками.
"""

TOOL_REGISTRY: dict[str, object] = {}

TOOL_PHASES: dict[str, str | None] = {
    "ping": None,
    "get_session": None,
    "list_sites": None,
    "select_site": None,
    "resolve_date": None,
    "find_positions": None,
    "get_position": None,
    "get_remaining": None,
    "get_entries": None,
    "propose_draft": None,
    "ask_clarification": None,
    "send_report_form": None,
    "report_friction": None,
    "finish_turn": None,
}


def register_tool(name: str, handler) -> None:
    TOOL_REGISTRY[name] = handler
