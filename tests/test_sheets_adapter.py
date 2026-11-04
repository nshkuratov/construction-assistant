"""GoogleSheetGateway против фейка gspread Spreadsheet в том же процессе (без сети)."""

import copy
import json
import re
from datetime import date
from pathlib import Path

import pytest
from gspread.exceptions import APIError

from src.adapters.factory_sheets import make_sheet_gateway
from src.adapters.sheets import GoogleSheetGateway, quote_tab
from src.config import Config
from src.domain.sheet_layout import col_index, col_letter, parse_date, parse_grid
from src.interfaces.errors import SheetsError
from src.interfaces.fakes import FakeSheetGateway
from src.models import CellWrite
from src.tools.seed_test_sheet import seed

GRID = json.loads((Path(__file__).parent / "fixtures" / "sample_grid.json").read_text(encoding="utf-8"))
XLSX = Path(__file__).parent.parent / "samples" / "sample_daily_report.xlsx"
_REF = re.compile(r"(\$?)([A-Z]{1,3})(\$?\d+)")


class _Resp:
    def __init__(self, code: int):
        self.code = code

    def json(self):
        return {"error": {"code": self.code, "message": "boom", "status": "X"}}


def api_error(code: int) -> APIError:
    return APIError(_Resp(code))


def _to_serial(v):
    d = parse_date(v) if isinstance(v, str) and re.fullmatch(r"\d{4}-\d\d-\d\d", v) else None
    return (d - date(1899, 12, 30)).days if d else v


def _shift_refs(formula: str, at: int) -> str:
    """Google сдвигает каждую ссылку на вставленную колонку и правее, поэтому левая граница диапазона
    сдвигается, а диапазон, который охватывает точку вставки, растёт."""
    return _REF.sub(
        lambda m: f"{m[1]}{col_letter(col_index(m[2]) + 1) if col_index(m[2]) >= at else m[2]}{m[3]}", formula
    )


class FakeSpreadsheet:
    """Три вызова gspread, которые использует шлюз, и ``values_batch_update`` для инструмента заполнения."""

    id = "SHEET"

    def __init__(self, tabs: dict[str, list[list]]):
        self.tabs = {t: [[_to_serial(c) for c in row] for row in g] for t, g in copy.deepcopy(tabs).items()}
        self.gids = {t: 100 + i for i, t in enumerate(self.tabs)}
        self.batches: list[list[dict]] = []
        self.get_calls: list[tuple[list[str], str]] = []
        self.fail_plan: list[tuple[str, int, bool]] = []  # (метод, код http, after_apply)

    def _maybe_fail(self, method: str, apply):
        for i, (m, code, after) in enumerate(self.fail_plan):
            if m == method:
                del self.fail_plan[i]
                if after:
                    apply()
                raise api_error(code)
        apply()

    def fetch_sheet_metadata(self):
        return {"sheets": [{"properties": {"title": t, "sheetId": g}} for t, g in self.gids.items()]}

    def _tab_of(self, gid: int) -> str:
        return next(t for t, g in self.gids.items() if g == gid)

    def values_batch_get(self, ranges, params=None):
        self.get_calls.append((list(ranges), (params or {}).get("valueRenderOption", "")))
        out = []
        for rng in ranges:
            m = re.fullmatch(r"'((?:[^']|'')*)'(?:!(.*))?", rng)
            tab = m[1].replace("''", "'")
            grid = self.tabs[tab]
            rows = re.fullmatch(r"(\d+):(\d+)", m[2] or "")
            if rows:
                grid = grid[: int(rows[2])]
            rows_out = [list(r) for r in grid]
            while rows_out and not any(c not in (None, "") for c in rows_out[-1]):
                rows_out.pop()
            out.append({"values": [[("" if c is None else c) for c in r] for r in rows_out]} if rows_out else {})
        return {"valueRanges": out}

    def values_batch_update(self, body):
        for item in body["data"]:
            tab = re.match(r"'((?:[^']|'')*)'", item["range"])[1].replace("''", "'")
            self.tabs[tab] = [list(r) for r in item["values"]]

    def batch_update(self, body):
        replies: list[dict] = []

        def apply():
            for req in body["requests"]:
                replies.append(self._apply(req))

        self.batches.append(body["requests"])
        self._maybe_fail("batch_update", apply)
        return {"replies": replies}

    def _apply(self, req: dict) -> dict:
        if "addSheet" in req:
            p = req["addSheet"]["properties"]
            gid = 100 + len(self.gids) + 50
            self.gids[p["title"]] = gid
            self.tabs[p["title"]] = []
            return {"addSheet": {"properties": {"sheetId": gid, "title": p["title"]}}}
        if "insertDimension" in req:
            r = req["insertDimension"]["range"]
            tab, at = self._tab_of(r["sheetId"]), r["startIndex"] + 1
            for row in self.tabs[tab]:
                if len(row) >= at:
                    row.insert(at - 1, None)
                for i, c in enumerate(row):
                    if isinstance(c, str) and c.startswith("="):
                        row[i] = _shift_refs(c, at)
        elif "updateCells" in req:
            u = req["updateCells"]
            tab, grid = self._tab_of(u["start"]["sheetId"]), None
            grid = self.tabs[tab]
            r0, c0 = u["start"]["rowIndex"], u["start"]["columnIndex"]
            for dr, row in enumerate(u["rows"]):
                while len(grid) <= r0 + dr:
                    grid.append([])
                for dc, cell in enumerate(row["values"]):
                    line = grid[r0 + dr]
                    while len(line) <= c0 + dc:
                        line.append(None)
                    uev = cell.get("userEnteredValue")
                    line[c0 + dc] = None if uev is None else next(iter(uev.values()))
        return {}

    def cell(self, tab: str, row: int, col: str):
        line = self.tabs[tab][row - 1]
        i = col_index(col) - 1
        return line[i] if i < len(line) else None


@pytest.fixture
def ss():
    return FakeSpreadsheet({"Садик": GRID, "Школа": GRID, "Заметки": [["hello"]]})


@pytest.fixture
def gw(ss):
    return GoogleSheetGateway(ss, sleep=lambda _s: None)


def serial(d: date) -> int:
    return (d - date(1899, 12, 30)).days


def last_date() -> date:
    # В образце 2026-09-30 повторяется в двух колонках, поэтому берём последнюю уникальную дату.
    return date(2026, 9, 29)


def first_date(grid=GRID) -> date:
    snap = parse_grid(grid)
    return min(date.fromisoformat(k) for k in snap.date_columns)


def first_position(snap):
    return snap.positions[0]


def test_list_site_tabs_skips_foreign_layout(gw, caplog):
    assert gw.list_site_tabs() == ["Садик", "Школа"]
    gw.list_site_tabs()
    assert sum("skipped" in r.message for r in caplog.records) == 1  # записано в лог один раз


def test_load_snapshots_one_batch_get_for_all_tabs(gw, ss):
    snaps = gw.load_snapshots(["Садик", "Школа"])
    assert len(ss.get_calls) == 1
    assert ss.get_calls[0] == (["'Садик'", "'Школа'"], "UNFORMATTED_VALUE")
    assert snaps["Садик"].date_columns == parse_grid(GRID).date_columns
    assert snaps["Садик"].sheet_url.endswith("#gid=100")


def test_quote_tab_doubles_apostrophes():
    assert quote_tab("O'Neil") == "'O''Neil'"


def test_unknown_tab_raises(gw):
    with pytest.raises(SheetsError, match="не найден"):
        gw.load_snapshots(["Нет такого"])


def test_existing_date_column_found_no_write(gw, ss):
    day = last_date()
    letter = gw.ensure_date_column("Садик", day)
    assert letter == parse_grid(GRID).date_columns[day.isoformat()]
    assert ss.batches == []


def test_new_column_for_later_date_created_once(gw, ss):
    day = date(2026, 12, 1)
    letter = gw.ensure_date_column("Садик", day)
    assert gw.ensure_date_column("Садик", day) == letter
    assert len(ss.batches) == 1
    kinds = [next(iter(r)) for r in ss.batches[0]]
    assert kinds[:2] == ["insertDimension", "copyPaste"]
    col = col_index(letter)
    assert ss.tabs["Садик"][2][col - 1] == serial(day)
    assert ss.cell("Садик", 4, letter) == "объем"
    assert parse_grid(ss.tabs["Садик"]).date_columns[day.isoformat()] == letter


def test_date_between_two_existing_dates_inserts_in_order(gw, ss):
    dates = sorted(date.fromisoformat(k) for k in parse_grid(GRID).date_columns)
    gap = next(a for a, b in zip(dates, dates[1:], strict=False) if (b - a).days > 1)
    day = date.fromordinal(gap.toordinal() + 1)
    letter = gw.ensure_date_column("Садик", day)
    gw.ensure_date_column("Садик", day)
    assert len(ss.batches) == 1
    snap = parse_grid(ss.tabs["Садик"])
    keys = list(snap.date_columns)
    assert keys == sorted(keys) and day.isoformat() in keys and snap.date_columns[day.isoformat()] == letter
    assert len(keys) == len(parse_grid(GRID).date_columns) + 1
    copy_req = next(r["copyPaste"] for r in ss.batches[0] if "copyPaste" in r)
    assert copy_req["source"]["startColumnIndex"] == col_index(letter)  # более поздняя дата, сдвинута вправо


def test_sum_range_extension_sent_and_covers_new_column_for_earliest_date(gw, ss):
    day = date.fromordinal(first_date().toordinal() - 1)
    letter = gw.ensure_date_column("Садик", day)
    formulas = [r for r in ss.batches[0] if "updateCells" in r and "formulaValue" in str(r)]
    assert formulas, "SUM extension request expected"
    done = ss.tabs["Садик"][4][8]
    m = re.fullmatch(r"=SUM\(([A-Z]+)5:([A-Z]+)5\)", done)
    assert col_index(m[1]) <= col_index(letter) <= col_index(m[2])  # состояние таблицы, а не только запрос
    assert m[1] == letter


def test_write_cells_to_a1_and_none_clears(gw, ss):
    pos = parse_grid(GRID).positions
    day = last_date()
    letter = parse_grid(GRID).date_columns[day.isoformat()]
    gw.write_cells("Садик", day, [CellWrite(position_id=pos[0].id, expected_name=pos[0].name, value=7.5)])
    assert ss.cell("Садик", pos[0].id, letter) == 7.5
    gw.write_cells("Садик", day, [CellWrite(position_id=pos[0].id, expected_name=pos[0].name, value=None)])
    assert ss.cell("Садик", pos[0].id, letter) is None


def test_write_to_new_date_is_one_batch_with_insert_and_values(gw, ss):
    pos = parse_grid(GRID).positions[1]
    day = date(2026, 12, 2)
    gw.write_cells("Садик", day, [CellWrite(position_id=pos.id, expected_name=pos.name, value=3)])
    assert len(ss.batches) == 1
    letter = parse_grid(ss.tabs["Садик"]).date_columns[day.isoformat()]
    assert ss.cell("Садик", pos.id, letter) == 3


def test_name_mismatch_refuses_whole_write(gw, ss):
    pos = parse_grid(GRID).positions
    before = copy.deepcopy(ss.tabs["Садик"])
    writes = [
        CellWrite(position_id=pos[0].id, expected_name=pos[0].name, value=1),
        CellWrite(position_id=pos[1].id, expected_name="другое имя", value=2),
    ]
    with pytest.raises(SheetsError, match="Ничего не записано"):
        gw.write_cells("Садик", date(2026, 12, 3), writes)
    assert ss.tabs["Садик"] == before and ss.batches == []


def test_write_to_section_row_refused(gw, ss):
    with pytest.raises(SheetsError):
        gw.write_cells("Садик", last_date(), [CellWrite(position_id=4, expected_name="ОВ 3", value=1)])


def test_429_then_success(gw, ss):
    ss.fail_plan = [("batch_update", 429, False)]
    pos = first_position(parse_grid(GRID))
    gw.write_cells("Садик", last_date(), [CellWrite(position_id=pos.id, expected_name=pos.name, value=2)])
    letter = parse_grid(GRID).date_columns[last_date().isoformat()]
    assert ss.cell("Садик", pos.id, letter) == 2


def test_persistent_5xx_raises_sheets_error(gw, ss):
    ss.fail_plan = [("batch_update", 503, False)] * 10
    pos = first_position(parse_grid(GRID))
    with pytest.raises(SheetsError, match="недоступны"):
        gw.write_cells("Садик", last_date(), [CellWrite(position_id=pos.id, expected_name=pos.name, value=2)])
    assert len(ss.batches) == 4  # первая попытка + 3 повтора


def test_403_is_not_retried(gw, ss):
    ss.fail_plan = [("batch_update", 403, False)]
    with pytest.raises(SheetsError, match="права редактора"):
        gw.ensure_date_column("Садик", date(2026, 12, 9))
    assert len(ss.batches) == 1


def test_retry_after_insert_applied_does_not_duplicate_column(gw, ss):
    day = date(2026, 12, 5)
    ss.fail_plan = [("batch_update", 503, True)]  # вставка применена, ответ потерян
    letter = gw.ensure_date_column("Садик", day)
    snap = parse_grid(ss.tabs["Садик"])
    assert list(snap.date_columns).count(day.isoformat()) == 1
    assert len(snap.date_columns) == len(parse_grid(GRID).date_columns) + 1
    assert snap.date_columns[day.isoformat()] == letter
    assert len(ss.batches) == 1  # повтор заново прочитал строку дат и нашёл колонку


def test_factory_fixture_and_google_dispatch(monkeypatch):
    monkeypatch.setattr(Config, "SHEETS_BACKEND", "fixture")
    monkeypatch.setattr(Config, "SHEET_FIXTURE_FILE", str(Path(__file__).parent / "fixtures" / "sample_grid.json"))
    monkeypatch.setattr(Config, "FIXTURE_SITES", "А, Б")
    gateway = make_sheet_gateway()
    assert isinstance(gateway, FakeSheetGateway) and gateway.list_site_tabs() == ["А", "Б"]
    monkeypatch.setattr(Config, "SHEETS_BACKEND", "nope")
    with pytest.raises(SheetsError):
        make_sheet_gateway()
    monkeypatch.setattr(Config, "SHEETS_BACKEND", "google")
    monkeypatch.setattr(Config, "GOOGLE_SHEET_ID", "")
    with pytest.raises(SheetsError, match="GOOGLE_SHEET_ID"):
        make_sheet_gateway()


def test_seed_creates_tabs_idempotently():
    ss = FakeSpreadsheet({})
    urls = seed(ss, ["Садик", "Школа"], XLSX)
    assert list(urls) == ["Садик", "Школа"] and all("#gid=" in u for u in urls.values())
    gw = GoogleSheetGateway(ss)
    assert gw.list_site_tabs() == ["Садик", "Школа"]
    snap = gw.load_snapshots(["Садик"])["Садик"]
    assert len(snap.positions) > 10 and snap.date_columns
    batches_before, tabs_before = len(ss.batches), copy.deepcopy(ss.tabs)
    seed(ss, ["Садик", "Школа"], XLSX)
    assert len(ss.batches) == batches_before and ss.tabs == tabs_before
    assert any("mergeCells" in r for b in ss.batches for r in b)
    assert any("repeatCell" in r for b in ss.batches for r in b)


def test_seed_refuses_tab_with_foreign_data():
    ss = FakeSpreadsheet({"Садик": [["чужое"]]})
    with pytest.raises(SheetsError, match="чужими данными"):
        seed(ss, ["Садик"], XLSX)
