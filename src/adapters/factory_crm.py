"""Выбирает приёмник CRM по ``Config.CRM_BACKEND``."""

import json
from pathlib import Path

from src.adapters.bitrix import BitrixError, BitrixSink
from src.config import Config
from src.interfaces.crm import CrmSink
from src.interfaces.fakes import FakeCrmSink


def make_crm_sink() -> CrmSink:
    backend = Config.CRM_BACKEND
    if backend == "fake":
        return FakeCrmSink()
    if backend != "bitrix":
        raise ValueError(f"Unknown CRM_BACKEND={backend!r}; use bitrix or fake")
    if not Config.BITRIX_WEBHOOK_URL:
        raise BitrixError("Не задан BITRIX_WEBHOOK_URL.")
    path = Path(Config.BITRIX_FIELDS_FILE)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise BitrixError(f"Нет файла полей {path}. Запустите python -m src.tools.bitrix_setup.") from None
    except json.JSONDecodeError:
        raise BitrixError(f"Файл полей {path} повреждён. Запустите bitrix_setup ещё раз.") from None
    return BitrixSink(Config.BITRIX_WEBHOOK_URL, int(data["entityTypeId"]), data["fields"])
