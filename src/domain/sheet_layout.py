"""Чистые разбор и планирование разметки листа с дневным отчётом (без ввода-вывода и чтения часов).

Правила разметки, взятые из образца таблицы:
- заголовки в строках 1-2; столбец "Выполненный объем" содержит по одной формуле ``SUM`` на строку;
- строка дат — первая строка под заголовками с датой в первом столбце справа от этого заголовка;
- строка с названием и БЕЗ единицы — раздел (первый или сразу после строки "ИТОГО") или группа
  (внутри раздела); строки "ИТОГО…"/"ВСЕГО…" — итоги; строка с названием И единицей — позиция;
- строки и ``Position.id`` — номера строк листа с единицы, столбцы тоже нумеруются с единицы.

``parse_grid`` читает только константы, поэтому сетка в виде формул тоже подходит.
"""

import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from src.domain.units import normalize_unit
from src.models import CatalogSnapshot, Position

_EPOCH = date(1899, 12, 30)  # начало отсчёта серийных дат в Google Таблицах и Excel
_SERIAL_RANGE = (20000, 80000)
_DATE_SCAN_ROWS = 10
_RANGE_RE = re.compile(r"\$?([A-Z]{1,3})\$?(\d+):\$?([A-Z]{1,3})\$?(\d+)")
_TOTAL_PREFIXES = ("итого", "всего")


def col_letter(index: int) -> str:
    """Индекс столбца с единицы в буквы (1 -> A, 27 -> AA)."""
    if index < 1:
        raise ValueError("column index starts at 1")
    out = ""
    while index:
        index, rem = divmod(index - 1, 26)
        out = chr(65 + rem) + out
    return out


def col_index(letters: str) -> int:
    n = 0
    for ch in letters.upper():
        n = n * 26 + ord(ch) - 64
    return n


def cell_at(grid: list[list], row: int, col: int):
    """Значение в (row, col) с единицы; отсутствующая ячейка — ``None`` (строки сетки разной длины)."""
    if row < 1 or col < 1 or row > len(grid):
        return None
    r = grid[row - 1]
    return r[col - 1] if col <= len(r) else None


def parse_date(value, year: int | None = None) -> date | None:
    """Разбирает ячейку с датой: серийное число, ``datetime``/``date``, текст ISO или ``dd.mm[.yyyy]``.

    ``year`` дополняет текст вида ``dd.mm``; без него эта форма даёт ``None``.
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if _SERIAL_RANGE[0] <= value <= _SERIAL_RANGE[1]:
            return _EPOCH + timedelta(days=int(value))
        return None
    if isinstance(value, str):
        text = value.strip()
        m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})(?:[T ].*)?", text)
        if m:
            return _safe_date(int(m[1]), int(m[2]), int(m[3]))
        m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?", text)
        if m:
            y = int(m[3]) if m[3] else year
            return _safe_date(y, int(m[2]), int(m[1])) if y else None
    return None


def _safe_date(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def _number(value) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip().replace(",", ".").replace(" ", "")
        if text.startswith("="):
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _text(value) -> str:
    return " ".join(str(value).split()) if value is not None else ""


def _squash(value) -> str:
    return "".join(_text(value).lower().split())


@dataclass(frozen=True)
class _Layout:
    done_col: int
    date_row: int
    first_date_col: int
    name_col: int
    unit_col: int
    planned_col: int
    price_col: int
    number_col: int


def _find_header(grid: list[list], predicate) -> int | None:
    for row in (1, 2):
        if row > len(grid):
            break
        for col in range(1, len(grid[row - 1]) + 1):
            if predicate(_squash(cell_at(grid, row, col))):
                return col
    return None


def _detect_layout(grid: list[list]) -> _Layout:
    done_col = _find_header(grid, lambda s: s == "выполненныйобъем")
    if done_col is None:
        raise ValueError('header "Выполненный объем" not found in rows 1-2')
    first_date_col = done_col + 1
    date_row = None
    for row in range(3, min(len(grid), _DATE_SCAN_ROWS) + 1):
        if parse_date(cell_at(grid, row, first_date_col), year=2000) is not None:
            date_row = row
            break
    if date_row is None:
        raise ValueError("date row not found right of the done column")
    return _Layout(
        done_col=done_col,
        date_row=date_row,
        first_date_col=first_date_col,
        name_col=_find_header(grid, lambda s: s.startswith("наименование")) or 2,
        unit_col=_find_header(grid, lambda s: s.startswith("едизм")) or 3,
        planned_col=_find_header(grid, lambda s: s in ("к-во", "кол-во", "количество")) or 4,
        price_col=_find_header(grid, lambda s: s.startswith("зпзаед")) or 5,
        number_col=1,
    )


def _date_cells(grid: list[list], layout: _Layout) -> list[tuple[int, date]]:
    """(столбец, дата) для каждой ячейки с датой справа от столбца выполнения, по порядку столбцов."""
    out: list[tuple[int, date]] = []
    year = datetime.now(UTC).year
    row = grid[layout.date_row - 1]
    for col in range(layout.first_date_col, len(row) + 1):
        d = parse_date(row[col - 1], year=year)
        if d is not None:
            out.append((col, d))
            year = d.year
    return out


def parse_grid(
    grid: list[list],
    site_id: str = "",
    sheet_url: str = "",
    taken_at: datetime | None = None,
) -> CatalogSnapshot:
    """Разбирает сетку листа в снимок каталога со ВСЕМИ датами и каждой строкой позиции."""
    layout = _detect_layout(grid)
    dates = _date_cells(grid, layout)
    positions: list[Position] = []
    section, group = "", None
    prev_structural: str | None = None  # None | "total" | "section" | "group" | "position"
    for row in range(layout.date_row + 1, len(grid) + 1):
        name = _text(cell_at(grid, row, layout.name_col))
        if not name:
            continue
        unit = _text(cell_at(grid, row, layout.unit_col))
        if name.lower().startswith(_TOTAL_PREFIXES):
            prev_structural = "total"
        elif not unit:
            if prev_structural in (None, "total"):
                section, group = name, None
                prev_structural = "section"
            else:
                group = name
                prev_structural = "group"
        else:
            number = _number(cell_at(grid, row, layout.number_col))
            values = {}
            for col, d in dates:
                v = _number(cell_at(grid, row, col))
                if v is not None:
                    values[d.isoformat()] = v
            positions.append(
                Position(
                    id=row,
                    number=int(number) if number is not None else None,
                    section=section,
                    group=group,
                    name=name,
                    unit=unit,
                    unit_norm=normalize_unit(unit),
                    planned=_number(cell_at(grid, row, layout.planned_col)),
                    unit_price=_number(cell_at(grid, row, layout.price_col)),
                    values=values,
                )
            )
            prev_structural = "position"
    return CatalogSnapshot(
        site_id=site_id,
        sheet_url=sheet_url,
        taken_at=taken_at or datetime.now(UTC),
        date_columns={d.isoformat(): col_letter(col) for col, d in dates},
        positions=positions,
    )


@dataclass(frozen=True)
class DateColumnPlan:
    """Куда попадает дата отчёта.

    ``column`` — индекс с единицы. ``insert`` равно True, если столбца даты ещё нет и его нужно
    создать (заголовок пишется в ``date_row``). ``shift_right`` равно True, если существующие столбцы
    сдвигаются вправо, освобождая место (новая дата раньше последней). ``formula_updates`` —
    пары ``(ячейка, новая_формула)`` в координатах после вставки.
    """

    column: int
    insert: bool
    shift_right: bool = False
    formula_updates: list[tuple[str, str]] = field(default_factory=list)
    date_row: int = 0

    @property
    def column_letter(self) -> str:
        return col_letter(self.column)


def plan_date_column(grid: list[list], day: date) -> DateColumnPlan:
    """Выбирает столбец для ``day`` и нужные расширения диапазонов SUM.

    В ``grid`` столбец выполнения должен быть в виде формул. Даты остаются по порядку: новая дата
    вставляется перед первой более поздней или добавляется после последней.
    """
    layout = _detect_layout(grid)
    dates = _date_cells(grid, layout)
    existing = next((col for col, d in dates if d == day), None)
    shift = False
    if existing is not None:
        col, insert = existing, False
    else:
        insert = True
        later = next((col for col, d in dates if d > day), None)
        if later is not None:
            col, shift = later, True
        elif dates:
            col = dates[-1][0] + 1
        else:
            col = layout.first_date_col
    return DateColumnPlan(
        column=col,
        insert=insert,
        shift_right=shift,
        formula_updates=_range_updates(grid, layout, col, shift),
        date_row=layout.date_row,
    )


def _range_updates(grid: list[list], layout: _Layout, col: int, shift: bool) -> list[tuple[str, str]]:
    updates: list[tuple[str, str]] = []
    done_letter = col_letter(layout.done_col)
    for row in range(layout.date_row + 1, len(grid) + 1):
        formula = cell_at(grid, row, layout.done_col)
        if not isinstance(formula, str) or not formula.startswith("="):
            continue

        def rewrite(m: re.Match) -> str:
            if m[2] != m[4]:
                return m[0]  # вертикальный или блочный диапазон не является построчным диапазоном дат
            c1, c2 = col_index(m[1]), col_index(m[3])
            if shift:
                c1, c2 = (c1 + 1 if c1 >= col else c1), (c2 + 1 if c2 >= col else c2)
            c1, c2 = min(c1, col), max(c2, col)
            return f"{col_letter(c1)}{m[2]}:{col_letter(c2)}{m[4]}"

        new = _RANGE_RE.sub(rewrite, formula)
        if new != formula:
            updates.append((f"{done_letter}{row}", new))
    return updates
