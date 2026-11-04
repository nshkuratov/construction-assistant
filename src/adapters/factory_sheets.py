"""Выбирает шлюз к таблице по ``Config.SHEETS_BACKEND``."""

import json
from pathlib import Path

from src.adapters.sheets import GoogleSheetGateway, connect
from src.config import Config
from src.interfaces.errors import SheetsError
from src.interfaces.fakes import FakeSheetGateway
from src.interfaces.sheets import SheetGateway


def make_sheet_gateway() -> SheetGateway:
    backend = Config.SHEETS_BACKEND.strip().lower()
    if backend == "google":
        return GoogleSheetGateway(connect(Config.GOOGLE_SERVICE_ACCOUNT_FILE, Config.GOOGLE_SHEET_ID))
    if backend == "fixture":
        grid = json.loads(Path(Config.SHEET_FIXTURE_FILE).read_text(encoding="utf-8"))
        tabs = [t.strip() for t in Config.FIXTURE_SITES.split(",") if t.strip()]
        return FakeSheetGateway(grid, tabs)
    raise SheetsError(f"Неизвестный SHEETS_BACKEND={Config.SHEETS_BACKEND!r}: нужен google или fixture.")
