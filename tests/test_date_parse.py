from datetime import date

import pytest

from src.domain.date_parse import resolve_date

TODAY = date(2026, 11, 3)  # вторник


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("сегодня", date(2026, 11, 3)),
        ("вчера", date(2026, 11, 2)),
        ("позавчера", date(2026, 11, 1)),
        ("во вторник", date(2026, 11, 3)),
        ("в пятницу", date(2026, 10, 30)),
        ("в понедельник", date(2026, 11, 2)),
        ("2 ноября", date(2026, 11, 2)),
        ("за 2 ноября", date(2026, 11, 2)),
        ("02.11", date(2026, 11, 2)),
        ("2.11", date(2026, 11, 2)),
        ("02.11.2026", date(2026, 11, 2)),
        ("02.11.26", date(2026, 11, 2)),
        ("2026-11-02", date(2026, 11, 2)),
        ("5 ноября", date(2025, 11, 5)),
    ],
)
def test_forms(text, expected):
    result = resolve_date(text, TODAY)
    assert result is not None and result.day == expected and not result.future


def test_month_and_year_boundary():
    assert resolve_date("вчера", date(2026, 1, 1)).day == date(2025, 12, 31)
    assert resolve_date("31 декабря", date(2026, 1, 2)).day == date(2025, 12, 31)
    assert resolve_date("позавчера", date(2026, 3, 1)).day == date(2026, 2, 27)


def test_leap_day_without_year_goes_back_to_a_leap_year():
    assert resolve_date("29 февраля", date(2026, 3, 1)).day == date(2024, 2, 29)


def test_explicit_future_date_is_flagged():
    result = resolve_date("05.11.2026", TODAY)
    assert result is not None and result.day == date(2026, 11, 5) and result.future


@pytest.mark.parametrize("text", ["", "абракадабра", "31 апреля", "32.11", "13.13", "30.02.2026"])
def test_unparseable_is_none(text):
    assert resolve_date(text, TODAY) is None
