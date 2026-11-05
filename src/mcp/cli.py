"""Диспетчер инструментов: ``dispatch`` — единственная точка входа для каждого вызова; ``main`` — его
обёртка для командной строки (``python -m src.mcp.cli <tool> --json '<args>'``), а сервер вызывает
``dispatch`` в том же процессе.

Любой путь возвращает конверт из ``src/mcp/errors.py``: ошибки валидации, неизвестный инструмент,
типизированный ``ToolError`` и ожидаемый внутренний сбой. Исключение типа вне ``_EXPECTED_FAILURES`` —
это баг, он проявляется трассировкой, а не глотается.
"""

import argparse
import json
import sys
import time

from pydantic import ValidationError

from src.interfaces.errors import CrmError, SheetsError
from src.mcp.audit import arg_summary, record_tool_call
from src.mcp.errors import (
    INTERNAL,
    UNKNOWN_TOOL,
    VALIDATION,
    ToolError,
    error_envelope,
    ok_envelope,
)
from src.mcp.registry import TOOL_REGISTRY, register_tool
from src.mcp.schemas import TOOL_ARGS
from src.utils.timeutil import utcnow_iso

_EXPECTED_FAILURES = (OSError, LookupError, TypeError, RuntimeError, ArithmeticError, SheetsError, CrmError)


def _handle_ping(_args: dict) -> dict:
    return {"status": "ok", "timestamp": utcnow_iso()}


register_tool("ping", _handle_ping)

# Блок агрегирующих импортов: каждый модуль-обёртка регистрирует свои инструменты при импорте.
import src.mcp.wrappers.catalog_tools  # noqa: E402, F401
import src.mcp.wrappers.session_tools  # noqa: E402, F401


def _run(tool: str, params) -> dict:
    handler = TOOL_REGISTRY.get(tool)
    if handler is None:
        return error_envelope(UNKNOWN_TOOL, f"Unknown tool: {tool}")
    if not isinstance(params, dict):
        return error_envelope(VALIDATION, "Arguments must be a JSON object")
    try:
        args = TOOL_ARGS[tool].model_validate(params).model_dump()
    except ValidationError as e:
        detail = "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors())
        return error_envelope(VALIDATION, detail)
    try:
        return ok_envelope(handler(args))
    except ToolError as e:
        return error_envelope(e.error_type, e.message)
    except ValueError as e:
        return error_envelope(VALIDATION, str(e))
    except _EXPECTED_FAILURES as e:
        return error_envelope(INTERNAL, f"{type(e).__name__}: {e}")


def dispatch(tool: str, params) -> dict:
    """Выполняет один вызов инструмента и возвращает его конверт.

    Пишется в аудит; при ожидаемом сбое не бросает исключение.
    """
    start = time.monotonic()
    envelope = _run(tool, params)
    record_tool_call(
        tool=tool,
        arg_summary=arg_summary(tool, params),
        output_bytes=len(json.dumps(envelope, default=str)),
        duration_ms=(time.monotonic() - start) * 1000,
        success=envelope["ok"],
    )
    return envelope


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MCP CLI dispatcher")
    parser.add_argument("tool_name", help="Name of the tool to invoke")
    parser.add_argument("--json", dest="json_args", default="{}", help="JSON params")
    args = parser.parse_args(argv)
    try:
        params = json.loads(args.json_args)
    except json.JSONDecodeError as e:
        envelope = error_envelope(VALIDATION, f"Invalid JSON: {e}")
    else:
        envelope = dispatch(args.tool_name, params)
    print(json.dumps(envelope, ensure_ascii=False, default=str))
    return 0 if envelope["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
