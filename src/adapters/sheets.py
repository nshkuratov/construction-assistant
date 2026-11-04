"""Реализация ``SheetGateway`` для Google Таблиц: одна таблица, по листу на объект.

Все решения о разметке принимает ``src/domain/sheet_layout.py``, а этот модуль занимается только вводом-выводом.
С ``Spreadsheet`` из gspread он говорит тремя вызовами (``fetch_sheet_metadata``, ``values_batch_get``,
``batch_update``), поэтому в тестах его можно заменить фейком в том же процессе.
"""

import logging
import time
from collections.abc import Callable
from datetime import date
from typing import Any, TypeVar

import gspread
from gspread.exceptions import APIError

from src.domain.sheet_layout import col_index, parse_grid, plan_date_column
from src.interfaces.errors import SheetsError
from src.models import CatalogSnapshot, CellWrite

logger = logging.getLogger(__name__)

T = TypeVar("T")

MAX_RETRIES = 3
_BACKOFF_BASE_S = 1.0
_HEADER_ROWS = 10  # разбор разметки ищет заголовки и строку дат в этих строках
_EPOCH = date(1899, 12, 30)
_DATE_LABEL = "объем"
_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def quote_tab(tab: str) -> str:
    """Имя листа как префикс A1: в ``'``, внутренние ``'`` удваиваются."""
    return "'" + tab.replace("'", "''") + "'"


def connect(service_account_file: str, spreadsheet_id: str) -> gspread.Spreadsheet:
    if not service_account_file or not spreadsheet_id:
        raise SheetsError("Не заданы GOOGLE_SERVICE_ACCOUNT_FILE и GOOGLE_SHEET_ID.")
    try:
        client = gspread.service_account(filename=service_account_file, scopes=_SCOPES)
        return client.open_by_key(spreadsheet_id)
    except FileNotFoundError:
        raise SheetsError(f"Файл ключа не найден: {service_account_file}") from None
    except (APIError, ValueError) as exc:
        raise _translate(exc) from exc


def _status(exc: Exception) -> int | None:
    return getattr(exc, "code", None) if isinstance(exc, APIError) else None


def _retryable(exc: Exception) -> bool:
    code = _status(exc)
    return code is not None and (code == 429 or code >= 500)


def _translate(exc: Exception) -> SheetsError:
    code = _status(exc)
    if code in (401, 403):
        return SheetsError("Нет доступа к таблице: дайте сервисному аккаунту права редактора.")
    if code == 404:
        return SheetsError("Таблица не найдена: проверьте GOOGLE_SHEET_ID.")
    if code == 429 or (code is not None and code >= 500):
        return SheetsError("Google Таблицы сейчас недоступны, попробуйте позже.")
    return SheetsError(f"Ошибка Google Таблиц: {exc}")


class GoogleSheetGateway:
    def __init__(self, spreadsheet: Any, sleep: Callable[[float], None] = time.sleep):
        self._ss = spreadsheet
        self._sleep = sleep
        self._gids: dict[str, int] = {}
        self._skipped: set[str] = set()

    def _with_retry(self, attempt: Callable[[], T]) -> T:
        """Повторяет ``attempt`` при 429/5xx. Попытка должна заново читать всё состояние, от которого зависит."""
        for n in range(MAX_RETRIES + 1):
            try:
                return attempt()
            except APIError as exc:
                if not _retryable(exc) or n == MAX_RETRIES:
                    raise _translate(exc) from exc
                logger.warning("Sheets call failed with %s; retry %d", _status(exc), n + 1)
                self._sleep(_BACKOFF_BASE_S * 2**n)
        raise AssertionError("unreachable")

    def _refresh_gids(self) -> None:
        meta = self._ss.fetch_sheet_metadata()
        self._gids = {s["properties"]["title"]: s["properties"]["sheetId"] for s in meta["sheets"]}

    def _gid(self, tab: str) -> int:
        if tab not in self._gids:
            self._with_retry(self._refresh_gids)
        try:
            return self._gids[tab]
        except KeyError:
            raise SheetsError(f"Лист «{tab}» не найден в таблице.") from None

    def _read(self, ranges: list[str], render: str) -> list[list[list]]:
        resp = self._with_retry(lambda: self._ss.values_batch_get(ranges, params={"valueRenderOption": render}))
        return [vr.get("values", []) for vr in resp["valueRanges"]]

    def _read_tab(self, tab: str, render: str) -> list[list]:
        self._gid(tab)
        return self._read([quote_tab(tab)], render)[0]

    def list_site_tabs(self) -> list[str]:
        self._with_retry(self._refresh_gids)
        titles = list(self._gids)
        if not titles:
            return []
        heads = self._read([f"{quote_tab(t)}!1:{_HEADER_ROWS}" for t in titles], "UNFORMATTED_VALUE")
        tabs = []
        for title, grid in zip(titles, heads, strict=True):
            try:
                parse_grid(grid)
            except ValueError as exc:
                if title not in self._skipped:
                    self._skipped.add(title)
                    logger.warning("Tab %r skipped: %s", title, exc)
                continue
            tabs.append(title)
        return tabs

    def load_snapshots(self, tabs: list[str]) -> dict[str, CatalogSnapshot]:
        if not tabs:
            return {}
        for tab in tabs:
            self._gid(tab)
        grids = self._read([quote_tab(t) for t in tabs], "UNFORMATTED_VALUE")
        out = {}
        for tab, grid in zip(tabs, grids, strict=True):
            try:
                out[tab] = parse_grid(grid, site_id=tab, sheet_url=self.sheet_url(tab))
            except ValueError as exc:
                raise SheetsError(f"Лист «{tab}» не похож на отчёт: {exc}") from exc
        return out

    def ensure_date_column(self, tab: str, day: date) -> str:
        gid = self._gid(tab)

        def attempt() -> str:
            grid = self._read([quote_tab(tab)], "FORMULA")[0]
            plan, has_dates = self._plan(tab, grid, day)
            if plan.insert:
                self._ss.batch_update({"requests": _insert_requests(gid, plan, day, has_dates)})
            return plan.column_letter

        return self._with_retry(attempt)

    def write_cells(self, tab: str, day: date, writes: list[CellWrite]) -> None:
        gid = self._gid(tab)

        def attempt() -> None:
            grid = self._read([quote_tab(tab)], "FORMULA")[0]
            self._check_rows(tab, grid, writes)
            plan, has_dates = self._plan(tab, grid, day)
            requests = _insert_requests(gid, plan, day, has_dates) if plan.insert else []
            requests += [_cell_request(gid, w.position_id, plan.column, w.value) for w in writes]
            if requests:
                self._ss.batch_update({"requests": requests})

        self._with_retry(attempt)

    def _plan(self, tab: str, grid: list[list], day: date):
        """План столбца даты и признак того, есть ли на листе столбец даты, откуда копировать формат."""
        try:
            return plan_date_column(grid, day), bool(parse_grid(grid).date_columns)
        except ValueError as exc:
            raise SheetsError(f"Лист «{tab}» не похож на отчёт: {exc}") from exc

    def _check_rows(self, tab: str, grid: list[list], writes: list[CellWrite]) -> None:
        try:
            names = {p.id: p.name for p in parse_grid(grid).positions}
        except ValueError as exc:
            raise SheetsError(f"Лист «{tab}» не похож на отчёт: {exc}") from exc
        for w in writes:
            if names.get(w.position_id) != " ".join(w.expected_name.split()):
                raise SheetsError(
                    f"Таблица изменилась: в строке {w.position_id} нет позиции «{w.expected_name}». "
                    "Ничего не записано, обновите данные."
                )

    def sheet_url(self, tab: str) -> str:
        return f"https://docs.google.com/spreadsheets/d/{self._ss.id}/edit#gid={self._gid(tab)}"


def _cell_request(gid: int, row: int, col: int, value: float | None) -> dict:
    cell: dict = {}
    if value is not None:
        cell["userEnteredValue"] = {"numberValue": value}
    return _update_cells(gid, row, col, [cell])


def _update_cells(gid: int, row: int, col: int, cells: list[dict]) -> dict:
    return {
        "updateCells": {
            "start": {"sheetId": gid, "rowIndex": row - 1, "columnIndex": col - 1},
            "rows": [{"values": cells}],
            "fields": "userEnteredValue",
        }
    }


def _insert_requests(gid: int, plan, day: date, has_dates: bool) -> list[dict]:
    """Вставляет столбец, копирует формат соседа, пишет заголовок с датой и обновляет формулы SUM."""
    col = plan.column
    reqs: list[dict] = [
        {
            "insertDimension": {
                "range": {"sheetId": gid, "dimension": "COLUMNS", "startIndex": col - 1, "endIndex": col},
                "inheritFromBefore": not plan.shift_right,
            }
        }
    ]
    neighbour = col + 1 if plan.shift_right else col - 1
    if has_dates:
        reqs.append(
            {
                "copyPaste": {
                    "source": {
                        "sheetId": gid,
                        "startColumnIndex": neighbour - 1,
                        "endColumnIndex": neighbour,
                    },
                    "destination": {"sheetId": gid, "startColumnIndex": col - 1, "endColumnIndex": col},
                    "pasteType": "PASTE_FORMAT",
                }
            }
        )
    serial = (day - _EPOCH).days
    reqs.append(_update_cells(gid, plan.date_row, col, [{"userEnteredValue": {"numberValue": serial}}]))
    reqs.append(_update_cells(gid, plan.date_row + 1, col, [{"userEnteredValue": {"stringValue": _DATE_LABEL}}]))
    for cell, formula in plan.formula_updates:
        letters = "".join(ch for ch in cell if ch.isalpha())
        row = int(cell[len(letters) :])
        reqs.append(_update_cells(gid, row, col_index(letters), [{"userEnteredValue": {"formulaValue": formula}}]))
    return reqs
