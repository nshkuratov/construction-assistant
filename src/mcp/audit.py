"""Аудит вызовов инструментов: одна строка JSONL на вызов, добавляется в ``Config.RUN_TELEMETRY_LOG``.

Без секретов: ``arg_summary`` берёт только поля из белого списка, не содержащие чувствительных данных;
для остальных инструментов он даёт ``{}``. Текст прораба и токены в лог не попадают. По возможности:
неудачная запись теряет событие и не ломает вызов.
"""

import logging

from src.config import Config
from src.utils.persistence import append_jsonl
from src.utils.timeutil import utcnow_iso

logger = logging.getLogger(__name__)


def arg_summary(tool: str, args: dict) -> dict:
    if not isinstance(args, dict):
        return {}
    if tool == "finish_turn":
        return {"outcome": args.get("outcome")}
    if tool in ("find_positions", "get_remaining") and isinstance(args.get("queries"), list):
        return {"n_queries": len(args["queries"])}
    if tool == "propose_draft" and isinstance(args.get("lines"), list):
        return {"n_lines": len(args["lines"])}
    return {}


def record_tool_call(*, tool: str, arg_summary: dict, output_bytes: int, duration_ms: float, success: bool) -> None:
    """Добавляет одну строку аудита. Ничего не делает, если телеметрия выключена."""
    if not Config.ENABLE_RUN_TELEMETRY:
        return
    record = {
        "ts": utcnow_iso(),
        "tool": tool,
        "args": arg_summary,
        "output_bytes": output_bytes,
        "duration_ms": round(duration_ms, 3),
        "success": success,
    }
    try:
        append_jsonl(Config.RUN_TELEMETRY_LOG, record)
    except (TypeError, ValueError):
        logger.warning("Failed to record telemetry for %s; dropping the event", tool)
