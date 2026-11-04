"""Фейки шлюза таблицы и приёмника CRM в памяти — для тестов и разработки без сети.

Оба ведут журнал вызовов (``calls``) и умеют падать по команде: ``fail(method, times=N)`` бросает
типизированную ошибку при следующих N вызовах этого метода. ``after_apply=True`` сначала применяет
эффект и только потом бросает ошибку — так имитируется 5xx, пришедший после того, как сервер уже
сделал работу (повтор не должен дублировать результат).
"""

import copy
import re
from datetime import date

from src.domain.sheet_layout import cell_at, col_index, parse_grid, plan_date_column
from src.interfaces.errors import CrmError, SheetsError
from src.models import CatalogSnapshot, CellWrite, CrmEntry, CrmSite


class _Failures:
    def __init__(self, error: type[Exception]):
        self._error = error
        self._plan: dict[str, list[int | bool]] = {}

    def set(self, method: str, times: int, after_apply: bool) -> None:
        self._plan[method] = [times, after_apply]

    def before(self, method: str) -> bool:
        """Бросает ошибку сразу, если пора падать до применения. Возвращает True, если пора падать после применения."""
        entry = self._plan.get(method)
        if not entry or entry[0] <= 0:
            return False
        entry[0] -= 1
        if entry[1]:
            return True
        raise self._error(f"injected failure in {method}")

    def after(self, method: str) -> None:
        raise self._error(f"injected failure after applying {method}")


def _set_cell(grid: list[list], row: int, col: int, value) -> None:
    r = grid[row - 1]
    while len(r) < col:
        r.append(None)
    r[col - 1] = value


class FakeSheetGateway:
    """Несколько листов, каждый — глубокая копия сетки-фикстуры (список строк, формулы текстом)."""

    def __init__(self, grid: list[list], tabs: list[str], base_url: str = "https://fake.sheets/"):
        self.grids: dict[str, list[list]] = {t: copy.deepcopy(grid) for t in tabs}
        self.calls: list[tuple[str, tuple]] = []
        self._base_url = base_url
        self._failures = _Failures(SheetsError)

    def fail(self, method: str, times: int = 1, after_apply: bool = False) -> None:
        self._failures.set(method, times, after_apply)

    def _grid(self, tab: str) -> list[list]:
        try:
            return self.grids[tab]
        except KeyError:
            raise SheetsError(f"unknown tab {tab!r}") from None

    def list_site_tabs(self) -> list[str]:
        self.calls.append(("list_site_tabs", ()))
        self._failures.before("list_site_tabs")
        return list(self.grids)

    def load_snapshots(self, tabs: list[str]) -> dict[str, CatalogSnapshot]:
        self.calls.append(("load_snapshots", (tuple(tabs),)))
        self._failures.before("load_snapshots")
        return {t: parse_grid(self._grid(t), site_id=t, sheet_url=self.sheet_url(t)) for t in tabs}

    def ensure_date_column(self, tab: str, day: date) -> str:
        self.calls.append(("ensure_date_column", (tab, day)))
        late_failure = self._failures.before("ensure_date_column")
        letter = self._apply_date_column(tab, day)
        if late_failure:
            self._failures.after("ensure_date_column")
        return letter

    def _apply_date_column(self, tab: str, day: date) -> str:
        grid = self._grid(tab)
        plan = plan_date_column(grid, day)
        if plan.insert:
            col = plan.column
            if plan.shift_right:
                for row in grid:
                    if len(row) >= col:
                        row.insert(col - 1, None)
            _set_cell(grid, plan.date_row, col, day.isoformat())
            label = cell_at(grid, plan.date_row + 1, col - 1)
            if isinstance(label, str) and plan.date_row + 1 <= len(grid):
                _set_cell(grid, plan.date_row + 1, col, label)
            for cell, formula in plan.formula_updates:
                m = re.fullmatch(r"([A-Z]+)(\d+)", cell)
                _set_cell(grid, int(m[2]), col_index(m[1]), formula)
        return plan.column_letter

    def write_cells(self, tab: str, day: date, writes: list[CellWrite]) -> None:
        self.calls.append(("write_cells", (tab, day, tuple(writes))))
        late_failure = self._failures.before("write_cells")
        grid = self._grid(tab)
        names = {p.id: p.name for p in parse_grid(grid).positions}
        for w in writes:
            if names.get(w.position_id) != w.expected_name:
                raise SheetsError(f"row {w.position_id} is not {w.expected_name!r}; nothing written")
        col = col_index(self._apply_date_column(tab, day))
        for w in writes:
            _set_cell(grid, w.position_id, col, w.value)
        if late_failure:
            self._failures.after("write_cells")

    def sheet_url(self, tab: str) -> str:
        return f"{self._base_url}{tab}"

    def column_of(self, tab: str, day: date) -> str | None:
        """Помощник для тестов: буква столбца для ``day`` на листе ``tab`` или ``None``."""
        snap = parse_grid(self._grid(tab))
        return snap.date_columns.get(day.isoformat())

    def cell(self, tab: str, row: int, col_letters: str):
        return cell_at(self._grid(tab), row, col_index(col_letters))


class FakeCrmSink:
    def __init__(self, sites: list[CrmSite] | None = None):
        default = [CrmSite(value_id="1", name="Садик"), CrmSite(value_id="2", name="Школа")]
        self.sites = sites if sites is not None else default
        self.entries: dict[tuple[str, date, str], CrmEntry] = {}
        self.calls: list[tuple[str, tuple]] = []
        self._failures = _Failures(CrmError)

    def fail(self, method: str, times: int = 1, after_apply: bool = False) -> None:
        self._failures.set(method, times, after_apply)

    @staticmethod
    def _key(e: CrmEntry) -> tuple[str, date, str]:
        return (e.crm_value_id, e.day, e.position_key)

    def list_sites(self) -> list[CrmSite]:
        self.calls.append(("list_sites", ()))
        self._failures.before("list_sites")
        return list(self.sites)

    def upsert_entries(self, entries: list[CrmEntry]) -> None:
        self.calls.append(("upsert_entries", (tuple(entries),)))
        late_failure = self._failures.before("upsert_entries")
        for e in entries:
            self.entries[self._key(e)] = e
        if late_failure:
            self._failures.after("upsert_entries")

    def delete_entries(self, entries: list[CrmEntry]) -> None:
        self.calls.append(("delete_entries", (tuple(entries),)))
        late_failure = self._failures.before("delete_entries")
        for e in entries:
            self.entries.pop(self._key(e), None)
        if late_failure:
            self._failures.after("delete_entries")

