"""Единая конфигурация: переопределение через окружение с литеральными значениями по умолчанию.

``load_dotenv()`` вызывается при импорте, поэтому значения из ``.env`` доходят до каждого подпроцесса.
Каждый параметр читается через ``os.getenv`` или типизированный парсер ``_env_*`` с литеральным
значением по умолчанию. Остальной код читает переменные только через ``Config``, обращение
``Config.FOO`` идёт без экземпляра. При неверном значении типизированный парсер пишет в лог
и берёт значение по умолчанию, а не падает.
"""

import logging
import os

from dotenv import load_dotenv

# CA_SKIP_DOTENV=1 не пускает `.env` репозитория (с секретами) в тесты и в процесс MCP-инструментов.
if os.getenv("CA_SKIP_DOTENV", "").strip().lower() not in {"1", "true", "yes", "on"}:
    load_dotenv()

logger = logging.getLogger(__name__)


def _env_bool(name: str, default: str) -> bool:
    """Разбирает флаг окружения по общему правилу проекта. ``default`` — СТРОКА в стиле окружения."""
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    """Разбирает целое из окружения; при неверном значении пишет в лог и возвращает ``default``."""
    raw = os.getenv(name, str(default))
    try:
        return int(raw)
    except ValueError:
        logger.warning("Invalid %s=%r; falling back to %d", name, raw, default)
        return default


def _env_float(name: str, default: float) -> float:
    """Разбирает дробное из окружения; при неверном значении пишет в лог и возвращает ``default``."""
    raw = os.getenv(name, str(default))
    try:
        return float(raw)
    except ValueError:
        logger.warning("Invalid %s=%r; falling back to %s", name, raw, default)
        return default


class Config:
    # --- Telegram ---
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
    # Список id чатов через запятую. Пустой список запрещает всех.
    TELEGRAM_ALLOWED_CHAT_IDS = os.getenv("TELEGRAM_ALLOWED_CHAT_IDS", "")
    # Необязательный чат для уведомлений разработчику поддержки о трениях и расхождениях.
    SUPPORT_CHAT_ID = os.getenv("SUPPORT_CHAT_ID", "")

    # --- Engine ---
    DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
    ENGINE_BACKEND = os.getenv("ENGINE_BACKEND", "codex")  # codex | fake
    ENGINE_TURN_TIMEOUT_S = _env_int("ENGINE_TURN_TIMEOUT_S", 180)
    ENGINE_RETRIES = _env_int("ENGINE_RETRIES", 1)
    CODEX_COMMAND = os.getenv("CODEX_COMMAND", "codex")
    CODEX_MODEL = os.getenv("CODEX_MODEL", "deepseek-v4-flash")
    CODEX_REASONING_EFFORT = os.getenv("CODEX_REASONING_EFFORT", "medium")

    # --- Stores ---
    SHEETS_BACKEND = os.getenv("SHEETS_BACKEND", "fixture")  # google | fixture
    GOOGLE_SERVICE_ACCOUNT_FILE = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "")
    # Одна таблица, по листу на объект.
    GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "")
    SHEET_FIXTURE_FILE = os.getenv("SHEET_FIXTURE_FILE", "tests/fixtures/sample_grid.json")
    # Имена листов, которые показывает фейковый шлюз.
    FIXTURE_SITES = os.getenv("FIXTURE_SITES", "Садик,Школа")
    CRM_BACKEND = os.getenv("CRM_BACKEND", "fake")  # bitrix | fake
    BITRIX_WEBHOOK_URL = os.getenv("BITRIX_WEBHOOK_URL", "")
    BITRIX_FIELDS_FILE = os.getenv("BITRIX_FIELDS_FILE", "data/bitrix_fields.json")

    # --- Runtime ---
    CA_TZ = os.getenv("CA_TZ", "Europe/Moscow")
    CA_DATA_DIR = os.getenv("CA_DATA_DIR", "data")
    # Задаётся на каждый ход движка для процесса MCP. Пусто — у процесса нет чата.
    CA_CHAT_DIR = os.getenv("CA_CHAT_DIR", "")
    FRICTION_LOG = os.getenv("FRICTION_LOG", "logs/friction.jsonl")
    HISTORY_TURNS = _env_int("HISTORY_TURNS", 6)
    # Снимок старше этого возраста перечитывается перед ходом движка.
    SNAPSHOT_MAX_AGE_S = _env_int("SNAPSHOT_MAX_AGE_S", 60)
    CARD_PAGE_SIZE = _env_int("CARD_PAGE_SIZE", 8)
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

    # --- MCP server / limits ---
    # Предохранитель: максимум вызовов инструментов на процесс сервера (защита от зацикливания харнесса).
    MCP_MAX_TOOL_CALLS_PER_RUN = _env_int("MCP_MAX_TOOL_CALLS_PER_RUN", 100)
    # Журнал аудита только на добавление: строка на вызов инструмента, только поля из белого списка
    # (см. src/mcp/audit.py).
    ENABLE_RUN_TELEMETRY = _env_bool("ENABLE_RUN_TELEMETRY", "true")
    RUN_TELEMETRY_LOG = os.getenv("RUN_TELEMETRY_LOG", "logs/run_telemetry.jsonl")
