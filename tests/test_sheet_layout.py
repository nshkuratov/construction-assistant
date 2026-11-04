from datetime import date

import pytest

from src.domain.sheet_layout import (
    cell_at,
    col_index,
    col_letter,
    parse_date,
    parse_grid,
    plan_date_column,
)

# Посчитано скриптом по tests/fixtures/sample_grid.json (строки с названием и единицей). В тексте
# задания написано "около 65".
POSITION_COUNT = 62
DATE_COLUMN_COUNT = 39


@pytest.fixture
def snap(sample_grid):
    return parse_grid(sample_grid)


def test_position_count_matches_the_script_count(snap):
    assert len(snap.positions) == POSITION_COUNT
    assert len(snap.date_columns) == DATE_COLUMN_COUNT


def test_sections_and_groups(snap):
    assert {p.section for p in snap.positions} == {"ОВ 3", "ЭМ3"}
    groups = {p.group for p in snap.positions if p.group}
    assert groups == {"Материалы для сист К1-К7", "Дренаж системы К1…К7", "Крепежные материалы сичтемы К1…К7"}
    names = {p.name for p in snap.positions}
    structural = groups | {"ОВ 3", "ЭМ3"}
    assert not names & structural
    assert not [n for n in names if n.upper().startswith("ИТОГО")]
    em = [p for p in snap.positions if p.section == "ЭМ3"]
    assert len(em) == 18 and all(p.group is None for p in em)  # новый раздел сбрасывает группу


def test_last_row_without_number_is_a_position(snap):
    last = snap.positions[-1]
    assert (last.id, last.number, last.name) == (71, None, "Прокладка силовых кабелей на скобах")


def test_duplicate_names_are_two_positions(snap):
    a, b = (p for p in snap.positions if p.id in (48, 49))
    assert a.name == b.name and a.id != b.id and (a.number, b.number) == (44, 45)


def test_row_17_values_and_done_total(snap):
    p = next(p for p in snap.positions if p.id == 17)
    assert p.values == {
        "2026-08-21": 15.0,
        "2026-08-24": 11.0,
        "2026-08-25": 15.0,
        "2026-08-29": 46.0,
        "2026-08-31": 53.0,
    }
    assert p.done_total == 140.0
    assert (p.planned, p.left) == (265.0, 125.0)


def test_planned_none_gives_left_none(snap):
    p = next(p for p in snap.positions if p.id == 68)  # "Установка АВ на DIN-рейку": цена пустая
    assert p.unit_price is None and p.planned == 3.0
    p.planned = None
    assert p.left is None


def test_units_are_normalised_and_columns_are_letters(snap):
    cable = next(p for p in snap.positions if p.id == 55)
    assert (cable.unit, cable.unit_norm) == ("М", "м")
    assert snap.date_columns["2026-08-19"] == "J"
    assert snap.date_columns["2026-09-30"] == "AW"


def test_missing_done_header_is_an_error():
    with pytest.raises(ValueError, match="Выполненный объем"):
        parse_grid([["a", "b"], [], ["x"]])


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (46253, date(2026, 8, 19)),  # серийный номер: 2026-08-19 это 46253 дня после 1899-12-30
        ("2026-08-19", date(2026, 8, 19)),
        ("19.08.2026", date(2026, 8, 19)),
        ("19.08", date(2026, 8, 19)),
        ("32.13", None),
        ("объем", None),
        (5, None),
        (True, None),
    ],
)
def test_parse_date_forms(raw, expected):
    assert parse_date(raw, year=2026) == expected


def test_column_letters_round_trip():
    assert [col_letter(i) for i in (1, 26, 27, 52, 53, 702, 703)] == ["A", "Z", "AA", "AZ", "BA", "ZZ", "AAA"]
    assert all(col_index(col_letter(i)) == i for i in range(1, 800))


def test_existing_date_is_reused(sample_grid):
    plan = plan_date_column(sample_grid, date(2026, 8, 24))
    assert (plan.insert, plan.shift_right, plan.column_letter) == (False, False, "M")
    assert plan.formula_updates == []


def test_date_after_the_last_takes_the_next_column(sample_grid):
    plan = plan_date_column(sample_grid, date(2026, 10, 5))
    assert (plan.insert, plan.shift_right, plan.column_letter) == (True, False, "AX")
    assert plan.formula_updates == []  # AX лежит внутри SUM(J:CZ)


def test_date_between_two_dates_is_inserted_between(sample_grid):
    # 2026-08-27 лежит между 08-26 (O) и 08-28 (P)
    plan = plan_date_column(sample_grid, date(2026, 8, 27))
    assert (plan.insert, plan.shift_right, plan.column_letter) == (True, True, "P")


def test_append_beyond_a_short_range_extends_formulas(sample_grid):
    for row in sample_grid[4:]:
        if len(row) > 8 and isinstance(row[8], str) and "SUM(J" in row[8]:
            row[8] = row[8].replace(":CZ", ":AW")
    plan = plan_date_column(sample_grid, date(2026, 10, 5))
    assert plan.column_letter == "AX"
    done_rows = [r for r in range(5, 75) if str(cell_at(sample_grid, r, 9)).startswith("=SUM(J")]
    assert {c for c, _ in plan.formula_updates} == {f"I{r}" for r in done_rows}
    assert all(f == f"=SUM(J{c[1:]}:AX{c[1:]})" for c, f in plan.formula_updates)


def test_earliest_date_insert_extends_the_range_to_the_new_column(sample_grid):
    plan = plan_date_column(sample_grid, date(2026, 8, 1))
    assert (plan.insert, plan.shift_right, plan.column_letter) == (True, True, "J")
    update = dict(plan.formula_updates)
    assert update["I5"] == "=SUM(J5:DA5)"  # диапазон J:CZ сдвинут в K:DA, затем расширен обратно до J
    assert update["I52"] == "=SUM(J52:DA52)"


def test_vertical_sums_are_left_alone(sample_grid):
    plan = plan_date_column(sample_grid, date(2026, 8, 1))
    assert "I52" in dict(plan.formula_updates)
    assert all(cell.startswith("I") for cell, _ in plan.formula_updates)
