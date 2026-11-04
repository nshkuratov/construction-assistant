"""Русские фразы с датой в дату относительно заданного ``today``. Чистая функция: без часов и ввода-вывода."""

import re
from datetime import date, timedelta
from typing import NamedTuple

_MONTHS = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4, "мая": 5, "июн": 6, "июл": 7, "август": 8,
    "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12,
}  # fmt: skip
_WEEKDAYS = {
    "понедельник": 0, "вторник": 1, "сред": 2, "четверг": 3, "пятниц": 4, "суббот": 5, "воскресень": 6,
}  # fmt: skip
_LOOKBACK_YEARS = 8

_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_NUMERIC = re.compile(r"(?<![\d.])(\d{1,2})\.(\d{1,2})(?:\.(\d{4}|\d{2}))?(?![\d.])")
_WORDY = re.compile(r"(?<!\d)(\d{1,2})\s+([а-я]+)(?:\s+(\d{4}))?")


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


class ResolvedDate(NamedTuple):
    day: date
    future: bool


def _full_year(raw: str) -> int:
    year = int(raw)
    return 2000 + year if year < 100 else year


def _most_recent(today: date, month: int, day: int) -> date | None:
    """Последняя дата ``day.month``, не позже ``today`` (при необходимости из прошлого года)."""
    for back in range(_LOOKBACK_YEARS):
        candidate = _safe_date(today.year - back, month, day)
        if candidate is not None and candidate <= today:
            return candidate
    return None


def _build(today: date, day: int, month: int, year: int | None) -> ResolvedDate | None:
    if year is None:
        found = _most_recent(today, month, day)
        return ResolvedDate(found, False) if found else None
    found = _safe_date(year, month, day)
    return ResolvedDate(found, found > today) if found else None


def resolve_date(text: str, today: date) -> ResolvedDate | None:
    """Разбирает фразу вроде "вчера", "в пятницу", "2 ноября", "02.11", "02.11.2026".

    Название дня недели означает ближайший такой день не позже ``today`` (сегодня считается). Дата
    без года означает ближайшую не в будущем. Явная дата в будущем даёт ``future=True``.
    Для неразбираемого текста возвращается ``None``.
    """
    text = text.lower().replace("ё", "е").strip()

    if m := _ISO.search(text):
        return _build(today, int(m.group(3)), int(m.group(2)), int(m.group(1)))
    if m := _NUMERIC.search(text):
        year = _full_year(m.group(3)) if m.group(3) else None
        return _build(today, int(m.group(1)), int(m.group(2)), year)
    for m in _WORDY.finditer(text):
        for stem_, month in _MONTHS.items():
            if m.group(2).startswith(stem_):
                year = int(m.group(3)) if m.group(3) else None
                return _build(today, int(m.group(1)), month, year)

    if "позавчера" in text:
        return ResolvedDate(today - timedelta(days=2), False)
    if "вчера" in text:
        return ResolvedDate(today - timedelta(days=1), False)
    if "сегодня" in text:
        return ResolvedDate(today, False)
    for stem_, weekday in _WEEKDAYS.items():
        if stem_ in text:
            return ResolvedDate(today - timedelta(days=(today.weekday() - weekday) % 7), False)
    return None
