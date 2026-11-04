"""Общие фикстуры pytest: изолированное окружение ``Config`` и сетка образца таблицы."""

import os

os.environ["CA_SKIP_DOTENV"] = "1"  # до любого импорта src: `.env` репозитория не должен попасть в тест

import copy
import json
from pathlib import Path

import pytest

from src.interfaces.fakes import FakeCrmSink, FakeSheetGateway

GRID_PATH = Path(__file__).parent / "fixtures" / "sample_grid.json"
REPO_LOGS = Path(__file__).resolve().parent.parent / "logs"


def snapshot_logs(root: Path = REPO_LOGS) -> dict[str, tuple[int, int]]:
    """``{относительный путь: (размер, mtime_ns)}`` для каждого файла в ``root``."""
    if not root.exists():
        return {}
    return {
        str(f.relative_to(root)): (f.stat().st_size, f.stat().st_mtime_ns)
        for f in sorted(root.rglob("*"))
        if f.is_file()
    }


@pytest.fixture(scope="session", autouse=True)
def _repo_logs_untouched():
    """Падает, если какой-то тест записал в настоящий ``logs/`` (журнал живого бота)."""
    before = snapshot_logs()
    yield
    after = snapshot_logs()
    changed = sorted(k for k in before.keys() | after.keys() if before.get(k) != after.get(k))
    assert not changed, f"the test suite changed files under logs/: {changed}"


# Все переменные окружения, которые читает ``Config`` (src/config.py). Сбрасываются перед каждым тестом,
# чтобы настоящий `.env` (он грузится один раз при импорте) не попал в тест, который проверяет значение по умолчанию.
_CONFIG_ENV_VARS = (
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_ALLOWED_CHAT_IDS",
    "SUPPORT_CHAT_ID",
    "DEEPSEEK_API_KEY",
    "ENGINE_BACKEND",
    "ENGINE_TURN_TIMEOUT_S",
    "ENGINE_RETRIES",
    "CODEX_COMMAND",
    "CODEX_MODEL",
    "CODEX_REASONING_EFFORT",
    "SHEETS_BACKEND",
    "GOOGLE_SERVICE_ACCOUNT_FILE",
    "GOOGLE_SHEET_ID",
    "SHEET_FIXTURE_FILE",
    "FIXTURE_SITES",
    "CRM_BACKEND",
    "BITRIX_WEBHOOK_URL",
    "BITRIX_FIELDS_FILE",
    "CA_TZ",
    "CA_DATA_DIR",
    "CA_CHAT_DIR",
    "FRICTION_LOG",
    "HISTORY_TURNS",
    "SNAPSHOT_MAX_AGE_S",
    "CARD_PAGE_SIZE",
    "LOG_LEVEL",
    "MCP_MAX_TOOL_CALLS_PER_RUN",
    "ENABLE_RUN_TELEMETRY",
    "RUN_TELEMETRY_LOG",
)


@pytest.fixture(autouse=True)
def _clear_operator_env(monkeypatch, tmp_path):
    for name in _CONFIG_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    from src.config import Config

    monkeypatch.setattr(Config, "RUN_TELEMETRY_LOG", str(tmp_path / "telemetry.jsonl"))


@pytest.fixture
def sample_grid() -> list[list]:
    """Свежая глубокая копия образца таблицы (формулы как текст, даты как строки ISO)."""
    return copy.deepcopy(json.loads(GRID_PATH.read_text(encoding="utf-8")))


@pytest.fixture
def sheet_gateway(sample_grid) -> FakeSheetGateway:
    return FakeSheetGateway(sample_grid, ["Садик", "Школа"])


@pytest.fixture
def crm_sink() -> FakeCrmSink:
    return FakeCrmSink()


@pytest.fixture
def chat(tmp_path, monkeypatch, sample_grid):
    """Каталог чата с двумя снимками объектов, активным объектом и фиксированными часами (2026-09-02)."""
    from datetime import date

    from src.config import Config
    from src.domain.sheet_layout import parse_grid
    from src.session.store import SessionStore

    store = SessionStore(42, str(tmp_path))
    store.write_snapshot(parse_grid(sample_grid, site_id="sad"))
    store.write_snapshot(parse_grid(sample_grid, site_id="shk"))
    (store.dir / "sites.json").write_text(
        '[{"site_id":"sad","name":"Садик","sheet_tab":"Садик","crm_value_id":"1"},'
        '{"site_id":"shk","name":"Школа","sheet_tab":"Школа","crm_value_id":"2"}]',
        encoding="utf-8",
    )
    store.update_session(lambda s: setattr(s, "site_id", "sad"))
    monkeypatch.setattr(Config, "CA_CHAT_DIR", str(store.dir))
    monkeypatch.setattr(Config, "FRICTION_LOG", str(tmp_path / "friction.jsonl"))
    for module in ("catalog_tools", "session_tools"):
        monkeypatch.setattr(f"src.mcp.wrappers.{module}.today", lambda: date(2026, 9, 2))
    return store
