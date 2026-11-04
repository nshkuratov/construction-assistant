"""Шов хранилища таблицы. Синхронный; вызывающий код использует ``asyncio.to_thread``. Пишет только сервис фиксации."""

from datetime import date
from typing import Protocol

from src.models import CatalogSnapshot, CellWrite


class SheetGateway(Protocol):
    def list_site_tabs(self) -> list[str]: ...

    def load_snapshots(self, tabs: list[str]) -> dict[str, CatalogSnapshot]:
        """Одно пакетное чтение всех листов; каждый снимок содержит все даты."""
        ...

    def ensure_date_column(self, tab: str, day: date) -> str:
        """Возвращает букву столбца для ``day``; если столбца нет, создаёт его с соблюдением порядка дат."""
        ...

    def write_cells(self, tab: str, day: date, writes: list[CellWrite]) -> None:
        """Пишет итоговые значения в столбец ``day``. ``None`` очищает ячейку. Если имя в строке не равно
        ``expected_name``, вся запись отклоняется с ``SheetsError``."""
        ...

    def sheet_url(self, tab: str) -> str: ...
