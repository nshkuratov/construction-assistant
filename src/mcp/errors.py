"""Конверты и тип ошибки для слоя MCP.

Любой ответ инструмента, на любом пути, — ОДИН из двух:
``{"ok": true, ...result}`` или ``{"ok": false, "error": {"type": ..., "message": ...}}``.

"""

import dataclasses
import math
from datetime import date, datetime

VALIDATION = "validation"
UNKNOWN_TOOL = "unknown_tool"
NOT_IMPLEMENTED = "not_implemented"
INTERNAL = "internal"
CIRCUIT_BREAKER = "circuit_breaker"


class ToolError(Exception):
    """Обработчик отклоняет вызов типизированной ошибкой (``unit_mismatch``, ``not_found``, ...)."""

    def __init__(self, error_type: str, message: str):
        super().__init__(message)
        self.error_type = error_type
        self.message = message


def error_envelope(error_type: str, message: str) -> dict:
    return {"ok": False, "error": {"type": error_type, "message": message}}


def ok_envelope(result: dict | None = None) -> dict:
    return {"ok": True, **sanitize_for_json(result or {})}


def sanitize_for_json(obj):
    """Приводит результаты обработчиков (dataclass, модели pydantic, даты, NaN) к данным, безопасным для JSON."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return sanitize_for_json(dataclasses.asdict(obj))
    if hasattr(obj, "model_dump") and not isinstance(obj, type):
        return sanitize_for_json(obj.model_dump(mode="json"))
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, float) and math.isnan(obj):
        return None
    if isinstance(obj, dict):
        return {k: sanitize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [sanitize_for_json(v) for v in obj]
    return obj
