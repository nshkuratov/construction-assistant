"""Заполняет таблицу Google вкладкой на каждый объект, скопированной с образца ежедневного отчёта.

Запуск: ``python -m src.tools.seed_test_sheet --site Садик --site Школа``. Берёт
``GOOGLE_SERVICE_ACCOUNT_FILE`` и ``GOOGLE_SHEET_ID`` из конфигурации, если они не заданы опциями.
Повторный запуск безопасен: вкладка, где уже есть разбираемая раскладка отчёта, остаётся как есть.
"""

import argparse
import sys
from datetime import date, datetime
from pathlib import Path

import openpyxl

from src.adapters.sheets import GoogleSheetGateway, connect, quote_tab
from src.config import Config
from src.domain.sheet_layout import parse_grid
from src.interfaces.errors import SheetsError

DEFAULT_XLSX = Path("samples/sample_daily_report.xlsx")
_EPOCH = date(1899, 12, 30)
_MIN_COLS = 104  # столбец «выполнено» суммирует J:CZ, поэтому вкладка должна доходить до CZ
_DATE_FORMAT = {"type": "DATE", "pattern": "dd.mm.yyyy"}


def _value(v):
    if isinstance(v, datetime):
        return (v.date() - _EPOCH).days
    if isinstance(v, date):
        return (v - _EPOCH).days
    return v


def read_sample(path: Path) -> tuple[list[list], list[tuple[int, int, int, int]], int]:
    """Лист в виде строк значений (формулы как текст, даты как серийные числа), объединения ячеек и строка дат."""
    ws = openpyxl.load_workbook(path).active
    grid = [[_value(c) for c in row] for row in ws.iter_rows(values_only=True)]
    merges = [
        (m.min_row - 1, m.max_row, m.min_col - 1, m.max_col) for m in ws.merged_cells.ranges
    ]  # 0-based start, exclusive end, as the API wants
    date_row = next(
        i
        for i, row in enumerate(ws.iter_rows(min_row=1, max_row=10), start=1)
        for c in row
        if isinstance(c.value, datetime)
    )
    return grid, merges, date_row


def seed(spreadsheet, sites: list[str], xlsx: Path = DEFAULT_XLSX) -> dict[str, str]:
    """Создаёт или заполняет по одной вкладке на объект. Возвращает URL каждой вкладки."""
    grid, merges, date_row = read_sample(xlsx)
    gateway = GoogleSheetGateway(spreadsheet)
    meta = spreadsheet.fetch_sheet_metadata()
    existing = {s["properties"]["title"]: s["properties"]["sheetId"] for s in meta["sheets"]}
    for site in sites:
        if site in existing:
            if _holds_report(spreadsheet, site):
                continue
            if _has_content(spreadsheet, site):
                raise SheetsError(f"Лист «{site}» уже занят чужими данными; выберите другое имя.")
            gid = existing[site]
        else:
            gid = _add_tab(spreadsheet, site, len(grid), max(len(r) for r in grid))
        _fill(spreadsheet, gid, site, grid, merges, date_row)
    return {site: gateway.sheet_url(site) for site in sites}


def _holds_report(spreadsheet, tab: str) -> bool:
    values = spreadsheet.values_batch_get(
        [f"{quote_tab(tab)}!1:10"], params={"valueRenderOption": "UNFORMATTED_VALUE"}
    )["valueRanges"][0].get("values", [])
    try:
        parse_grid(values)
    except ValueError:
        return False
    return True


def _has_content(spreadsheet, tab: str) -> bool:
    values = spreadsheet.values_batch_get([quote_tab(tab)])["valueRanges"][0].get("values", [])
    return any(str(c).strip() for row in values for c in row)


def _add_tab(spreadsheet, title: str, rows: int, cols: int) -> int:
    props = {"title": title, "gridProperties": {"rowCount": max(rows, 100), "columnCount": max(cols, _MIN_COLS)}}
    resp = spreadsheet.batch_update({"requests": [{"addSheet": {"properties": props}}]})
    return resp["replies"][0]["addSheet"]["properties"]["sheetId"]


def _fill(spreadsheet, gid: int, tab: str, grid, merges, date_row: int) -> None:
    spreadsheet.values_batch_update(
        {
            "valueInputOption": "USER_ENTERED",
            "data": [{"range": f"{quote_tab(tab)}!A1", "values": [[c for c in row] for row in grid]}],
        }
    )
    requests = [
        {
            "mergeCells": {
                "range": {
                    "sheetId": gid,
                    "startRowIndex": r0,
                    "endRowIndex": r1,
                    "startColumnIndex": c0,
                    "endColumnIndex": c1,
                },
                "mergeType": "MERGE_ALL",
            }
        }
        for r0, r1, c0, c1 in merges
    ]
    requests.append(
        {
            "repeatCell": {
                "range": {
                    "sheetId": gid,
                    "startRowIndex": date_row - 1,
                    "endRowIndex": date_row,
                    "startColumnIndex": 9,
                },
                "cell": {"userEnteredFormat": {"numberFormat": _DATE_FORMAT}},
                "fields": "userEnteredFormat.numberFormat",
            }
        }
    )
    spreadsheet.batch_update({"requests": requests})


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--site", action="append", required=True, help="tab name; repeat for several sites")
    ap.add_argument("--spreadsheet-id", default=Config.GOOGLE_SHEET_ID)
    ap.add_argument("--service-account-file", default=Config.GOOGLE_SERVICE_ACCOUNT_FILE)
    ap.add_argument("--xlsx", type=Path, default=DEFAULT_XLSX)
    args = ap.parse_args(argv)
    try:
        urls = seed(connect(args.service_account_file, args.spreadsheet_id), args.site, args.xlsx)
    except SheetsError as exc:
        print(exc, file=sys.stderr)
        return 1
    for site, url in urls.items():
        print(f"{site}: {url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
